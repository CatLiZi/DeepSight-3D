"""Failure recovery, fresh-image triggering and fail-closed motor checks; fake IO only."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from locator_demo.capture import JpegParser, StereoCameraCapture
from locator_demo.diagnostics import DiagnosticJournal
from locator_demo.head import AxisConfig, DirectionConfig, HeadPose
from locator_demo.motion import SmoothHeadHardware
from locator_demo.settings import ControlConfig, SavedSettings, VisionProcessingConfig, FeatureConfig, save_settings
from locator_demo.servo_bus import ZPServoBus
from locator_demo.visual_trigger import VisualAxisTrigger
from locator_demo.web.app import DemoRuntime, RuntimeConfig, create_app
from locator_demo.camera import VisionFrameResult, VisionStatus
from locator_demo.vision import FaceTarget
from test_stage1_runtime import FakeAudio, FakeVision
from test_servo_bus import FakeSerial
from test_native_capture import jpeg


class TriggerTests(unittest.TestCase):
    def test_repeated_frame_cannot_confirm_or_start(self):
        t = VisualAxisTrigger()
        for now in (1., 1.1, 2.):
            self.assertEqual(t.update(-.3, band=.04, sequence=1, now=now)[0], 0.)
        self.assertEqual(t.hits, 1)

    def test_two_fresh_frames_confirm_up_and_release_has_hysteresis(self):
        t = VisualAxisTrigger()
        self.assertEqual(t.update(-.06, band=.04, sequence=1, now=1.)[0], 0.)
        self.assertEqual(t.update(-.06, band=.04, sequence=2, now=1.1)[0], -.06)
        value, band = t.update(-.03, band=.04, sequence=3, now=1.2)
        self.assertEqual(value, -.03)
        self.assertAlmostEqual(band, .024)
        self.assertEqual(t.update(-.02, band=.04, sequence=4, now=1.3)[0], 0.)

    def test_jitter_and_sign_reversal_never_instantly_relaunch(self):
        t = VisualAxisTrigger()
        for seq, value in enumerate((-.06, .06, -.06, .06, -.02, .02)):
            self.assertEqual(t.update(value, band=.04, sequence=seq, now=seq*.1)[0], 0.)

    def test_nan_is_rejected(self):
        self.assertEqual(VisualAxisTrigger().update(float('nan'), band=.04, sequence=1, now=1)[0], 0.)


class SerialRecoveryTests(unittest.TestCase):
    def runtime(self):
        with patch('locator_demo.web.app.WasapiArrayAudio', FakeAudio), patch('locator_demo.web.app.StereoVisionTracker', FakeVision), patch('locator_demo.web.app.choose_servo_port', return_value=None):
            r = DemoRuntime(RuntimeConfig())
        r.config.yaw_id, r.config.pitch_id = 0, 2
        self.addCleanup(r.shutdown)
        return r

    def test_noisy_echo_and_wrong_id_do_not_hide_the_correct_frame(self):
        fake = FakeSerial()
        fake.response_map['#002PRAD!'] = b'#002PRAD!#000P1300!noise#002P1567!'
        bus = ZPServoBus(serial_factory=lambda *a, **k: fake)
        self.assertEqual(bus.read_position(2), 1567)
        self.assertEqual(fake.writes, ['#002PRAD!'])

    def test_position_retries_are_bounded_and_do_not_arm(self):
        bus = ZPServoBus()
        with patch.object(bus, 'read_position_response', side_effect=['', '#000P1500!', '#002P1540!']) as read, patch.object(bus, '_write') as write:
            self.assertEqual(bus.read_position(2), 1540)
            self.assertEqual(read.call_count, 3)
            write.assert_not_called()

    def test_partial_write_is_an_error(self):
        fake = FakeSerial()
        fake.write = Mock(return_value=2)
        bus = ZPServoBus(serial_factory=lambda *a, **k: fake)
        with self.assertRaisesRegex(IOError, 'Partial'):
            bus.move_position_only(2, 1550, 300)

    def test_readonly_recovery_discards_partial_success_from_old_session(self):
        r = self.runtime()
        r.bus._serial = FakeSerial()
        with patch.object(r.bus, 'read_position', side_effect=[1504, None, 1512, 1567]) as read, patch.object(r.bus, 'reconnect_readonly') as reopen, patch.object(r.bus, '_write') as write:
            r.sync_motion_pose()
            self.assertEqual([c.args[0] for c in read.call_args_list], [0, 2, 0, 2])
            self.assertEqual(r.pose, HeadPose(1512, 1567))
            reopen.assert_called_once()
            write.assert_not_called()

    def test_missing_pitch_cannot_start_after_one_recovery(self):
        r = self.runtime()
        r.bus._serial = FakeSerial()
        with patch.object(r.bus, 'read_position', side_effect=[1504, None, 1504, None]), patch.object(r.bus, 'reconnect_readonly') as reopen, patch.object(r.bus, '_write') as write:
            with self.assertRaisesRegex(RuntimeError, 'Servo 2.*feedback'):
                r.sync_motion_pose()
            self.assertFalse(r.running)
            self.assertFalse(r._motion_pose_synced)
            reopen.assert_called_once()
            write.assert_not_called()
            self.assertEqual(r.events[-1]['event'], 'motion_sync_error')

    def test_out_of_limits_never_triggers_a_reconnect(self):
        r = self.runtime()
        r.bus._serial = FakeSerial()
        with patch.object(r.bus, 'read_position', side_effect=[1500, 2000]), patch.object(r.bus, 'reconnect_readonly') as reopen:
            with self.assertRaisesRegex(RuntimeError, 'limits'):
                r.sync_motion_pose()
            reopen.assert_not_called()

    def test_stop_cancels_pending_start(self):
        r = self.runtime()
        generation = r.begin_start()
        r.halt_motion()
        with self.assertRaisesRegex(RuntimeError, '取消'):
            r.finish_start(generation)
        self.assertFalse(r.running)

    def test_missing_feedback_during_tracking_stops_both_axes(self):
        r = self.runtime()
        r.running = True
        r.head = SimpleNamespace(yaw=AxisConfig(0), pitch=AxisConfig(2), cancel=Mock(), stop=Mock(),
            metrics=lambda:dict(prepared=True,worker_active=True))
        r.bus = SimpleNamespace(connected=True, parse_position=ZPServoBus.parse_position,
            read_position_response=lambda sid, **k: '#000P1500!' if sid == 0 else '', close=Mock())
        r._control_stop = SimpleNamespace(wait=Mock(side_effect=[False, False, True]), set=Mock())
        r._feedback_loop()
        self.assertFalse(r.running)
        self.assertIn('Servo 2', r._motion_fault)
        r.head.stop.assert_called_once()


class MotorFaultTests(unittest.TestCase):
    def test_io_fault_remains_latched_instead_of_replaying_target(self):
        head = SimpleNamespace(yaw=AxisConfig(0), pitch=AxisConfig(2), prepare=Mock(side_effect=IOError('disconnected')), move_to=Mock())
        smooth = SmoothHeadHardware(head, initial=HeadPose(1500, 1553), config=ControlConfig())
        smooth.move_to(HeadPose(1510, 1560), time_ms=100)
        smooth._worker.join(timeout=1)
        with self.assertRaisesRegex(RuntimeError, 'fresh position'):
            smooth.move_to(HeadPose(1520, 1570), time_ms=100)
        self.assertEqual(smooth.estimated_pose, HeadPose(1500, 1553))
        head.prepare.assert_called_once()
        head.move_to.assert_not_called()
        smooth.stop()

    def test_cancel_during_prepare_does_not_replay_old_goal(self):
        entered, release = threading.Event(), threading.Event()
        def prepare():
            entered.set(); release.wait(1)
        head = SimpleNamespace(yaw=AxisConfig(0), pitch=AxisConfig(2), prepare=prepare, move_to=Mock())
        smooth = SmoothHeadHardware(head, initial=HeadPose(1500, 1553), config=ControlConfig())
        smooth.move_to(HeadPose(1600, 1600), time_ms=100)
        self.assertTrue(entered.wait(.5))
        smooth.cancel(); release.set()
        time.sleep(.05)
        smooth.stop()
        head.move_to.assert_not_called()


class JournalTests(unittest.TestCase):
    def test_failures_survive_restart_and_files_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = DiagnosticJournal(tmp)
            for handler in journal._handlers.values():
                handler.maxBytes = 500
            for n in range(25):
                journal.record('position_error', {'axis': 2, 'sequence': n}, level='error')
            journal.close()
            again = DiagnosticJournal(tmp)
            again.record('restarted'); again.close()
            paths = list(Path(tmp).glob('runtime.jsonl*'))
            self.assertLessEqual(len(paths), 5)
            rows = [json.loads(line) for path in paths for line in path.read_text(encoding='utf-8').splitlines()]
            self.assertTrue(any(row['event'] == 'position_error' for row in rows))
            self.assertEqual(json.loads((Path(tmp)/'runtime.jsonl').read_text().splitlines()[-1])['event'], 'restarted')

    def test_api_error_has_request_id_and_is_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(simulated=True, settings_path=Path(tmp)/'settings.json')
            r = app.state.runtime
            self.addCleanup(r.shutdown)
            with TestClient(app) as client:
                response = client.get('/api/diagnostics/log/not-a-journal')
                self.assertEqual(response.status_code, 404)
                identity = response.headers['X-Request-ID']
                self.assertTrue(any(row['event']=='api_error' and row['request_id']==identity for row in r.journal.recent))


class CameraRecoveryTests(unittest.TestCase):
    def test_parser_resynchronizes_after_truncated_jpeg(self):
        data = jpeg()
        self.assertEqual(JpegParser().feed(data[:40]+data), [data])

    def test_old_capture_process_cannot_publish_into_new_session(self):
        capture = StereoCameraCapture(width=128, height=36)
        capture._process = object()
        capture._accept(jpeg(), process=object())
        self.assertIsNone(capture.latest())

    def test_stall_watchdog_only_restarts_capture_and_keeps_profile(self):
        capture = StereoCameraCapture(width=128, height=36)
        capture._enabled = True
        capture._process = SimpleNamespace(poll=lambda:None)
        capture._accept(jpeg(), now=time.perf_counter()-5)
        capture._watchdog_stop = SimpleNamespace(wait=Mock(side_effect=[False, True]))
        capture.start = Mock()
        capture._close_process = Mock()
        capture._watch_stream()
        capture._close_process.assert_called_once()
        self.assertEqual(capture._recoveries, 1)
        self.assertEqual((capture.width,capture.height,capture.requested_fps), (128,36,60))


class RuntimeVerticalTests(unittest.TestCase):
    def test_moderate_vertical_face_motion_triggers_both_directions_on_fresh_frames(self):
        for y, sign in ((.08,-1),(-.08,1)):
            with self.subTest(y=y), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)/'settings.json'
                save_settings(path, SavedSettings(direction=DirectionConfig(pitch_up_sign=1), vision=VisionProcessingConfig(tracking_strategy='visual_priority'),
                    features=FeatureConfig(audio_enabled=False,visual_enabled=True,camera_enabled=True)))
                r = DemoRuntime(RuntimeConfig(simulated=True,settings_path=path))
                self.addCleanup(r.shutdown)
                target=FaceTarget(270,180+y*180-50,370,180+y*180+50,score=.95,face_id=1)
                frame=VisionFrameResult(target,640,360,'',VisionStatus(True,'fixture','ok'),targets=[target],
                    control_ready=True,detection_sequence=1,binocular={'target_selection':{'locked_person_id':1}})
                r.vision=SimpleNamespace(read=lambda:frame,stop=Mock(),capture_metrics=lambda:{},set_audio_guidance=Mock())
                r.running=True; r.move=Mock()
                r.tick(include_frame=False)
                r.move.assert_not_called()
                frame=replace(frame,detection_sequence=2)
                with patch('locator_demo.web.app.time.time', return_value=time.time()+.1):
                    r.tick(include_frame=False)
                    r.tick(include_frame=False)
                    self.assertEqual(r._visual_control['reason'],'image_feedback')
                r.move.assert_called_once()
                self.assertGreater((r.move.call_args.args[0].pitch-1500)*sign,0)
                self.assertEqual(r._visual_control['pitch']['reason'],'tracking')


if __name__=='__main__': unittest.main()
