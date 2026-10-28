"""Decision policy, direction reference and closed-loop steps; no hardware I/O."""
from copy import deepcopy
from dataclasses import replace
import math
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import numpy as np

from locator_demo.bearing import ScreenCenteredDirectionFrame,annotate_eye_bearings
from locator_demo.binocular import BinocularFusion
from locator_demo.head import HeadControllerLogic,HeadPose,DirectionConfig
from locator_demo.settings import SavedSettings,VisionProcessingConfig,FeatureConfig,save_settings,load_settings
from locator_demo.stereo_calibration import StereoRectifier,make_alignment_preview_profile
from locator_demo.visual_priority import VisualPrioritySelector,progressive_visual_step
from locator_demo.vision import FaceTarget,normalized_center_error
from locator_demo.camera import VisionFrameResult,VisionStatus
from locator_demo.web.app import DemoRuntime,RuntimeConfig,VisionProcessingConfigRequest
from test_av_guidance import voice
from test_stereo_calibration import alignment_source


def face(identity,angle,*,width=90,score=.9,interval=None):
    # Generic wide-view candidate fixture; the actual rig FOV is tested below.
    x=320-120*math.tan(math.radians(angle))
    bearing=dict(frame='screen_centered_optical',azimuth_left_deg=angle,
        azimuth_interval_deg=interval or [angle-4,angle+4],sigma_deg=6.,confidence=.9,
        source='single_eye_rectified_ray',virtual_u_px=x,virtual_v_px=180.)
    return FaceTarget(x-width/2,90,x+width/2,270,face_id=identity,score=score,
                      source_eye='left',tracking_source='scrfd',bearing=bearing)


class SectorSelectionTests(unittest.TestCase):
    def run_sound(self,selector,people,angle,*,start=1.,count=15,sequence=None):
        for n in range(count):
            now=start+n*.1
            selected=selector.update(people,now=now,size=(640,360),
                audio=voice(angle,now,n if sequence is None else sequence))
        return selected

    def test_silent_scene_prefers_larger_reliable_face_over_higher_detector_score(self):
        selector=VisualPrioritySelector();people={1:[face(1,-20,width=150,score=.5)],2:[face(2,20,width=40,score=.99)]}
        self.assertEqual(selector.update(people,now=1.,size=(640,360)),1)

    def test_same_acoustic_direction_prefers_larger_face(self):
        people={1:[face(1,-55,width=110)],2:[face(2,-67,width=40)]}
        selector=VisualPrioritySelector()
        self.assertEqual(self.run_sound(selector,people,-67),1)

    def test_search_uses_whole_face_interval_not_just_centre(self):
        people={1:[face(1,25,width=140)],2:[face(2,-48,width=80,interval=[-55,-41])]}
        selector=VisualPrioritySelector()
        self.assertEqual(self.run_sound(selector,people,-67),2)
        row=next(c for c in selector.metrics['candidates'] if c['person_id']==2)
        self.assertTrue(row['in_sector']);self.assertEqual(row['angular_gap_deg'],12)
        self.assertGreater(row['center_gap_deg'],15)

    def test_conflicting_sound_without_visible_face_never_replaces_visual_lock(self):
        selector=VisualPrioritySelector();people={1:[face(1,25,width=150)],2:[face(2,10,width=50)]}
        self.assertEqual(self.run_sound(selector,people,-67),1)
        self.assertEqual(selector.metrics['audio_reason'],'visual_audio_conflict')

    def test_audio_sector_requires_visual_confirmation_before_selecting_a_face(self):
        selector=VisualPrioritySelector();people={1:[face(1,25,width=140)],2:[face(2,-40,width=70)]}
        self.assertEqual(self.run_sound(selector,people,-40,count=2),1)
        self.assertEqual(self.run_sound(selector,people,-40,start=1.2),2)

    def test_repeated_audio_cannot_confirm_sector_candidate(self):
        selector=VisualPrioritySelector();people={1:[face(1,25,width=140)],2:[face(2,-40,width=70)]}
        self.assertEqual(self.run_sound(selector,people,-40,sequence=8),1)

    def test_stop_speaking_keeps_current_visible_face(self):
        selector=VisualPrioritySelector();people={1:[face(1,25,width=100)],2:[face(2,-40,width=96)]}
        self.assertEqual(self.run_sound(selector,people,-40),2)
        self.assertEqual(selector.update(people,now=2.5,size=(640,360)),2)

    def test_sustained_much_larger_face_can_take_over_without_sound(self):
        selector=VisualPrioritySelector();a=face(1,20,width=60);b=face(2,-20,width=130)
        selector.update({1:[a]},now=1.,size=(640,360))
        for now in (1.2,1.4,1.5):self.assertEqual(selector.update({1:[a],2:[b]},now=now,size=(640,360)),1)
        self.assertEqual(selector.update({1:[a],2:[b]},now=1.7,size=(640,360)),2)

    def test_small_area_changes_do_not_cause_switching(self):
        selector=VisualPrioritySelector();a=face(1,20,width=100);b=face(2,-20,width=110)
        selector.update({1:[a]},now=1.,size=(640,360))
        for now in (1.2,1.7,2.5):self.assertEqual(selector.update({1:[a],2:[b]},now=now,size=(640,360)),1)

    def test_two_eye_observations_are_not_added_as_double_size(self):
        selector=VisualPrioritySelector();a=face(1,20,width=70);b=face(2,-20,width=100)
        self.assertEqual(selector.update({1:[a,replace(a,source_eye='right')],2:[b]},now=1.,size=(640,360)),2)

    def test_large_low_confidence_or_nonfinite_box_is_not_a_near_person(self):
        selector=VisualPrioritySelector();a=face(1,0,width=200,score=.2);b=face(2,20,width=50)
        invalid=replace(a,face_id=3,x1=float('nan'))
        self.assertEqual(selector.update({1:[a],2:[b],3:[invalid]},now=1.,size=(640,360)),2)

    def test_brief_visual_loss_returns_no_old_coordinate_or_bystander(self):
        selector=VisualPrioritySelector();a=face(1,20,width=120);b=face(2,-20,width=50)
        selector.update({1:[a],2:[b]},now=1.,size=(640,360))
        self.assertIsNone(selector.update({2:[b]},now=1.2,size=(640,360)))
        self.assertEqual(selector.update({1:[a],2:[b]},now=1.3,size=(640,360)),1)

    def test_missing_visual_target_cannot_be_created_from_sound(self):
        selector=VisualPrioritySelector()
        self.assertIsNone(self.run_sound(selector,{},25))

    def test_area_tie_uses_angular_proximity(self):
        selector=VisualPrioritySelector();people={1:[face(1,-45,width=100)],2:[face(2,-60,width=96)]}
        self.assertEqual(self.run_sound(selector,people,-60),2)

    def test_search_half_width_is_configurable(self):
        selector=VisualPrioritySelector(sector_half_width_deg=8)
        people={1:[face(1,25,width=140)],2:[face(2,-48,width=80,interval=[-55,-41])]}
        self.assertEqual(self.run_sound(selector,people,-67),1)


class ScreenReferenceTests(unittest.TestCase):
    def setUp(self):
        source=alignment_source();source['K_left'][0][2]+=14.;source['K_right'][0][2]+=14.
        self.rectifier=StereoRectifier(make_alignment_preview_profile(source),allow_alignment_only=True)
        self.direction=ScreenCenteredDirectionFrame(self.rectifier);self.size=self.rectifier.crop[2:]

    def test_centre_of_display_is_zero_angle_and_zero_control_error(self):
        w,h=self.size;t=FaceTarget(w/2-20,h/2-30,w/2+20,h/2+30,score=.9)
        result=annotate_eye_bearings({'left':[t]},self.direction,self.size)['left'][0]
        self.assertAlmostEqual(result.bearing['azimuth_left_deg'],0.,places=8)
        self.assertAlmostEqual(result.bearing['elevation_up_deg'],0.,places=8)
        np.testing.assert_allclose(normalized_center_error(result,frame_width=w,frame_height=h),(0,0),atol=1e-10)

    def test_left_is_positive_right_is_negative(self):
        w,h=self.size;rays,_=self.direction.bearing_rays('left',[[w/2-50,h/2],[w/2+50,h/2]],self.size)
        self.assertLess(rays[0][0],0);self.assertGreater(rays[1][0],0)

    def test_zero_degree_overlay_projects_to_image_centre(self):
        p=np.asarray(self.direction.direction_projection(self.size)['left']);point=p@np.array([0,0,1.])
        np.testing.assert_allclose(point[:2]/point[2],np.array(self.size)/2,atol=1e-8)

    def test_view_range_is_computed_from_current_crop(self):
        bounds=self.direction.direction_view(self.size)['visible_azimuth_interval_deg']
        self.assertLess(bounds[0],0);self.assertGreater(bounds[1],0);self.assertLess(bounds[1]-bounds[0],180)

    def test_direction_reference_does_not_enable_depth(self):
        self.assertFalse(self.direction.metric_ready);self.assertIsNone(self.direction.q)
        with self.assertRaises(ValueError):self.direction.projection_for_size(self.size)

    def test_steering_stays_in_screen_reference_even_if_distance_is_later_available(self):
        w,h=self.size;t=face(1,0)
        t=replace(t,bearing={**t.bearing,'virtual_u_px':w/2,'virtual_v_px':h/2},
                  stereo_position={'frame':'rectified_camera_midpoint','virtual_u_px':w/2+80,'virtual_v_px':h/2})
        self.assertEqual(normalized_center_error(t,frame_width=w,frame_height=h),(0.,0.))


class ProgressiveStepTests(unittest.TestCase):
    def setUp(self):
        self.logic=HeadControllerLogic(direction=DirectionConfig(pitch_up_sign=1))
        self.limits=dict(yaw_deadband=.03,pitch_deadband=.03,max_yaw_delta=30,max_pitch_delta=30,min_yaw_delta=2,min_pitch_delta=0)

    def test_coarse_medium_fine_caps_shrink(self):
        steps=[]
        for error in (.6,.15,.05):
            pose,metrics=progressive_visual_step(self.logic,HeadPose(1500,1500),error,0,self.limits)
            steps.append(abs(pose.yaw-1500))
        self.assertEqual(steps,[30,12,4])

    def test_raw_centre_crossing_brakes_filter_lag(self):
        pose,metrics=progressive_visual_step(self.logic,HeadPose(1500,1500),.2,0,self.limits,measured=(-.04,0))
        self.assertEqual(pose,HeadPose(1500,1500));self.assertEqual(metrics['yaw_stage'],'hold')

    def test_raw_deadband_stops_despite_old_filter_value(self):
        pose,_=progressive_visual_step(self.logic,HeadPose(1500,1500),.2,0,self.limits,measured=(.01,0))
        self.assertEqual(pose,HeadPose(1500,1500))

    def test_closed_loop_reduces_error_without_a_distance(self):
        pose=HeadPose(1500,1500);goal=HeadPose(1350,1550);errors=[]
        for _ in range(100):
            error=((goal.yaw-pose.yaw)/200,(pose.pitch-goal.pitch)/200)
            errors.append(np.linalg.norm(error))
            pose,metrics=progressive_visual_step(self.logic,pose,*error,self.limits)
        self.assertLess(errors[-1],.05);self.assertTrue(all(b<=a+1e-9 for a,b in zip(errors,errors[1:])))
        self.assertFalse(metrics['metric_distance_used'])

    def test_visual_corrections_follow_the_same_directions_as_manual_jogs(self):
        current=HeadPose(1500,1500)
        for x,y,direction,axis in ((-.4,0,'left','yaw'),(.4,0,'right','yaw'),(0,-.4,'up','pitch'),(0,.4,'down','pitch')):
            pose,_=progressive_visual_step(self.logic,current,x,y,self.limits)
            manual=self.logic.jog(current,direction)
            self.assertGreater((getattr(pose,axis)-getattr(current,axis))*(getattr(manual,axis)-getattr(current,axis)),0)

    def test_axis_range_still_limits_every_step(self):
        pose,_=progressive_visual_step(self.logic,HeadPose(1200,1800),1.,-1.,self.limits)
        self.assertGreaterEqual(pose.yaw,1200);self.assertLessEqual(pose.pitch,1800)


class RuntimePriorityTests(unittest.TestCase):
    def test_settings_preserve_sector_and_new_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'settings.json'
            save_settings(path,SavedSettings(vision=VisionProcessingConfig(tracking_strategy='visual_priority',audio_sector_half_width_deg=12.)))
            actual=load_settings(path).vision
            self.assertEqual(actual.tracking_strategy,'visual_priority');self.assertEqual(actual.audio_sector_half_width_deg,12.)

    def test_new_policy_is_passed_to_worker_without_enabling_motion(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime=DemoRuntime(RuntimeConfig(simulated=True,settings_path=Path(tmp)/'settings.json'))
            runtime.vision.set_guided_mode=Mock()
            try:
                runtime.update_vision_processing_config(VisionProcessingConfigRequest(tracking_strategy='visual_priority',audio_sector_half_width_deg=12.))
                runtime.vision.set_guided_mode.assert_called_once_with(True,visual_priority=True,sector_half_width_deg=12.)
                self.assertFalse(runtime.running)
            finally:runtime.stop()

    def test_same_detection_frame_cannot_accumulate_multiple_visual_moves(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'settings.json';save_settings(path,SavedSettings(vision=VisionProcessingConfig(tracking_strategy='visual_priority'),
                features=FeatureConfig(audio_enabled=False,visual_enabled=True,camera_enabled=True)))
            runtime=DemoRuntime(RuntimeConfig(simulated=True,settings_path=path));target=face(1,25,width=120)
            frame=VisionFrameResult(target,640,360,'',VisionStatus(True,'fixture','ok'),targets=[target],
                binocular={'people':[{'person_id':1}],'target_selection':{'locked_person_id':1}},control_ready=True,detection_sequence=42)
            runtime.vision=SimpleNamespace(read=lambda:frame,stop=lambda:None,capture_metrics=lambda:{},set_audio_guidance=Mock())
            runtime.move=Mock();runtime.running=True
            try:
                runtime.tick(include_frame=False);runtime.tick(include_frame=False)
                self.assertEqual(runtime.move.call_count,0)  # First fresh frame only confirms a candidate direction.
                frame = replace(frame, detection_sequence=43)
                with patch('locator_demo.web.app.time.time', return_value=time.time()+.1):
                    runtime.tick(include_frame=False);runtime.tick(include_frame=False)
                self.assertEqual(runtime.move.call_count,1)
                self.assertEqual(runtime._visual_approach['mode'],'progressive_image_feedback')
            finally:runtime.stop()


if __name__=='__main__':unittest.main()
