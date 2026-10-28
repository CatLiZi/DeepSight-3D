"""Cancellation before dispatch and late enqueue cleanup, with fake IO only."""
import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from locator_demo.camera import VisionFrameResult, VisionStatus
from locator_demo.head import HeadPose
from locator_demo.settings import SavedSettings, FeatureConfig, VisionProcessingConfig, save_settings
from locator_demo.vision import FaceTarget
import locator_demo.web.app as module
from locator_demo.web.app import DemoRuntime, RuntimeConfig, create_app


class GateLock:
    def __init__(self):
        self.lock = threading.RLock()
        self.entered = threading.Event()

    def __enter__(self):
        self.entered.set()
        self.lock.acquire()
        return self

    def __exit__(self, *args):
        self.lock.release()

    def __getattr__(self, name):
        return getattr(self.lock, name)


class MotionIntentTests(unittest.TestCase):
    def runtime(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        r = DemoRuntime(RuntimeConfig(simulated=True, settings_path=Path(temp.name)/'settings.json'))
        r.head = SimpleNamespace(move_to=Mock(), cancel=Mock(), stop=Mock(), estimated_pose=r.pose)
        r._make_head = Mock(return_value=r.head)
        self.addCleanup(r.shutdown)
        return r

    @staticmethod
    def action(r, name):
        return r.move(HeadPose(1540, 1530), 900) if name == 'move' else r.jog(name, 30, 900)

    def block_feedback(self, r):
        r.config.simulated = False  # Audio/vision/bus objects remain simulated.
        r.config.yaw_id, r.config.pitch_id = 0, 2
        r._motion_pose_synced = False
        entered, release = threading.Event(), threading.Event()

        def read(sid):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test feedback barrier timed out')
            return 1500

        r.bus.read_position = Mock(side_effect=read)
        return entered, release

    def blocked_action(self, r, fn, entered, release, *, fault=False):
        errors = []

        def run():
            try: fn()
            except Exception as exc: errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            start = time.perf_counter()
            r.halt_motion('feedback lost' if fault else '', fault=fault)
            elapsed = time.perf_counter() - start
            self.assertLess(elapsed, .2)  # Stop must not wait for the feedback barrier.
        finally:
            release.set(); worker.join(2)
        self.assertFalse(worker.is_alive())
        return errors

    def test_stop_during_full_pose_feedback_prevents_manual_submission(self):
        r = self.runtime(); entered, release = self.block_feedback(r)
        errors = self.blocked_action(r, lambda: self.action(r, 'move'), entered, release)
        r.head.move_to.assert_not_called()
        self.assertTrue(errors and '取消' in str(errors[0]))
        self.assertFalse(r.running)

    def test_stop_during_single_axis_feedback_prevents_either_manual_axis(self):
        for direction in ('left', 'up'):
            with self.subTest(direction=direction):
                r = self.runtime(); entered, release = self.block_feedback(r)
                errors = self.blocked_action(r, lambda: self.action(r, direction), entered, release)
                r.head.move_to.assert_not_called()
                self.assertTrue(errors and '取消' in str(errors[0]))

    def test_stop_cancels_requests_already_waiting_for_the_runtime_lock(self):
        for name in ('move', 'left', 'up'):
            with self.subTest(name=name):
                r = self.runtime(); gate = GateLock(); r._tick_lock = gate
                errors = []

                def run():
                    try: self.action(r, name)
                    except Exception as exc: errors.append(exc)

                gate.lock.acquire()
                worker = threading.Thread(target=run); worker.start()
                try:
                    self.assertTrue(gate.entered.wait(1))
                    r.halt_motion()
                finally:
                    gate.lock.release(); worker.join(2)
                self.assertFalse(worker.is_alive())
                r.head.move_to.assert_not_called()
                self.assertTrue(errors and '取消' in str(errors[0]))

    def test_cancelled_feedback_cannot_clear_a_newer_fault(self):
        r = self.runtime(); entered, release = self.block_feedback(r)
        errors = self.blocked_action(r, r.sync_motion_pose, entered, release, fault=True)
        self.assertTrue(errors and '取消' in str(errors[0]))
        self.assertEqual(r._motion_fault, 'feedback lost')
        self.assertFalse(r._motion_pose_synced)
        r._make_head.assert_not_called()

    def test_newer_start_intent_invalidates_old_manual_feedback(self):
        r = self.runtime(); entered, release = self.block_feedback(r)
        errors = []

        def run():
            try: self.action(r, 'move')
            except Exception as exc: errors.append(exc)

        worker = threading.Thread(target=run); worker.start()
        try:
            self.assertTrue(entered.wait(1))
            r.begin_start()
        finally:
            release.set(); worker.join(2)
        r.head.move_to.assert_not_called()
        self.assertTrue(errors and '取消' in str(errors[0]))

    def test_fresh_request_after_cancellation_is_not_permanently_latched(self):
        r = self.runtime(); entered, release = self.block_feedback(r)
        self.blocked_action(r, lambda: self.action(r, 'move'), entered, release)
        self.assertEqual(r._motion_fault, '')
        r.head.move_to.reset_mock()
        result = self.action(r, 'move')
        self.assertEqual(result, HeadPose(1540, 1530))
        r.head.move_to.assert_called_once()

    def test_normal_manual_requests_preserve_clamping_and_axis_behaviour(self):
        r = self.runtime()
        self.assertEqual(r.move(HeadPose(2300, 1900), 900), HeadPose(2200, 1800))
        self.assertEqual(r.jog('left', 30, 900), HeadPose(2170, 1800))
        self.assertEqual(r.head.move_to.call_count, 2)
        self.assertEqual(r._motion_fault, '')

    def test_cancelled_start_sync_stays_cancelled_and_cannot_reset_fault(self):
        r = self.runtime(); entered, release = self.block_feedback(r)
        generation = r.begin_start()
        errors = self.blocked_action(r, lambda: r.sync_motion_pose(generation=generation), entered, release, fault=True)
        self.assertTrue(errors and '取消' in str(errors[0]))
        self.assertEqual(r._motion_fault, 'feedback lost')
        with self.assertRaises(RuntimeError):
            r.finish_start(generation)

    def test_late_enqueue_after_stop_is_cancelled_without_committing_target(self):
        r = self.runtime(); r._motion_pose_synced = True
        initial = r.pose
        entered, release = threading.Event(), threading.Event()
        active = [None]

        def enqueue(pose, **kwargs):
            entered.set()
            if not release.wait(2): raise RuntimeError('test dispatch timed out')
            active[0] = pose

        r.head.move_to.side_effect = enqueue
        r.head.cancel.side_effect = lambda: active.__setitem__(0, None)
        errors = self.blocked_action(r, lambda: self.action(r, 'move'), entered, release)
        self.assertIsNone(active[0])
        self.assertEqual(r.pose, initial)
        self.assertTrue(errors and '取消' in str(errors[0]))

    def test_auto_tick_cancellation_is_not_reported_as_a_communication_fault(self):
        r = self.runtime()
        cancelled = getattr(module, 'MotionRequestCancelled', RuntimeError)
        r._tick_locked = Mock(side_effect=cancelled('动作已取消'))
        r.snapshot = Mock(return_value={'running': False})
        self.assertEqual(r.tick(include_frame=False), {'running': False})
        self.assertEqual(r._motion_fault, '')

    def test_stop_inside_visual_computation_does_not_submit_old_auto_goal(self):
        r = self.runtime(); r._motion_pose_synced = True
        r.features = replace(r.features, audio_enabled=False, camera_enabled=True, visual_enabled=True)
        r.vision_processing = replace(r.vision_processing, tracking_strategy='visual_priority')
        r.state_machine = r._make_state_machine()
        target = FaceTarget(270, 151, 370, 251, score=.95, face_id=1)
        frame = VisionFrameResult(target, 640, 360, '', VisionStatus(True, 'fixture', 'ok'),
            targets=[target], control_ready=True, detection_sequence=1,
            binocular={'target_selection': {'locked_person_id': 1}})
        r.vision = SimpleNamespace(read=lambda: frame, stop=Mock(), capture_metrics=lambda: {}, set_audio_guidance=Mock())
        r.running = True
        r.tick(include_frame=False)
        frame = replace(frame, detection_sequence=2)
        original = r.logic.apply_visual_error

        def compute(*args, **kwargs):
            pose = original(*args, **kwargs)
            r.halt_motion()
            return pose

        with patch.object(r.logic, 'apply_visual_error', side_effect=compute), \
             patch('locator_demo.web.app.time.time', return_value=time.time()+.1):
            r.tick(include_frame=False)
        r.head.move_to.assert_not_called()
        self.assertFalse(r.running)
        self.assertEqual(r._motion_fault, '')


class MotionIntentApiTests(unittest.TestCase):
    def app(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        app = create_app(simulated=True, settings_path=Path(temp.name)/'settings.json')
        r = app.state.runtime
        r.start_control_loop = Mock()
        r.head = SimpleNamespace(move_to=Mock(), cancel=Mock(), stop=Mock(), estimated_pose=r.pose)
        r._make_head = Mock(return_value=r.head)
        self.addCleanup(r.shutdown)
        return app, r

    def test_stop_cancels_http_move_and_jog_waiting_for_feedback(self):
        for route, payload in (('/api/head/move', {'yaw': 1540, 'pitch': 1530, 'time_ms': 900}),
                               ('/api/head/jog', {'direction': 'up', 'amount': 30, 'time_ms': 900})):
            with self.subTest(route=route):
                app, r = self.app(); r.config.simulated = False
                r.config.yaw_id, r.config.pitch_id = 0, 2
                entered, release = threading.Event(), threading.Event()

                def read(sid):
                    entered.set()
                    if not release.wait(2): raise RuntimeError('test API feedback timeout')
                    return 1500

                r.bus.read_position = Mock(side_effect=read)
                responses = []
                with TestClient(app) as client:
                    worker = threading.Thread(target=lambda: responses.append(client.post(route, json=payload)))
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        response = client.post('/api/demo/stop')
                        self.assertEqual(response.status_code, 200)
                        self.assertFalse(response.json()['running'])
                    finally:
                        release.set(); worker.join(2)
                    self.assertFalse(worker.is_alive())
                    self.assertTrue(responses and '取消' in (responses[0].json()['error'] or ''))
                    r.head.move_to.assert_not_called()

    def test_http_intent_is_captured_before_the_worker_starts(self):
        requests = (('/api/head/move', {'yaw': 1540, 'pitch': 1530}, 'move'),
                    ('/api/head/jog', {'direction': 'up', 'amount': 30}, 'jog'),
                    ('/api/servo/direct-move', {'servo_id': 2, 'position': 1530}, 'direct_servo_move'))
        for route, payload, name in requests:
            with self.subTest(route=route):
                app, r = self.app(); r._motion_pose_synced = True
                entered, release = threading.Event(), threading.Event()
                original_to_thread = asyncio.to_thread

                async def delayed(fn, *args, **kwargs):
                    if getattr(fn, '__self__', None) is r and getattr(fn, '__name__', '') == name:
                        entered.set()
                        if not await original_to_thread(release.wait, 2):
                            raise RuntimeError('test worker barrier timed out')
                    return await original_to_thread(fn, *args, **kwargs)

                responses = []
                with TestClient(app) as client, patch('locator_demo.web.app.asyncio.to_thread', side_effect=delayed):
                    worker = threading.Thread(target=lambda: responses.append(client.post(route, json=payload)))
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        self.assertEqual(client.post('/api/demo/stop').status_code, 200)
                    finally:
                        release.set(); worker.join(2)
                    self.assertFalse(worker.is_alive())
                    self.assertTrue(responses and '取消' in (responses[0].json()['error'] or ''))
                    r.head.move_to.assert_not_called()
                    self.assertEqual(r.bus.moves, [])


if __name__ == '__main__':
    unittest.main()
