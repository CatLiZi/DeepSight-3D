"""R52: exclusive owners, sound-first acquisition and interruptible vision.

Pure fixtures: no serial ports, microphones, cameras or motor commands.
"""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from locator_demo.audio_primary import AudioPrimarySelector
from locator_demo.binocular import BinocularFusion
from locator_demo.settings import FeatureConfig, load_settings
from locator_demo.head import HeadPose
from locator_demo.web.app import FeatureConfigRequest
import test_dominant_sound as core_fixture
from test_visual_priority import face
from test_av_guidance import voice


class AudioPrimarySelectionTests(unittest.TestCase):
    def setUp(self):
        self.s = AudioPrimarySelector()
        self.now = 10.
        self.n = 0
        self.people = {1: [face(1, 2)], 2: [face(2, -40, width=160)]}

    def step(self, *, core=1, epoch=1, angle=0., people=None, speaking=False,
             dt=.1, stamp=None, context_age=0.):
        self.now += dt
        self.n += 1
        self.s.acoustic_target = dict(epoch=epoch, core_id=core, locked=core is not None,
            geometry_valid=True, relative_azimuth_deg=angle,
            live_matches_core=speaking, updated_monotonic=self.now-context_age)
        return self.s.update(self.people if people is None else people, now=self.now,
            size=(640,360), audio=voice(angle,self.now,self.n) if speaking else None,
            frame_time=self.now if stamp is None else stamp)

    def acquired(self):
        for _ in range(5): selected = self.step()
        self.assertEqual(selected, 1)

    def test_silence_does_not_select_largest_face_without_acoustic_goal(self):
        for _ in range(20): self.assertIsNone(self.step(core=None))

    def test_far_goal_requires_turn_before_visual_acquisition(self):
        for _ in range(10): self.assertIsNone(self.step(angle=-40, speaking=True))
        self.assertEqual(self.s.metrics['mode'],'turn_to_sound')

    def test_near_goal_selects_face_in_sound_interval_not_largest_bystander(self):
        self.acquired()
        self.assertEqual(self.s.metrics['mode'],'visual_refine')

    def test_brief_speech_can_finish_turn_and_acquire_face_in_silence(self):
        self.step(angle=-40, speaking=True)
        for _ in range(5): self.assertIsNone(self.step(angle=-25))
        self.acquired()
        self.assertFalse(self.s.metrics['audio_usable'])

    def test_new_confirmed_source_immediately_interrupts_visible_face(self):
        self.acquired()
        self.assertIsNone(self.step(core=2, angle=-40, speaking=True))
        self.assertIsNone(self.s.person)
        shifted={1:[face(1,40)],2:[face(2,0)]}
        for _ in range(5): selected=self.step(core=2,angle=0,people=shifted)
        self.assertEqual(selected,2)

    def test_new_epoch_cannot_reuse_old_face_with_recycled_core_id(self):
        self.acquired()
        self.assertIsNone(self.step(epoch=2,angle=-35))
        self.assertIsNone(self.s.person)

    def test_silent_matched_person_can_move_and_remains_visual_target(self):
        self.acquired()
        moving={1:[face(1,22)],2:[face(2,-40,width=160)]}
        for _ in range(30): self.assertEqual(self.step(people=moving),1)

    def test_disappearance_does_not_fall_back_to_unrelated_face(self):
        self.acquired()
        for _ in range(30): self.assertIsNone(self.step(people={2:self.people[2]}))

    def test_old_goal_cannot_recruit_later_bystander_after_arrival_window(self):
        for _ in range(35): self.step(people={})
        for _ in range(10): self.assertIsNone(self.step())
        # A new current observation allows association again.
        for _ in range(5): selected=self.step(speaking=True)
        self.assertEqual(selected,1)

    def test_short_face_occlusion_recovers_same_face_without_new_audio(self):
        self.acquired()
        for _ in range(3): self.assertIsNone(self.step(people={}))
        self.assertEqual(self.step(),1)

    def test_duplicate_or_stale_video_never_confirms_face(self):
        stamp=self.now+.1
        for _ in range(6): self.assertIsNone(self.step(stamp=stamp))
        self.assertIsNone(self.step(stamp=self.now-1))

    def test_observation_gap_does_not_bridge_candidate_confirmation(self):
        self.step()
        self.assertIsNone(self.step(dt=.6))

    def test_stale_context_withdraws_visual_assist_without_refreshing_deadline(self):
        self.acquired()
        for _ in range(30): self.step()
        self.assertIsNone(self.step(context_age=1))
        for _ in range(5): self.assertIsNone(self.step())

    def test_invalid_angle_never_selects_face(self):
        for angle in (float('nan'),float('inf'),91.):
            self.assertIsNone(self.step(angle=angle))

    def test_same_cluster_conflict_needs_multiple_fresh_audio_observations(self):
        self.acquired()
        self.assertEqual(self.step(angle=-25,speaking=True),1)
        for _ in range(5): selected=self.step(angle=-25,speaking=True)
        self.assertIsNone(selected)


class PrimaryModeRuntimeTests(unittest.TestCase):
    setUp_fixture=core_fixture.CoreRuntimeTests.setUp_fixture
    tearDown=core_fixture.CoreRuntimeTests.tearDown
    tick_voice=core_fixture.CoreRuntimeTests.tick_voice

    def setUp(self):
        core_fixture.CoreRuntimeTests.setUp(self)
        r=self.runtime
        r.features=FeatureConfig(primary_mode='audio_primary')
        self.selector=AudioPrimarySelector()
        self.context={}
        self.visual_people={1:[face(1,0)]}
        self.freeze_frame=False
        def receive(context): self.context=context
        def read():
            if not self.freeze_frame:
                self.selector.acoustic_target=self.context
                target_id=self.selector.update(self.visual_people,now=self.now,size=(640,400),
                    audio=self.guidance.call_args.args[0] if self.guidance.call_args else None,
                    frame_time=self.now)
                target=self.visual_people[target_id][0] if target_id is not None else None
                self.published=replace(self.frame,target=target,targets=[t for ts in self.visual_people.values() for t in ts],
                    control_ready=target is not None,
                    binocular={'people':[{'person_id':i} for i in self.visual_people],
                        'target_selection':dict(self.selector.metrics),
                        'selection_reason':'observed_face' if target else 'no_faces'})
            return self.published
        r.vision=SimpleNamespace(read=read,stop=lambda:None,capture_metrics=lambda:{},
            set_audio_guidance=self.guidance,set_acoustic_target=receive,set_guided_mode=Mock())

    def seed_face(self):
        for _ in range(16): self.tick_voice(angle=0.)
        self.assertTrue(self.runtime.last_state.target_confirmed)

    def test_visible_face_does_not_block_other_side_sound(self):
        self.seed_face()
        for _ in range(40): self.tick_voice(angle=-40.)
        self.assertTrue(self.commands)
        self.assertLess(self.commands[-1].yaw,1500)
        self.assertFalse(self.runtime.last_state.target_confirmed)

    def test_old_camera_result_cannot_hold_control_after_new_core_switch(self):
        self.seed_face()
        self.freeze_frame=True
        self.head.is_moving=True
        for _ in range(40): state=self.tick_voice(angle=-40.)
        self.assertFalse(state['visual']['locked'])
        self.head.cancel.assert_called()
        self.assertTrue(state['audio']['core_source']['motion_authorized'])

    def test_utterance_pause_still_completes_finite_audio_turn(self):
        self.visual_people={}
        for _ in range(10): state=self.tick_voice(angle=-35.)
        goal=state['audio']['core_source']['goal_yaw']
        for _ in range(6):
            self.head.estimated_pose=self.runtime.pose
            state=self.tick_voice(unknown=True)
        self.assertEqual(self.runtime.pose.yaw,goal)
        self.assertEqual(state['audio']['direction'],'unknown')

    def test_visual_assist_uses_existing_adaptive_pitch_and_yaw_controller(self):
        self.seed_face()
        self.commands.clear()
        self.visual_people={1:[replace(face(1,-25),y1=220,y2=380)]}
        for _ in range(12): state=self.tick_voice(unknown=True)
        self.assertTrue(self.commands)
        self.assertTrue(any(p.pitch!=1550 for p in self.commands))
        self.assertEqual(state['audio']['core_source']['motion_reason'],'audio_primary_visual_refinement')

    def test_visual_loss_without_new_voice_does_not_resume_old_sound_goal(self):
        self.seed_face()
        self.visual_people={}
        self.commands.clear()
        for _ in range(30): state=self.tick_voice(unknown=True)
        self.assertFalse(self.commands)
        self.assertFalse(state['audio']['core_source']['motion_authorized'])

    def test_camera_off_keeps_independent_audio_turn(self):
        self.runtime.features=FeatureConfig(primary_mode='audio_primary',camera_enabled=False)
        for _ in range(12): self.tick_voice(angle=-35.)
        self.assertTrue(self.commands)

    def test_failed_stereo_preflight_disables_visual_assist_but_not_audio(self):
        self.runtime._stereo_preflight={'status':'misaligned'}
        for _ in range(12): state=self.tick_voice(angle=-35.)
        self.assertTrue(self.commands)
        self.assertFalse(state['visual']['locked'])

    def test_visual_assistance_toggle_does_not_mute_primary_sound_guidance(self):
        r=self.runtime
        r.vision_processing=replace(r.vision_processing,active_speaker_enabled=False)
        self.seed_face()
        self.assertIsNotNone(self.guidance.call_args.args[0])

    def test_visual_primary_keeps_existing_visual_priority_despite_offscreen_sound(self):
        self.runtime.features=FeatureConfig(primary_mode='visual_priority')
        self.runtime.vision.read=lambda:self.frame
        for _ in range(30): state=self.tick_voice(angle=-40.)
        self.assertFalse(self.commands)
        self.assertEqual(state['audio']['core_source']['motion_reason'],'visual_target_has_priority')

    def test_feature_api_is_exclusive_persisted_and_cancels_previous_goal(self):
        self.seed_face()
        response=self.runtime.update_feature_config(FeatureConfigRequest(primary_mode='visual_priority'))
        self.assertFalse(response['features']['audio_enabled'])
        self.assertTrue(response['features']['visual_enabled'])
        self.assertTrue(self.runtime.features.uses_audio)
        self.assertEqual(load_settings(self.runtime.config.settings_path).features.primary_mode,'visual_priority')
        self.assertFalse(self.runtime._dominant_sound.snapshot()['locked'])
        self.head.cancel.assert_called()

    def test_preview_or_stop_does_not_issue_commands(self):
        for mode in ('preview','audio_primary'):
            self.runtime.features=FeatureConfig(primary_mode=mode)
            if mode=='audio_primary':self.runtime.halt_motion()
            self.commands.clear()
            for _ in range(15): self.tick_voice(angle=-40.)
            self.assertFalse(self.commands)

    def test_legacy_both_switch_payload_resolves_to_one_owner(self):
        response=self.runtime.update_feature_config(FeatureConfigRequest(audio_enabled=True,visual_enabled=True))
        self.assertEqual(response['features']['primary_mode'],'visual_priority')
        self.assertFalse(response['features']['audio_enabled'])


class PrimaryBinocularTests(unittest.TestCase):
    def test_stereo_pipeline_associates_sound_face_and_revokes_both_eyes_on_source_change(self):
        import numpy as np
        fusion=BinocularFusion(guided=True,audio_primary=True)
        images={eye:np.zeros((360,640,3),dtype=np.uint8) for eye in ('left','right')}
        targets={'left':[face(7,0)],'right':[replace(face(9,0),source_eye='right')]}
        health={eye:{'valid':True,'quality_score':1} for eye in images}
        match=[{'left_track':7,'right_track':9,'bearing':targets['left'][0].bearing}]
        for i in range(12):
            now=10+i*.1
            result=fusion.update(images,targets,health,now=now,frame_time=now,
                geometric_matches=match,acoustic_target=dict(epoch=3,core_id=1,locked=True,
                    geometry_valid=True,relative_azimuth_deg=0,updated_monotonic=now))
        self.assertTrue(result['control_ready'])
        selected=result['target'].face_id
        self.assertTrue(all(any(t.face_id==selected for t in ts) for ts in result['targets'].values()))
        result=fusion.update(images,targets,health,now=11.2,frame_time=11.2,geometric_matches=match,
            acoustic_target=dict(epoch=3,core_id=2,locked=True,geometry_valid=True,
                relative_azimuth_deg=-40,updated_monotonic=11.2))
        self.assertIsNone(result['target'])
        self.assertFalse(result['control_ready'])
        self.assertEqual(result['summary']['target_selection']['acoustic_core_id'],2)


if __name__=='__main__': unittest.main()
