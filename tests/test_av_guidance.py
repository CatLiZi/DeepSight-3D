"""Known-geometry and decision-sequence tests; no physical device is opened."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import math,tempfile,time,unittest
from unittest.mock import Mock
import cv2
import numpy as np

from locator_demo.audio import AudioEstimate,AudioDirection,apply_audio_channel_mapping,with_motor_suppression
from locator_demo.av_selector import AudioVisualSelector
from locator_demo.bearing import AlignedBearingMatcher,annotate_eye_bearings
from locator_demo.binocular import BinocularFusion
from locator_demo.camera import VisionFrameResult,VisionStatus
from locator_demo.settings import SavedSettings,VisionProcessingConfig,FeatureConfig,save_settings,load_settings
from locator_demo.speech_vad import StreamingSpeechVAD
from locator_demo.stereo_calibration import StereoRectifier,make_alignment_preview_profile
from locator_demo.vision import FaceTarget,normalized_center_error
from locator_demo.web.app import DemoRuntime,RuntimeConfig,VisionProcessingConfigRequest
from test_stereo_calibration import alignment_source


def voice(angle,now,sequence,**kwargs):
    return AudioEstimate(AudioDirection.LEFT if angle>8 else AudioDirection.RIGHT if angle< -8 else AudioDirection.CENTER,
        .9,angle/100000,.2,angle,.95,.9,2.5,'speech_like',observed_monotonic=now,
        sample_sequence=sequence,temporal_stability=1.,azimuth_std_deg=6.,speech_backend='test_voice',**kwargs)


def person(identity,angle,*,width=80,score=.85):
    x=320-500*math.tan(math.radians(angle));y=160
    b={'frame':'rig_optical_bisector','azimuth_left_deg':angle,'azimuth_interval_deg':[angle-3,angle+3],
        'sigma_deg':6.,'confidence':.9,'source':'binocular_feature_rays',
        'virtual_u_px':x,'virtual_v_px':y}
    return FaceTarget(x-width/2,80,x+width/2,240,face_id=identity,score=score,tracking_source='scrfd',bearing=b)


def aligned_scene(baseline=.082):
    p=alignment_source();p['T']=[[-baseline],[0.],[0.]]
    rect=StereoRectifier(make_alignment_preview_profile(p),allow_alignment_only=True)
    rng=np.random.default_rng(384);gray=cv2.GaussianBlur(rng.integers(10,245,(360,640),dtype=np.uint8),(3,3),.6)
    disparity=500*baseline/2.
    right=cv2.warpAffine(gray,np.float32([[1,0,-disparity],[0,1,0]]),(640,360))
    images=rect.apply({eye:cv2.cvtColor(im,cv2.COLOR_GRAY2BGR) for eye,im in (('left',gray),('right',right))})
    x,y,_,_=rect.crop
    a=FaceTarget(180-x,70-y,310-x,240-y,face_id=1,score=.9,tracking_source='scrfd')
    b=replace(a,x1=a.x1-disparity,x2=a.x2-disparity,face_id=2)
    targets=annotate_eye_bearings({'left':[a],'right':[b]},rect,images['left'].shape[1::-1])
    return rect,images,targets


class BearingOnlyTests(unittest.TestCase):
    def test_alignment_can_estimate_binocular_direction_without_metric_projection_or_depth(self):
        rect,images,targets=aligned_scene();matches=AlignedBearingMatcher().match(images,targets,rect)
        self.assertEqual(len(matches),1);self.assertIsNone(rect.q)
        self.assertFalse(rect.metric_ready);self.assertNotIn('position',matches[0])
        self.assertEqual(matches[0]['kind'],'aligned_feature_correspondence')
        self.assertGreaterEqual(matches[0]['bearing']['inliers'],8)
        with self.assertRaises(ValueError):rect.projection_for_size((640,360))

    def test_direction_ray_does_not_depend_on_nominal_baseline_length(self):
        first=alignment_source();second=deepcopy(first);second['T']=[[-.164],[0.],[0.]]
        a=StereoRectifier(make_alignment_preview_profile(first),allow_alignment_only=True)
        b=StereoRectifier(make_alignment_preview_profile(second),allow_alignment_only=True)
        ra,_=a.bearing_rays('left',[[320,180],[400,190]],a.crop[2:])
        rb,_=b.bearing_rays('left',[[320,180],[400,190]],b.crop[2:])
        np.testing.assert_allclose(ra,rb,atol=1e-9)

    def test_negative_disparity_order_also_matches_without_distance(self):
        rect,images,targets=aligned_scene(-.082)
        self.assertEqual(len(AlignedBearingMatcher().match(images,targets,rect)),1)

    def test_missing_or_flat_second_view_never_fabricates_shared_bearing(self):
        rect,images,targets=aligned_scene();targets['right']=[]
        self.assertEqual(AlignedBearingMatcher().match(images,targets,rect),[])
        rect,images,targets=aligned_scene();images['right'][:]=146
        self.assertEqual(AlignedBearingMatcher().match(images,targets,rect),[])

    def test_duplicate_visual_candidates_are_not_resolved_by_bearing_match_score_alone(self):
        rect,images,targets=aligned_scene();targets['right'].append(replace(targets['right'][0],face_id=3))
        self.assertEqual(AlignedBearingMatcher().match(images,targets,rect),[])

    def test_two_eye_association_preserves_bearing_but_never_sets_xyz(self):
        rect,images,targets=aligned_scene();matches=AlignedBearingMatcher().match(images,targets,rect)
        fusion=BinocularFusion(guided=True);health={eye:{'valid':True} for eye in images}
        for t in (1.,1.1,1.2,1.3):result=fusion.update(images,targets,health,now=t,geometric_matches=matches)
        self.assertEqual(len(result['summary']['people']),1)
        self.assertTrue(result['summary']['uses_two_eye_bearing'])
        self.assertIsNone(result['target'].stereo_position)
        self.assertFalse(result['summary']['depth_enabled'])

    def test_control_uses_common_bearing_instead_of_the_display_eye_box(self):
        a=person(1,20.);b=replace(a,x1=a.x1+200,x2=a.x2+200)
        self.assertEqual(normalized_center_error(a,frame_width=640,frame_height=360),
            normalized_center_error(b,frame_width=640,frame_height=360))


class GuidedSelectionTests(unittest.TestCase):
    def setUp(self):
        self.big=person(1,-25,width=160,score=.95);self.small=person(2,25,width=50,score=.8)
        self.people={1:[self.big],2:[self.small]}

    def run_voice(self,selector,angle,start=1.,count=7):
        result=None
        for n in range(count):
            now=start+n*.1;result=selector.update(self.people,now=now,size=(640,360),audio=voice(angle,now,n+round(start*100)))
        return result

    def test_voice_direction_selects_smaller_face_instead_of_largest_face(self):
        selector=AudioVisualSelector();self.assertEqual(self.run_voice(selector,25),2)
        self.assertEqual(selector.metrics['mode'],'audio_guided')
        self.assertFalse(selector.metrics['speaker_identity_confirmed'])

    def test_silence_keeps_visual_lock_without_needing_speech_to_continue(self):
        selector=AudioVisualSelector();self.run_voice(selector,25)
        for t in (2.,2.5,3.):self.assertEqual(selector.update(self.people,now=t,size=(640,360)),2)
        self.assertEqual(selector.metrics['mode'],'visual_fallback')

    def test_initial_silent_scene_uses_visual_quality(self):
        selector=AudioVisualSelector();self.assertEqual(selector.update(self.people,now=1.,size=(640,360)),1)

    def test_repeated_same_audio_sequence_cannot_accumulate_confirmation(self):
        selector=AudioVisualSelector()
        for n in range(8):
            t=1+n*.1;winner=selector.update(self.people,now=t,size=(640,360),audio=voice(25,t,7))
        self.assertEqual(winner,1);self.assertEqual(selector.audio_hits,1)

    def test_stale_sound_cannot_pull_visual_lock_to_another_person(self):
        selector=AudioVisualSelector();self.assertEqual(selector.update(self.people,now=2.,size=(640,360),audio=voice(25,1.,1)),1)
        self.assertEqual(selector.metrics['audio_reason'],'stale_audio')

    def test_angularly_ambiguous_people_keep_visual_fallback(self):
        selector=AudioVisualSelector();people={1:[person(1,-1,score=.95)],2:[person(2,1,score=.8)]}
        for n in range(8):
            t=1+n*.1;winner=selector.update(people,now=t,size=(640,360),audio=voice(0,t,n))
        self.assertEqual(winner,1);self.assertEqual(selector.metrics['audio_reason'],'angular_ambiguity')

    def test_out_of_view_sound_does_not_select_nearest_visible_face_arbitrarily(self):
        selector=AudioVisualSelector();self.assertEqual(self.run_voice(selector,85),1)
        self.assertEqual(selector.metrics['audio_reason'],'audio_outside_visible_faces')

    def test_motor_noise_protection_forces_visual_fallback(self):
        selector=AudioVisualSelector();a=replace(voice(25,1.,1),motor_suppressed=True)
        self.assertEqual(selector.update(self.people,now=1.,size=(640,360),audio=a),1)
        self.assertEqual(selector.metrics['audio_reason'],'motor_guard')

    def test_short_visual_loss_does_not_reuse_old_coordinates_or_jump_to_bystander(self):
        selector=AudioVisualSelector();self.run_voice(selector,25)
        self.assertIsNone(selector.update({1:[self.big]},now=1.7,size=(640,360)))
        self.assertEqual(selector.metrics['mode'],'visual_reacquire_wait')
        self.assertEqual(selector.update(self.people,now=1.8,size=(640,360)),2)
        self.assertEqual(selector.update({1:[self.big]},now=3.,size=(640,360)),1)

    def test_same_eye_short_id_reset_recovers_fresh_coordinates(self):
        selector=AudioVisualSelector();old=replace(self.big,source_eye='left')
        selector.update({1:[old]},now=1.,size=(640,360))
        new=replace(old,face_id=9,x1=old.x1+2,x2=old.x2+2)
        self.assertEqual(selector.update({9:[new]},now=1.1,size=(640,360)),9)
        self.assertTrue(selector.metrics['continuity_reassociated'])

    def test_short_id_reset_never_merges_two_ambiguous_candidates(self):
        selector=AudioVisualSelector();old=replace(self.big,source_eye='left')
        selector.update({1:[old]},now=1.,size=(640,360))
        candidates={9:[replace(old,face_id=9)],10:[replace(old,face_id=10)]}
        self.assertIsNone(selector.update(candidates,now=1.1,size=(640,360)))
        self.assertFalse(selector.metrics['continuity_reassociated'])

    def test_eye_history_survives_brief_opposite_eye_occlusion(self):
        selector=AudioVisualSelector();a=replace(self.big,source_eye='left');b=replace(a,source_eye='right')
        selector.update({1:[a,b]},now=1.,size=(640,360))
        selector.update({1:[b]},now=1.5,size=(640,360))
        self.assertEqual(selector.update({9:[replace(a,face_id=9)]},now=1.7,size=(640,360)),9)

    def test_new_direction_requires_stable_evidence_before_switch(self):
        selector=AudioVisualSelector();self.run_voice(selector,25)
        for n in range(3):
            t=1.7+n*.1;self.assertEqual(selector.update(self.people,now=t,size=(640,360),audio=voice(-25,t,n+50)),2)
        self.assertEqual(self.run_voice(selector,-25,start=2.,count=7),1)

    def test_audio_video_host_time_gap_cannot_be_treated_as_synchronized(self):
        selector=AudioVisualSelector();selector.update(self.people,now=2.,size=(640,360),audio=voice(25,2.,1),frame_time=1.)
        self.assertEqual(selector.metrics['audio_reason'],'audio_video_time_gap')

    def test_time_based_direction_filter_has_same_step_response_at_different_rates(self):
        a=AudioVisualSelector();b=AudioVisualSelector();old=person(1,0);new=person(1,20)
        a.smooth_target(old,1.,(640,360));b.smooth_target(old,1.,(640,360))
        for t in (1.05,1.1,1.15,1.2):ra=a.smooth_target(new,t,(640,360))
        for t in (1.1,1.2):rb=b.smooth_target(new,t,(640,360))
        self.assertAlmostEqual(ra.bearing['virtual_u_px'],rb.bearing['virtual_u_px'],places=7)

    def test_metadata_survives_channel_mapping_and_motor_suppression(self):
        a=voice(25,10.,77);b=apply_audio_channel_mapping(a,swap_channels=True)
        self.assertEqual(b.sample_sequence,77);self.assertEqual(b.observed_monotonic,10.)
        self.assertEqual(b.azimuth_deg,-25)
        self.assertEqual(with_motor_suppression(a,suppressed=True).sample_sequence,77)


class SpeechVADTests(unittest.TestCase):
    @unittest.skipUnless((Path(__file__).resolve().parents[1]/'models/silero_vad.onnx').exists(),'local official model not installed')
    def test_real_model_silence_is_not_speech_and_repeated_audio_is_not_replayed(self):
        vad=StreamingSpeechVAD();audio=np.zeros((4800,2),np.int16)
        p=vad.update(audio,sample_start=0);calls=vad.calls
        self.assertLess(p,.1);vad.update(audio,sample_start=0);self.assertEqual(vad.calls,calls)

    @unittest.skipUnless((Path(__file__).resolve().parents[1]/'models/silero_vad.onnx').exists(),'local official model not installed')
    def test_streaming_resampler_preserves_results_across_callback_boundaries(self):
        audio=np.random.default_rng(1).integers(-3000,3000,(15360,2),dtype=np.int16)
        a=StreamingSpeechVAD();b=StreamingSpeechVAD()
        a.update(audio,sample_start=0)
        for start in range(0,len(audio),960):b.update(audio[start:start+960],sample_start=start)
        np.testing.assert_allclose(a.state,b.state,atol=1e-5);self.assertEqual(a.calls,b.calls)

    def test_missing_model_fallback_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            vad=StreamingSpeechVAD(Path(tmp)/'missing.onnx')
            self.assertIsNone(vad.update(np.zeros((1536,2)),sample_start=0))
            self.assertEqual(vad.metrics()['backend'],'spectral_fallback')


class GuidedRuntimeTests(unittest.TestCase):
    def test_guided_strategy_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'settings.json';save_settings(p,SavedSettings(vision=VisionProcessingConfig(tracking_strategy='audio_visual_guided')))
            self.assertEqual(load_settings(p).vision.tracking_strategy,'audio_visual_guided')

    def test_runtime_honours_binocular_selection_without_legacy_largest_face_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'settings.json';save_settings(p,SavedSettings(vision=VisionProcessingConfig(tracking_strategy='audio_visual_guided'),
                features=FeatureConfig(audio_enabled=True,visual_enabled=True,camera_enabled=True)))
            runtime=DemoRuntime(RuntimeConfig(simulated=True,settings_path=p));large=person(1,-25,width=160);small=person(2,25,width=50)
            frame=VisionFrameResult(small,640,360,'',VisionStatus(True,'fixture','ok'),targets=[large,small],
                binocular={'people':[{'person_id':1},{'person_id':2}],'target_selection':{'mode':'audio_guided','audio_usable':True}},control_ready=True)
            runtime.vision=SimpleNamespace(read=lambda:frame,stop=lambda:None,capture_metrics=lambda:{},set_audio_guidance=Mock())
            try:
                runtime.tick(include_frame=False)
                self.assertEqual(runtime.last_target.face_id,2);self.assertFalse(runtime.running)
                self.assertFalse(runtime.audio_search_allowed)
            finally:runtime.stop()

    def test_switching_strategy_updates_vision_worker_without_motion(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime=DemoRuntime(RuntimeConfig(simulated=True,settings_path=Path(tmp)/'settings.json'))
            runtime.vision.set_guided_mode=Mock()
            try:
                runtime.update_vision_processing_config(VisionProcessingConfigRequest(tracking_strategy='audio_visual_guided'))
                runtime.vision.set_guided_mode.assert_called_once_with(True)
                self.assertFalse(runtime.running)
            finally:runtime.stop()


if __name__=='__main__':unittest.main()
