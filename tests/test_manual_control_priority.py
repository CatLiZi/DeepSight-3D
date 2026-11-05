"""Manual motion survives perception ticks; real planner, fake serial hardware."""
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from locator_demo.head import AxisConfig, HeadPose
from locator_demo.motion import SmoothHeadHardware
from locator_demo.settings import FeatureConfig
from locator_demo.web.app import DemoRuntime, FeatureConfigRequest, create_app
from fastapi.testclient import TestClient
import test_dominant_sound as fixture


class ManualPriorityTests(unittest.TestCase):
    setUp_fixture = fixture.CoreRuntimeTests.setUp_fixture
    tearDown = fixture.CoreRuntimeTests.tearDown

    def setUp(self):
        fixture.CoreRuntimeTests.setUp(self)
        r = self.runtime
        r.features = FeatureConfig(primary_mode='audio_primary')
        r.direction = replace(r.direction, pitch_up_sign=1)
        r.axis_limits = replace(r.axis_limits, yaw_min=1500, yaw_center=2000, yaw_max=2500,
                               pitch_min=1255, pitch_center=1550, pitch_max=1855)
        r.logic = r._make_logic()
        r.config.yaw_id = 0
        r.pose = HeadPose(1832, 1575)
        self.stream_commands = []
        driver = SimpleNamespace(yaw=AxisConfig(servo_id=0, center=2000, minimum=1500, maximum=2500),
                                 pitch=AxisConfig(servo_id=2, center=1550, minimum=1255, maximum=1855),
                                 move_to=lambda pose, **kw: self.stream_commands.append(pose))
        self.head = SmoothHeadHardware(driver, initial=r.pose, config=r.control,
                                       clock=lambda: self.now, autostart=False)
        self.head.cancel = Mock(wraps=self.head.cancel)
        r.head = self.head
        r.move = DemoRuntime.move.__get__(r, DemoRuntime)
        self.frame = replace(self.frame, target=None, targets=[], binocular={'people':[]}, control_ready=False)
        self.perception()

    def perception(self, angle=None):
        self.n += 1
        self.now += .05
        self.voice = fixture.observation(0. if angle is None else angle, self.now, self.n)
        if angle is None:
            self.voice = replace(self.voice, direction=fixture.AudioDirection.UNKNOWN, confidence=0.,
                speech_confidence=.01, doa_confidence=0., noise_state='no_voice_activity')
        with patch('locator_demo.web.app.time.time', return_value=100.+self.now), patch('time.perf_counter', return_value=self.now):
            return self.runtime.tick(include_frame=False)

    def manual_goal(self, goal):
        generation = self.runtime.halt_motion()
        self.runtime.move(goal, 1500, generation=generation)
        self.head.cancel.reset_mock()

    def finish(self, *, angle=None):
        for _ in range(100):
            self.perception(angle)
            self.head.step(self.now)

    def test_return_to_saved_center_survives_the_next_context_reset(self):
        target = self.runtime.logic.center_pose
        self.manual_goal(target)
        self.perception()
        self.assertEqual(self.runtime.pose, target)
        self.assertEqual(self.head.metrics()['goal_pose'], {'yaw':2000,'pitch':1550})
        self.head.cancel.assert_not_called()
        self.finish()
        self.assertEqual(self.stream_commands[-1], target)
        self.assertFalse(self.runtime.running)

    def test_new_sound_during_paused_manual_motion_cannot_cancel_it(self):
        target = HeadPose(2150, 1600)
        self.manual_goal(target)
        self.finish(angle=35.)
        self.assertTrue(self.runtime._dominant_sound.snapshot()['locked'])
        self.assertFalse(self.runtime._core_motion_authorized)
        self.head.cancel.assert_not_called()
        self.assertEqual(self.stream_commands[-1], target)

    def test_losing_preview_face_does_not_withdraw_manual_goal(self):
        target = HeadPose(1900, 1550)
        self.runtime.halt_motion()
        self.perception()
        # No context reset this time: isolate the visual-owner-loss branch.
        self.runtime.move(target, 1500, generation=self.runtime.motion_generation())
        self.runtime._audio_primary_owner = 'visual'
        self.head.cancel.reset_mock()
        self.finish()
        self.head.cancel.assert_not_called()
        self.assertEqual(self.stream_commands[-1], target)

    def test_manual_arrow_retains_its_target_through_audio_ticks(self):
        generation = self.runtime.halt_motion()
        goal = self.runtime.jog('up', 40, 900, generation=generation)
        self.head.cancel.reset_mock()
        self.finish(angle=-35.)
        self.assertEqual(goal, HeadPose(1832, 1615))
        self.assertTrue(self.stream_commands)
        self.assertEqual(self.stream_commands[-1], goal)
        self.head.cancel.assert_not_called()

    def test_stop_still_cancels_manual_trajectory(self):
        self.manual_goal(HeadPose(2200, 1550))
        for _ in range(6):
            self.perception()
            self.head.step(self.now)
        self.runtime.halt_motion()
        count = len(self.stream_commands)
        self.finish(angle=35.)
        self.assertEqual(len(self.stream_commands), count)
        self.assertFalse(self.head.is_moving)

    def test_mode_change_still_revokes_a_manual_trajectory(self):
        self.manual_goal(HeadPose(2200, 1550))
        self.runtime.update_feature_config(FeatureConfigRequest(primary_mode='visual_priority'))
        count = len(self.stream_commands)
        self.finish()
        self.assertEqual(len(self.stream_commands), count)
        self.assertFalse(self.runtime.running)

    def test_visual_primary_has_no_authority_to_cancel_paused_manual_goal(self):
        self.runtime.features = FeatureConfig(primary_mode='visual_priority')
        target = HeadPose(2000, 1550)
        self.manual_goal(target)
        self.finish(angle=35.)
        self.assertEqual(self.stream_commands[-1], target)

    def test_old_tick_generation_cannot_cancel_a_newer_intent(self):
        self.manual_goal(HeadPose(2200, 1550))
        old = self.runtime.motion_generation()
        self.runtime.begin_start()
        self.runtime.running = True
        self.assertFalse(self.runtime._cancel_automatic_trajectory(old, 'stale_tick'))
        self.head.cancel.assert_not_called()
        self.assertEqual(self.head.metrics()['goal_pose'], {'yaw':2200,'pitch':1550})

    def test_feedback_fault_still_cancels_and_latches_stop(self):
        self.manual_goal(HeadPose(2200, 1550))
        self.runtime.halt_motion('fixture invalid feedback', fault=True)
        count = len(self.stream_commands)
        self.finish(angle=35.)
        self.assertEqual(len(self.stream_commands), count)
        self.assertTrue(self.runtime._motion_fault)
        self.assertFalse(self.runtime.running)


class CenterApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app(simulated=True, settings_path=Path(self.tmp.name)/'settings.json')
        self.r = self.app.state.runtime
        self.addCleanup(self.r.shutdown)
        self.r.axis_limits = replace(self.r.axis_limits, yaw_min=1500, yaw_center=2000, yaw_max=2500,
                                    pitch_min=1255, pitch_center=1550, pitch_max=1855)
        self.r.config.yaw_id = 0
        self.r.logic = self.r._make_logic()
        self.r.pose = HeadPose(1832, 1575)
        self.r.bus.positions = {0:1832, 2:1575}
        self.r.head = self.r._make_head()
        self.r._motion_pose_synced = True

    def test_center_uses_saved_server_values_and_pauses_automatic_tracking(self):
        self.r.running = True
        self.r.sync_motion_pose = Mock(wraps=self.r.sync_motion_pose)
        with TestClient(self.app) as client:
            response = client.post('/api/head/center', json={'time_ms':1500,'yaw':1500,'pitch':1500})
        self.assertEqual(response.status_code,200)
        self.assertIsNone(response.json()['error'])
        self.assertEqual(response.json()['target'], {'yaw':2000,'pitch':1550})
        self.assertEqual(response.json()['status'],'submitted')
        self.assertFalse(self.r.running)
        self.r.sync_motion_pose.assert_called_once()
        self.assertEqual(self.r.bus.positions, {0:2000,2:1550})

    def test_center_missing_feedback_does_not_fall_back_to_assumed_position(self):
        self.r.sync_motion_pose = Mock(side_effect=RuntimeError('fixture feedback unavailable'))
        with TestClient(self.app) as client:
            result = client.post('/api/head/center').json()
        self.assertEqual(result['status'],'failed')
        self.assertIn('feedback unavailable',result['error'])
        self.assertEqual(self.r.bus.moves, [])

    def test_stop_while_center_waits_for_feedback_cancels_the_late_request(self):
        self.r.sync_motion_pose = Mock(side_effect=lambda **kw:self.r.halt_motion())
        with TestClient(self.app) as client:
            result = client.post('/api/head/center').json()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(self.r.bus.moves, [])

    def test_foreign_origin_cannot_center_the_head(self):
        with TestClient(self.app) as client:
            response = client.post('/api/head/center', headers={'Origin':'https://outside.example'})
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.r.bus.moves, [])

    def test_invalid_duration_is_rejected_before_motion(self):
        with TestClient(self.app) as client:
            for duration in (-1,0,20000):
                self.assertEqual(client.post('/api/head/center',json={'time_ms':duration}).status_code,422)
        self.assertEqual(self.r.bus.moves, [])


if __name__ == '__main__': unittest.main()
