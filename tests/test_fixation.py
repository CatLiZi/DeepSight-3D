"""Binocular fixation geometry and failure cases, with no hardware IO."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import math, unittest, tempfile, time
import numpy as np

from locator_demo.fixation import make_fixation,matched_fixation,fixation_overlay
from locator_demo.vision import FaceTarget,normalized_center_error
from locator_demo.bearing import AlignedBearingMatcher,ScreenCenteredDirectionFrame
from locator_demo.binocular import BinocularFusion
from locator_demo.visual_priority import VisualPrioritySelector
from locator_demo.visual_servo import ImageVelocity
from test_av_guidance import aligned_scene
from locator_demo.settings import SavedSettings,VisionProcessingConfig,FeatureConfig,save_settings
from locator_demo.camera import VisionFrameResult,VisionStatus
from locator_demo.web.app import DemoRuntime,RuntimeConfig


class Pinhole:
    """Independent known projection: rectified focal 500 px, centres 320/180."""
    def __init__(self, scale=1, roll=0):
        self.k=np.array([[500,0,320],[0,500,180],[0,0,1.]])*np.array([[scale],[scale],[1.]])
        c,s=math.cos(roll),math.sin(roll)
        self.r=np.array([[c,-s,0],[s,c,0],[0,0,1.]])
    def bearing_rays(self,eye,points,size):
        return (self.r@np.linalg.inv(self.k)@np.c_[points,np.ones(len(points))].T).T,self.k
    def direction_projection(self,size):
        return {eye:(self.k@self.r.T).tolist() for eye in ('left','right')}


def pair(points,rect=None,size=(640,360)):
    f=make_fixation(rect or Pinhole(),size,points,anchor_kind='paired_face_landmarks')
    eyes={eye:[FaceTarget(p[0]-35,p[1]-50,p[0]+35,p[1]+50,face_id=7,
            tracking_x=p[0],tracking_y=p[1],tracking_source='scrfd_kps',source_eye=eye,fixation=f)]
          for eye,p in points.items()}
    return eyes['left'][0] if 'left' in eyes else eyes['right'][0],eyes


class FixationGeometryTests(unittest.TestCase):
    def test_centred_near_target_needs_two_distinct_goals(self):
        t,eyes=pair({'left':[360,180],'right':[280,180]})
        np.testing.assert_allclose(normalized_center_error(t,frame_width=640,frame_height=360),(0,0),atol=1e-12)
        view=fixation_overlay(t,eyes,(640,360))
        for eye,point in [('left',[360,180]),('right',[280,180])]:
            np.testing.assert_allclose(view['eyes'][eye]['goal_px'],point,atol=1e-10)
        self.assertFalse(view['metric_distance_used'])

    def test_left_centred_but_right_off_centre_still_commands_joint_correction(self):
        t,eyes=pair({'left':[320,180],'right':[240,180]})
        self.assertAlmostEqual(normalized_center_error(t,frame_width=640,frame_height=360)[0],-.125)
        view=fixation_overlay(t,eyes,(640,360))
        np.testing.assert_allclose(view['eyes']['left']['goal_px'],[360,180])
        np.testing.assert_allclose(view['eyes']['right']['goal_px'],[280,180])
        # Common +40 pixel displacement completes the approach in BOTH eyes.
        t2,e2=pair({'left':[360,180],'right':[280,180]})
        self.assertLess(abs(normalized_center_error(t2,frame_width=640,frame_height=360)[0]),1e-12)

    def test_vertical_error_is_shared_not_discarded(self):
        t,e=pair({'left':[360,220],'right':[280,220]})
        self.assertAlmostEqual(normalized_center_error(t,frame_width=640,frame_height=360)[1],40/180)
        for v in fixation_overlay(t,e,(640,360))['eyes'].values():
            self.assertAlmostEqual(v['goal_px'][1],180)

    def test_near_far_and_negative_disparity_preserved(self):
        for d in (0,2,20,120,-40):
            t,e=pair({'left':[300+d/2,190],'right':[300-d/2,190]})
            view=fixation_overlay(t,e,(640,360))['eyes']
            self.assertAlmostEqual(view['left']['goal_px'][0]-view['right']['goal_px'][0],d)

    def test_custom_offsets_affect_control_and_both_goals(self):
        t,e=pair({'left':[360,180],'right':[280,180]})
        err=normalized_center_error(t,frame_width=640,frame_height=360,target_offset_x_norm=.05,target_offset_y_norm=-.05)
        np.testing.assert_allclose(err,[-.1,.1],atol=1e-12)
        view=fixation_overlay(t,e,(640,360),offsets=(.05,-.05))
        np.testing.assert_allclose(view['raw_error'],err,atol=1e-12)
        np.testing.assert_allclose(view['eyes']['right']['goal_px'],[312,162],atol=1e-10)

    def test_scaling_and_roll_use_projections(self):
        for scale in (1,2):
            t,e=pair({eye:(np.array(p)*scale).tolist() for eye,p in {'left':[360,180],'right':[280,180]}.items()},
                     rect=Pinhole(scale,.08),size=(640*scale,360*scale))
            view=fixation_overlay(t,e,(640*scale,360*scale))
            for eye in e:np.testing.assert_allclose(view['eyes'][eye]['target_px'],view['eyes'][eye]['goal_px'],atol=1e-9)

    def test_same_person_in_two_eyes_has_same_control_error(self):
        _,e=pair({'left':[400,220],'right':[300,220]})
        self.assertEqual(normalized_center_error(e['left'][0],frame_width=640,frame_height=360),
                         normalized_center_error(e['right'][0],frame_width=640,frame_height=360))

    def test_single_eye_goal_without_fake_second_reticle(self):
        t,e=pair({'right':[270,160]})
        v=fixation_overlay(t,e,(640,360))
        self.assertEqual(v['mode'],'single_eye');self.assertEqual(set(v['eyes']),{'right'})
        np.testing.assert_allclose(v['eyes']['right']['goal_px'],[320,180])

    def test_stale_absent_other_person_and_held_eye_hide_pair_goals(self):
        t,e=pair({'left':[360,180],'right':[280,180]})
        self.assertFalse(fixation_overlay(t,e,(640,360),fresh=False)['eyes'])
        for right in ([],[replace(e['right'][0],face_id=8)],[replace(e['right'][0],tracking_source='scrfd_held')]):
            self.assertFalse(fixation_overlay(t,{**e,'right':right},(640,360))['eyes'])

    def test_bad_geometry_and_size_do_not_draw_a_goal(self):
        t,e=pair({'left':[360,180],'right':[280,180]})
        self.assertFalse(fixation_overlay(t,e,(320,180))['eyes'])
        for key,value in [('ray',[float('nan'),0,1]),('focal_px',[0,500]),('projections',{'left':[[1]],'right':[[1]]})]:
            f=deepcopy(t.fixation);f[key]=value
            self.assertFalse(fixation_overlay(replace(t,fixation=f),e,(640,360))['eyes'])

    def test_landmarks_must_agree_with_verified_correspondences(self):
        t,e=pair({'left':[360,180],'right':[280,180]})
        p=np.array([[350,170],[360,180],[370,190]],dtype=float);q=p-[80,0]
        f=matched_fixation(e['left'][0],e['right'][0],p,q,Pinhole(),(640,360))
        self.assertEqual(f['anchor_kind'],'paired_face_landmarks')
        for bad in (replace(e['right'][0],tracking_y=220),replace(e['right'][0],tracking_x=300),replace(e['right'][0],tracking_source='different')):
            f=matched_fixation(e['left'][0],bad,p,q,Pinhole(),(640,360))
            self.assertEqual(f['anchor_kind'],'matched_face_texture')
            np.testing.assert_allclose(f['points']['right'],[280,180])

    def test_real_matcher_and_fusion_only_publish_confirmed_pairs(self):
        rect,images,targets=aligned_scene()
        direction=ScreenCenteredDirectionFrame(rect)
        matches=AlignedBearingMatcher().match(images,targets,direction)
        self.assertEqual(matches[0]['fixation']['mode'],'binocular')
        fusion=BinocularFusion(guided=True,visual_priority=True)
        health={eye:{'valid':True} for eye in images}
        first=fusion.update(images,targets,health,now=1,geometric_matches=matches)
        self.assertFalse(first['summary']['uses_two_eye_fixation'])
        for now in (1.1,1.2,1.3):r=fusion.update(images,targets,health,now=now,geometric_matches=matches)
        self.assertTrue(r['summary']['uses_two_eye_fixation'])
        r=fusion.update(images,{**targets,'right':[]},health,now=1.4,geometric_matches=[])
        self.assertFalse(r['summary']['uses_two_eye_fixation'])
        self.assertEqual(r['target'].fixation['mode'],'single_eye')

    def test_smoothing_resets_between_single_and_binocular_coordinates(self):
        t,_=pair({'left':[360,180],'right':[280,180]})
        mono,_=pair({'left':[450,180]})
        selector=VisualPrioritySelector()
        selector.smooth_target(mono,1,(640,360))
        actual=selector.smooth_target(t,1.1,(640,360))
        self.assertAlmostEqual(actual.fixation['virtual_u_px'],320)
        v=ImageVelocity();v.update((.5,0),identity=(7,'single'),sequence=1,observed_at=1)
        self.assertEqual(v.update((0,0),identity=(7,'binocular'),sequence=2,observed_at=1.1),(0,0))

    def test_runtime_commands_common_error_and_publishes_both_reticles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'settings.json'
            save_settings(path,SavedSettings(vision=VisionProcessingConfig(tracking_strategy='visual_priority',target_offset_x_norm=0),
                features=FeatureConfig(audio_enabled=False,visual_enabled=True,camera_enabled=True)))
            runtime=DemoRuntime(RuntimeConfig(simulated=True,settings_path=path))
            target,eyes=pair({'left':[320,220],'right':[240,220]})
            target=replace(target,score=.95)
            frame=VisionFrameResult(target,640,360,'',VisionStatus(True,'fixture','ok'),targets=[target],eye_targets=eyes,
                binocular={'people':[{'person_id':7}],'selected_person_id':7,'selected_eye':'left',
                           'sample_captured_monotonic':time.perf_counter(),'target_selection':{'locked_person_id':7}},
                control_ready=True,detection_sequence=20)
            runtime.vision=SimpleNamespace(read=lambda:frame,stop=lambda:None,capture_metrics=lambda:{},set_audio_guidance=Mock())
            runtime.move=Mock();runtime.running=True
            try:
                runtime.tick(include_frame=False)
                frame=replace(frame,detection_sequence=21)
                with patch('locator_demo.web.app.time.time',return_value=time.time()+.15):
                    result=runtime.tick(include_frame=False)
                self.assertEqual(runtime.move.call_count,1)
                np.testing.assert_allclose(runtime._visual_control['raw_error'],[-.125,40/180],atol=1e-12)
                overlay=result['visual']['fixation']
                self.assertEqual(set(overlay['eyes']),{'left','right'})
                self.assertEqual(result['algorithm_revision'],'R43')
                # Re-reading the same processed frame cannot issue another move.
                runtime.tick(include_frame=False)
                self.assertEqual(runtime.move.call_count,1)
            finally:runtime.stop()


if __name__=='__main__':unittest.main()
