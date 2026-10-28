import tempfile
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from locator_demo.array_audio import WasapiArrayAudio
from locator_demo.audio import AudioDirection
from locator_demo.capture import WasapiAudioCapture
from locator_demo.ear_localization import EarPairConfig, EarPairLocalizer
from locator_demo.head import HeadPose
from locator_demo.settings import AudioCaptureConfig, CameraConfig, SavedSettings, load_settings, save_settings
from locator_demo.stereo_vision import StereoVisionTracker, prepare_stereo_eyes
from locator_demo.web.app import DemoRuntime, RuntimeConfig


class EarLocalizationTests(unittest.TestCase):
    def setUp(self):
        self.config = EarPairConfig('ear_pair', 3, 2, True, .1317624)

    def audio(self, delay=9):
        n = 1920
        rng = np.random.default_rng(75)
        signal = rng.normal(size=n + 100)
        spectrum = np.fft.rfft(signal)
        freq = np.fft.rfftfreq(len(signal), 1 / 48000)
        spectrum *= (freq > 250) & (freq < 5500)
        signal = np.fft.irfft(spectrum, n=len(signal))
        signal *= 4000 / np.std(signal)
        out = np.zeros((n, 4), np.int16)
        out[:, 2] = signal[40:40 + n].astype(np.int16)
        out[:, 1] = signal[40 - delay:40 - delay + n].astype(np.int16)
        return out

    def test_left_arrives_first_means_robot_left(self):
        result = EarPairLocalizer(self.config).estimate(self.audio(9))
        self.assertEqual(result.direction, AudioDirection.LEFT)
        self.assertGreater(result.azimuth_deg, 20)
        self.assertLess(result.azimuth_deg, 40)
        self.assertAlmostEqual(result.tdoa_s * 48000, 9, delta=.5)

    def test_right_arrives_first_means_robot_right(self):
        result = EarPairLocalizer(self.config).estimate(self.audio(-9))
        self.assertEqual(result.direction, AudioDirection.RIGHT)
        self.assertLess(result.azimuth_deg, -20)

    def test_body_channels_cannot_affect_direction_or_speech(self):
        a = self.audio()
        b = a.copy()
        b[:, 0] = np.random.default_rng(2).integers(-32768, 32767, len(b), dtype=np.int16)
        b[:, 3] = 32767
        self.assertEqual(EarPairLocalizer(self.config).estimate(a), EarPairLocalizer(self.config).estimate(b))
        b[:, 1:3] = 0
        self.assertEqual(EarPairLocalizer(self.config).estimate(b).direction, AudioDirection.UNKNOWN)

    def test_copied_channels_and_clipping_are_not_directions(self):
        data = self.audio()
        data[:, 1] = data[:, 2]
        result = EarPairLocalizer(self.config).estimate(data)
        self.assertEqual(result.direction, AudioDirection.UNKNOWN)
        self.assertEqual(result.noise_state, 'channels_nearly_identical')
        data[:, 2] = 32767
        self.assertEqual(EarPairLocalizer(self.config).estimate(data).noise_state, 'ear_clipping')

    def test_mapping_requires_confirmation_and_distinct_channels(self):
        config = replace(self.config, mapping_confirmed=False)
        self.assertFalse(config.ready)
        self.assertEqual(EarPairLocalizer(config).estimate(self.audio()).direction, AudioDirection.UNKNOWN)
        with self.assertRaises(ValueError):
            replace(config, left_channel=2)
        with self.assertRaises(ValueError):
            replace(config, right_channel=5)

    def test_asd_mono_excludes_body_channels(self):
        audio = WasapiArrayAudio(ear_config=self.config)
        raw = np.tile(np.array([30000, 2000, 4000, -30000], '<i2'), (960, 1)).tobytes()
        audio.capture._callback(raw, 960, SimpleNamespace(currentTime=1., inputBufferAdcTime=1.), SimpleNamespace(input_overflow=False))
        rate, samples = audio.recent_mono_audio(seconds=.02)
        self.assertEqual(rate, 48000)
        np.testing.assert_allclose(samples, 3000 / 32768)

    def test_stale_audio_cannot_keep_a_direction_alive(self):
        audio = WasapiArrayAudio(ear_config=self.config)
        audio.capture.start = lambda: None
        audio.capture._callback(self.audio().tobytes(), 1920,
                                SimpleNamespace(currentTime=1., inputBufferAdcTime=1.), SimpleNamespace(input_overflow=False))
        self.assertEqual(audio.read_estimate().direction, AudioDirection.LEFT)
        old = replace(audio.capture.latest(), received_monotonic=time.perf_counter() - 1)
        audio.capture._history.clear()
        audio.capture._history.append(old)
        self.assertEqual(audio.read_estimate().direction, AudioDirection.UNKNOWN)
        self.assertFalse(audio.localization_ready)


class AssemblyCameraTests(unittest.TestCase):
    def test_rotation_preserves_eye_order_and_native_size(self):
        data = np.random.default_rng(1).integers(30, 220, (36, 128, 3), dtype=np.uint8)
        original = data.copy()
        images, health = prepare_stereo_eyes(data, 180, 180)
        np.testing.assert_array_equal(images['left'], cv2.rotate(data[:, :64], cv2.ROTATE_180))
        np.testing.assert_array_equal(images['right'], cv2.rotate(data[:, 64:], cv2.ROTATE_180))
        np.testing.assert_array_equal(data, original)
        self.assertTrue(health['left']['valid'])

    def test_auto_fallback_rejects_white_eye_and_uses_other_eye(self):
        tracker = StereoVisionTracker(video_size='128x36', output_width=320, left_rotation=180, right_rotation=180)
        tracker.capture.start = lambda: None
        original_metrics = tracker.capture.metrics
        tracker.capture.metrics = lambda: {**original_metrics(), 'ok': True}
        tracker._detect_image_targets = lambda _: []
        image = np.random.default_rng(5).integers(40, 200, (36, 128, 3), dtype=np.uint8)
        image[:, :64] = 255
        tracker.capture._accept(cv2.imencode('.jpg', image)[1].tobytes())
        try:
            tracker._start()
            deadline = time.perf_counter() + 2
            while tracker.capture_metrics()['processed_frames'] == 0 and time.perf_counter() < deadline:
                time.sleep(.01)
            self.assertEqual(tracker.capture_metrics()['detector_eye'], 'right')
            self.assertFalse(tracker.capture_metrics()['eye_health']['left']['valid'])
            self.assertTrue(tracker.read().status.ok)
        finally:
            tracker.stop()

    def test_assembly_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'settings.json'
            settings = SavedSettings(camera=CameraConfig(left_rotation=180, right_rotation=180, tracking_eye='auto'),
                                     audio_capture=AudioCaptureConfig(localization_mode='ear_pair', ear_left_channel=3,
                                         ear_right_channel=2, ear_mapping_confirmed=True, ear_spacing_m=.1317624))
            save_settings(p, settings)
            loaded = load_settings(p)
            self.assertEqual(loaded.camera, settings.camera)
            self.assertEqual(loaded.audio_capture, settings.audio_capture)

    def test_wasapi_raw_option_is_applied_to_this_stream_only(self):
        created = []
        class Stream:
            active = False
            def __init__(self, **kwargs):
                created.append(kwargs)
            def start(self): self.active = True
            def stop(self): self.active = False
            def close(self): pass
        module = SimpleNamespace(WasapiSettings=lambda **k: SimpleNamespace(_streaminfo=SimpleNamespace(streamOption=0)), RawInputStream=Stream)
        with patch('locator_demo.capture.choose_wasapi_input', return_value={'index': 1, 'name': 'YDPI4MIC'}):
            cap = WasapiAudioCapture(audio_module=module)
            try:
                cap.start()
                self.assertEqual(created[0]['extra_settings']._streaminfo.streamOption, 1)
                self.assertEqual(created[0]['channels'], 4)
            finally:
                cap.stop()


class AssemblyMotionTests(unittest.TestCase):
    def setUp(self):
        ports=patch('locator_demo.web.app.servo_port_candidates',return_value=[])
        ports.start();self.addCleanup(ports.stop)
    def runtime(self):
        from test_stage1_runtime import FakeAudio, FakeVision
        with patch('locator_demo.web.app.WasapiArrayAudio', FakeAudio), patch('locator_demo.web.app.StereoVisionTracker', FakeVision), patch('locator_demo.web.app.choose_servo_port', return_value=None):
            runtime = DemoRuntime(RuntimeConfig())
        runtime.config.yaw_id = 0
        runtime.config.pitch_id = 2
        return runtime

    def test_motion_initial_pose_comes_from_feedback_without_a_home_command(self):
        runtime = self.runtime()
        with patch.object(runtime.bus, 'read_position', side_effect=[1504, 1577]), patch.object(runtime.bus, '_write') as write:
            try:
                runtime.sync_motion_pose()
                self.assertEqual(runtime.pose, HeadPose(1504, 1577))
                self.assertEqual(runtime.head.estimated_pose, HeadPose(1504, 1577))
                self.assertTrue(runtime._motion_pose_synced)
                write.assert_not_called()
            finally:
                runtime.shutdown()

    def test_missing_feedback_stops_startup_instead_of_guessing_pose(self):
        runtime = self.runtime()
        runtime.running = True
        with patch.object(runtime.bus, 'read_position', return_value=None), patch.object(runtime.bus, '_write') as write:
            try:
                with self.assertRaisesRegex(RuntimeError, 'feedback'):
                    runtime.sync_motion_pose()
                self.assertFalse(runtime.running)
                self.assertFalse(runtime._motion_pose_synced)
                write.assert_not_called()
            finally:
                runtime.shutdown()

    def test_feedback_outside_limits_blocks_motion(self):
        runtime = self.runtime()
        with patch.object(runtime.bus, 'read_position', return_value=2000), patch.object(runtime.bus, '_write') as write:
            try:
                with self.assertRaisesRegex(RuntimeError, 'limits'):
                    runtime.sync_motion_pose()
                write.assert_not_called()
            finally:
                runtime.shutdown()

    def test_manual_pitch_checks_only_pitch_and_never_enables_missing_yaw(self):
        runtime = self.runtime()
        runtime.control = replace(runtime.control, smooth_motion=False)
        runtime.direction = replace(runtime.direction, pitch_up_sign=1)
        runtime.logic = runtime._make_logic()
        with patch.object(runtime.bus, 'read_position', return_value=1472) as read, patch.object(runtime.bus, 'move') as move, patch.object(runtime.bus,'activate_axis',return_value=1472):
            try:
                result = runtime.jog('up', 30, 900)
                read.assert_called_once_with(2)
                self.assertIsNone(runtime.head.yaw)
                self.assertEqual(result.pitch, 1502)
                move.assert_called_once_with(2, 1502, 900, force_arm=False)
                self.assertFalse(runtime._motion_pose_synced)
                self.assertFalse(runtime.running)
            finally:
                runtime.shutdown()

    def test_missing_requested_axis_blocks_manual_arrow_without_motion(self):
        runtime = self.runtime()
        with patch.object(runtime.bus, 'read_position', return_value=None) as read, patch.object(runtime.bus, '_write') as write:
            try:
                with self.assertRaisesRegex(RuntimeError, 'feedback'):
                    runtime.jog('left', 30, 900)
                read.assert_called_once_with(0)
                write.assert_not_called()
            finally:
                runtime.shutdown()

    def test_single_axis_sync_does_not_bypass_both_axis_check_for_full_move(self):
        runtime = self.runtime()
        with patch.object(runtime.bus, 'read_position', return_value=1472):
            runtime.sync_motion_pose(only_axis='pitch')
        with patch.object(runtime.bus, 'read_position', return_value=None), patch.object(runtime.bus, '_write') as write:
            try:
                with self.assertRaisesRegex(RuntimeError, 'feedback'):
                    runtime.move(HeadPose(1500, 1500), 900)
                write.assert_not_called()
            finally:
                runtime.shutdown()


if __name__ == '__main__':
    unittest.main()
