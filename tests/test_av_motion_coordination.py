"""AV switch coordination and smooth pursuit; all actuators are simulated."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from locator_demo.audio import AudioDirection, AudioEstimate
from locator_demo.camera import VisionFrameResult, VisionStatus
from locator_demo.head import HeadControllerLogic, HeadPose, DirectionConfig
from locator_demo.settings import SavedSettings, FeatureConfig, VisionProcessingConfig, save_settings
from locator_demo.vision import FaceTarget
from locator_demo.web.app import DemoRuntime, RuntimeConfig


class CoordinationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        path=Path(self.tmp.name)/'settings.json'
        save_settings(path,SavedSettings(vision=VisionProcessingConfig(tracking_strategy='visual_priority',
            target_offset_x_norm=0,visual_yaw_deadband=.03),features=FeatureConfig()))
        self.runtime=DemoRuntime(RuntimeConfig(simulated=True,settings_path=path))
        self.runtime._motion_pose_synced=True
        self.runtime.running=True
        self.runtime.pose=HeadPose(1600,1550)
        self.head=SimpleNamespace(estimated_pose=HeadPose(1580,1540),is_moving=False,stop=lambda:None)
        self.runtime.head=self.head
        self.runtime.move=Mock()
        self.voice=AudioEstimate(AudioDirection.LEFT,.9,.0002,.5,azimuth_deg=30,
            speech_confidence=.9,doa_confidence=.9,noise_state='speech_like',
            observed_monotonic=time.monotonic(),sample_sequence=1)
        self.runtime.audio.read_estimate=lambda:self.voice
        self.target=FaceTarget(270,100,370,300,face_id=7,score=.9)
        self.frame=VisionFrameResult(self.target,640,400,'',VisionStatus(True,'fixture','ok'),
            targets=[self.target],detection_sequence=1,control_eye='left',
            binocular={'people':[{'person_id':7}], 'target_selection':{'locked_person_id':7}},control_ready=True)
        self.guidance=Mock()
        self.runtime.vision=SimpleNamespace(read=lambda:self.frame,stop=lambda:None,capture_metrics=lambda:{},
            set_audio_guidance=self.guidance)

    def tearDown(self):
        self.runtime.shutdown()
        self.tmp.cleanup()

    def test_centred_new_frames_do_not_retarget_intermediate_planned_pose(self):
        for n in range(1,16):
            self.frame=replace(self.frame,detection_sequence=n)
            self.runtime.tick(include_frame=False)
        self.runtime.move.assert_not_called()

    def test_pitch_tracking_preserves_existing_yaw_goal(self):
        self.target=replace(self.target,y1=230,y2=390)
        for n in (1,2,3):
            self.frame=replace(self.frame,target=self.target,targets=[self.target],detection_sequence=n)
            with patch('locator_demo.web.app.time.time',return_value=100.+n*.1):
                self.runtime.tick(include_frame=False)
        self.assertTrue(self.runtime.move.called)
        self.assertEqual(self.runtime.move.call_args.args[0].yaw,1600)

    def test_audio_only_is_not_blocked_by_preview_face_lock(self):
        self.runtime.features=replace(self.runtime.features,visual_enabled=False)
        for n in (1,2,3):
            self.runtime.tick(include_frame=False)
        self.assertTrue(self.runtime.audio_search_allowed)
        self.assertTrue(self.runtime.move.called)

    def test_disabling_audio_switch_removes_acoustic_guidance(self):
        self.runtime.features=replace(self.runtime.features,audio_enabled=False)
        self.runtime.tick(include_frame=False)
        self.guidance.assert_called_once_with(None)

    def test_both_switches_keep_visual_target_and_sound_guidance(self):
        self.runtime.tick(include_frame=False)
        self.guidance.assert_called_once_with(self.voice)
        self.assertTrue(self.runtime.last_state.target_confirmed)
        self.assertIs(self.runtime.last_target,self.target)

    def test_motor_protection_releases_after_motion_stops(self):
        self.head.is_moving=True
        with patch('locator_demo.web.app.time.time',return_value=100.):
            s=self.runtime.tick(include_frame=False)
        self.assertTrue(s['audio']['motor_suppressed'])
        self.assertEqual(s['audio']['raw_observation']['direction'],'left')
        self.head.is_moving=False
        with patch('locator_demo.web.app.time.time',return_value=100.8):
            s=self.runtime.tick(include_frame=False)
        self.assertFalse(s['audio']['motor_suppressed'])
        self.assertEqual(s['audio']['direction'],'left')
        self.runtime.move.assert_not_called()


class AdaptiveVisualTests(unittest.TestCase):
    def setUp(self):
        from locator_demo.visual_servo import ImageVelocity,adaptive_visual_step
        self.velocity_class=ImageVelocity;self.step=adaptive_visual_step
        self.logic=HeadControllerLogic(direction=DirectionConfig(pitch_up_sign=1))
        self.pose=HeadPose(1500,1500)
        self.limits=dict(yaw_deadband=.03,pitch_deadband=.04,max_yaw_delta=120,max_pitch_delta=40,
            min_yaw_delta=2,min_pitch_delta=0)

    def request(self,x,velocity=0.,y=0.):
        return self.step(self.logic,self.pose,self.pose,(x,y),(x,y),(velocity,0),self.limits)

    def test_large_error_uses_large_step_and_shorter_duration(self):
        far,f=self.request(.6);near,n=self.request(.05)
        self.assertGreaterEqual(abs(far.yaw-1500),90)
        self.assertLess(abs(near.yaw-1500),15)
        self.assertLess(f['move_time_ms'],n['move_time_ms'])

    def test_outward_image_motion_leads_with_bounded_prediction(self):
        stationary,s=self.request(.16);moving,m=self.request(.16,1.2)
        self.assertGreater(abs(moving.yaw-1500),abs(stationary.yaw-1500))
        self.assertLess(m['move_time_ms'],s['move_time_ms'])
        self.assertLessEqual(abs(moving.yaw-1500),120)

    def test_inward_motion_does_not_accelerate_past_centre(self):
        a,ma=self.request(.16);b,mb=self.request(.16,-1.2)
        self.assertEqual(a,b);self.assertEqual(ma['move_time_ms'],mb['move_time_ms'])

    def test_deadband_keeps_goal_and_never_reverses_to_intermediate_pose(self):
        pose,m=self.step(self.logic,HeadPose(1540,1510),HeadPose(1600,1550),(0.,0.),(.01,.01),(2.,2.),self.limits)
        self.assertEqual(pose,HeadPose(1600,1550));self.assertEqual(m['yaw_stage'],'hold')

    def test_crossed_centre_does_not_follow_old_filter(self):
        pose,m=self.step(self.logic,self.pose,self.pose,(.3,0.),(-.05,0.),(0.,0.),self.limits)
        self.assertEqual(pose,self.pose);self.assertEqual(m['yaw_stage'],'hold')

    def test_nonfinite_inputs_hold_and_all_steps_obey_axis_limits(self):
        for x in (float('nan'),float('inf'),-float('inf')):
            self.assertEqual(self.request(x)[0],self.pose)
        for x in (-2,-.6,-.1,0,.1,.6,2):
            pose,_=self.step(self.logic,HeadPose(2190,1795),HeadPose(2190,1795),(x,-x),(x,-x),(0.,0.),self.limits)
            self.assertTrue(800<=pose.yaw<=2200 and 1200<=pose.pitch<=1800)

    def test_duplicate_reset_stale_and_eye_switch_do_not_invent_velocity(self):
        v=self.velocity_class()
        self.assertEqual(v.update((0,0),identity='a',sequence=1,observed_at=1.),(0,0))
        self.assertEqual(v.update((.1,0),identity='a',sequence=1,observed_at=1.1),(0,0))
        self.assertEqual(v.update((.2,0),identity='b',sequence=2,observed_at=1.2),(0,0))
        self.assertEqual(v.update((.3,0),identity='b',sequence=1,observed_at=1.3),(0,0))
        self.assertEqual(v.update((.4,0),identity='b',sequence=2,observed_at=2.),(0,0))

    def test_consistent_motion_is_estimated_but_single_detection_jump_is_rejected(self):
        v=self.velocity_class()
        for i in range(3):
            result=v.update((i*.05,0),identity=7,sequence=i+1,observed_at=1+i*.1)
        self.assertAlmostEqual(result[0],.5)
        self.assertEqual(v.update((1.8,0),identity=7,sequence=4,observed_at=1.3),(0,0))

    def test_direction_calibration_flip_preserves_visual_effect_and_flips_audio(self):
        old=HeadControllerLogic(direction=DirectionConfig(yaw_left_sign=-1,pitch_up_sign=1))
        new=HeadControllerLogic(direction=DirectionConfig(yaw_left_sign=1,pitch_up_sign=1))
        for x in (-.8,-.2,0,.2,.8):
            self.assertEqual(old.apply_visual_error(self.pose,-x,.1),new.apply_visual_error(self.pose,x,.1))
        self.assertLess(new.apply_audio_azimuth(self.pose,-35).yaw,self.pose.yaw)
        self.assertGreater(old.apply_audio_azimuth(self.pose,-35).yaw,self.pose.yaw)


if __name__=='__main__':
    unittest.main()
