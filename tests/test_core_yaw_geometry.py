"""Known-angle acoustic goals and staged delivery; mocked actuators only."""
from dataclasses import replace
import math
import unittest

from locator_demo.audio import AudioDirection
from locator_demo.array_pose import FeedbackAlignedYaw
from locator_demo.dominant_sound import DominantSoundLock
from locator_demo.head import HeadPose
from locator_demo.settings import AxisLimitsConfig as AxisLimits
import test_dominant_sound as fixture

SCALE = 2000. / 270.
CENTER = 1952


class CoreYawGeometryTests(unittest.TestCase):
    setUp_fixture = fixture.CoreRuntimeTests.setUp_fixture
    tearDown = fixture.CoreRuntimeTests.tearDown
    tick_voice = fixture.CoreRuntimeTests.tick_voice

    def setUp(self):
        fixture.CoreRuntimeTests.setUp(self)
        r = self.runtime
        r.audio_capture = replace(r.audio_capture, body_yaw_units_per_degree=SCALE,
                                  body_yaw_zero_position=CENTER)
        r.axis_limits = AxisLimits(yaw_min=1404,yaw_center=CENTER,yaw_max=2500,
                                  pitch_min=1255,pitch_center=1555,pitch_max=1855)
        r.control = replace(r.control,control_profile='fast')
        r.logic = r._make_logic()
        r.pose = self.head.estimated_pose = HeadPose(CENTER,1555)

    def acquire_angle(self, angle):
        # Simulate waiting for a current actuator request. This does not move.
        self.head.is_moving = True
        for _ in range(14):state = self.tick_voice(angle)
        self.assertTrue(state['audio']['core_source']['locked'])
        self.assertFalse(self.commands)
        return state['audio']['core_source']

    def finish_in_silence(self):
        self.head.is_moving = False
        for _ in range(12):
            state=self.tick_voice(unknown=True)
            self.head.estimated_pose=self.runtime.pose
        return state

    def test_known_angles_use_physical_scale_both_sides(self):
        for angle in (-80.,-60.,-45.,-30.,-15.,15.,30.,45.,60.,80.):
            with self.subTest(angle=angle):
                self.runtime._dominant_context=None
                core=self.acquire_angle(angle)
                expected=round(max(1404,min(2500,CENTER+SCALE*angle)))
                self.assertEqual(core['goal_yaw'],expected)

    def test_final_goal_is_independent_of_speed_profile(self):
        goals=[]
        for profile in ('fast','stable'):
            self.runtime.control=replace(self.runtime.control,control_profile=profile)
            goals.append(self.acquire_angle(45.)['goal_yaw'])
        self.assertEqual(goals,[2285,2285])

    def test_large_short_utterance_finishes_in_bounded_steps(self):
        core=self.acquire_angle(60.)
        state=self.finish_in_silence()
        self.assertEqual(core['goal_yaw'],2396)
        self.assertEqual(self.commands[-1].yaw,2396)
        self.assertGreater(len(self.commands),1)
        cap=self.runtime._profile_params()['audio_seek_max_step']
        previous=CENTER
        for pose in self.commands:
            self.assertLessEqual(abs(pose.yaw-previous),cap)
            self.assertEqual(pose.pitch,1555)
            previous=pose.yaw
        self.assertEqual(state['audio']['direction'],'unknown')
        self.assertEqual(state['audio']['core_source']['goal_yaw'],2396)

    def test_slow_profile_does_not_shorten_a_45_degree_goal(self):
        self.runtime.control=replace(self.runtime.control,control_profile='stable')
        self.acquire_angle(-45.)
        self.finish_in_silence()
        self.assertEqual(self.commands[-1].yaw,1619)
        self.assertGreater(len(self.commands),1)
        self.assertTrue(all(abs(b-a)<=160 for a,b in zip([CENTER]+[p.yaw for p in self.commands],
                                                        [p.yaw for p in self.commands])))

    def test_reversing_servo_sign_reverses_target_not_angle_size(self):
        self.runtime.direction=replace(self.runtime.direction,yaw_left_sign=-1)
        self.runtime.logic=self.runtime._make_logic()
        self.assertEqual(self.acquire_angle(30.)['goal_yaw'],1730)

    def test_completed_goal_does_not_repeat_relative_angle(self):
        self.acquire_angle(45.)
        self.finish_in_silence()
        n=len(self.commands)
        for _ in range(40):self.tick_voice(unknown=True)
        self.assertEqual(len(self.commands),n)
        self.assertEqual(self.runtime.pose.yaw,2285)

    def test_software_limit_is_reported_separately_from_step_limit(self):
        core=self.acquire_angle(80.)
        self.assertEqual(core['goal_yaw'],2500)
        self.assertTrue(core['goal_limited'])
        state=self.finish_in_silence()
        self.assertEqual(self.commands[-1].yaw,2500)
        self.assertEqual(state['audio']['steering']['stop_reason'],'yaw_limit')

    def test_unfinished_segment_does_not_claim_final_limit_reached(self):
        self.acquire_angle(80.)
        self.head.is_moving=False
        self.tick_voice(unknown=True)
        # A delayed actuator estimate remains at the previous position.
        state=self.tick_voice(unknown=True)
        self.assertLess(self.commands[-1].yaw,2500)
        self.assertEqual(state['audio']['steering']['stop_reason'],'finishing_core_motion')

    def test_missing_calibration_cannot_fall_back_to_tracking_gain(self):
        self.runtime.audio_capture=replace(self.runtime.audio_capture,body_yaw_units_per_degree=None)
        for _ in range(14):state=self.tick_voice(30.)
        self.assertFalse(self.commands)
        core=state['audio']['core_source']
        self.assertFalse(core['locked'])
        self.assertEqual(core['reason'],'yaw_angle_calibration_missing')

    def test_scale_change_cancels_old_target_and_needs_new_evidence(self):
        self.acquire_angle(60.)
        self.runtime.audio_capture=replace(self.runtime.audio_capture,body_yaw_units_per_degree=10.)
        self.head.cancel.reset_mock()
        state=self.tick_voice(unknown=True)
        self.head.cancel.assert_called_once()
        self.assertFalse(state['audio']['core_source']['locked'])
        self.assertFalse(state['audio']['core_source']['motion_authorized'])
        self.assertFalse(self.commands)

    def test_stop_between_segments_clears_remaining_intent(self):
        self.acquire_angle(60.)
        self.head.is_moving=False
        self.tick_voice(unknown=True)
        self.assertEqual(len(self.commands),1)
        self.head.estimated_pose=self.runtime.pose
        self.runtime.halt_motion()
        self.tick_voice(unknown=True)
        self.runtime.running=True
        self.runtime.begin_start()
        state=self.tick_voice(unknown=True)
        self.assertEqual(len(self.commands),1)
        self.assertFalse(state['audio']['core_source']['locked'])

    def test_visual_takeover_prevents_remaining_segments(self):
        self.acquire_angle(60.)
        self.head.is_moving=False
        self.tick_voice(unknown=True)
        self.head.estimated_pose=self.runtime.pose
        self.runtime.features=replace(self.runtime.features,visual_enabled=True)
        state=self.tick_voice(unknown=True)
        self.assertEqual(len(self.commands),1)
        self.assertFalse(state['audio']['core_source']['motion_authorized'])

    def test_audio_only_yaw_goal_does_not_require_level_pitch(self):
        self.runtime.pose=self.head.estimated_pose=HeadPose(CENTER,1604)
        core=self.acquire_angle(30.)
        self.assertEqual(core['goal_yaw'],2174)
        self.finish_in_silence()
        self.assertTrue(all(p.pitch==1604 for p in self.commands))

    def test_head_body_transform_and_acoustic_target_use_same_scale(self):
        tracker=FeedbackAlignedYaw()
        yaw=round(CENTER+SCALE*30.)
        pose=tracker.resolve(now=100.,wall_now=1000.,observed=99.99,window_s=.192,
            yaw=dict(position=yaw,ts=999.9,valid=True),pitch=dict(position=1555,ts=999.9,valid=True),
            estimated_yaw=yaw,moving=False,scale=SCALE,zero=CENTER,sign=1,
            yaw_limits=(1404,2500),pitch_center=1555,session='offline-test')
        self.runtime.pose=self.head.estimated_pose=HeadPose(yaw,1555)
        head_relative=60.-pose['head_yaw_deg']
        self.assertEqual(self.acquire_angle(head_relative)['goal_yaw'],2396)


class CoreGeometryInputTests(unittest.TestCase):
    def call(self, lock, *, scale=SCALE, now=10., yaw=CENTER, angle=60., seq=1, sign=1):
        return lock.update(fixture.observation(angle,now,seq),now=now,yaw=yaw,
            units_per_degree=scale,left_sign=sign,yaw_min=1404,yaw_max=2500,command_window=340)

    def test_missing_or_invalid_scale_clears_previous_goal(self):
        for invalid in (None,0.,-1.,True,float('nan'),float('inf'),31.):
            with self.subTest(scale=invalid):
                lock=DominantSoundLock()
                for i in range(14):state=self.call(lock,now=10.+i*.05,seq=i+1)
                self.assertTrue(state['locked'])
                state=self.call(lock,scale=invalid,now=11.,seq=20)
                self.assertFalse(state['locked'])
                self.assertIsNone(state['goal_yaw'])

    def test_geometry_change_does_not_reuse_old_evidence(self):
        lock=DominantSoundLock()
        for i in range(14):self.call(lock,now=10.+i*.05,seq=i+1)
        state=self.call(lock,scale=10.,now=11.,seq=20)
        self.assertFalse(state['locked'])

    def test_current_pose_rebases_a_fixed_source_with_physical_scale(self):
        lock=DominantSoundLock();goal=2285.
        for i in range(45):
            yaw=CENTER+i*5.
            state=self.call(lock,angle=(goal-yaw)/SCALE,yaw=yaw,now=10.+i*.05,seq=i+1)
        self.assertEqual(state['goal_yaw'],round(goal))
        self.assertEqual(len(state['candidates']),1)
        self.assertAlmostEqual(state['relative_azimuth_deg'],(goal-yaw)/SCALE,places=5)

    def test_raw_goal_does_not_get_cut_to_current_command_window(self):
        lock=DominantSoundLock()
        for i in range(14):state=self.call(lock,now=10.+i*.05,seq=i+1)
        self.assertEqual(state['goal_yaw'],2396)
        self.assertFalse(state['goal_limited'])


if __name__=='__main__':unittest.main()
