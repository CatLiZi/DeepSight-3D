"""Ownership, competing sources and natural speech pauses; no hardware I/O."""
from copy import copy
from dataclasses import replace
import unittest

from locator_demo.audio import AudioDirection
from locator_demo.head import HeadPose
from locator_demo.visual_priority import VisualPrioritySelector
import test_dominant_sound as core_fixture
from test_visual_priority import face


class EvidenceReviewTests(unittest.TestCase):
    setUp = core_fixture.CoreEvidenceTests.setUp
    feed = core_fixture.CoreEvidenceTests.feed
    acquire = core_fixture.CoreEvidenceTests.acquire

    def test_unqualified_short_peak_cannot_mask_qualified_sustained_source(self):
        weak=dict(speech_confidence=.7,confidence=.4,doa_confidence=.4,
                  temporal_stability=.7,azimuth_std_deg=15.)
        for _ in range(4):self.feed(30.,**weak)
        for _ in range(2):self.feed(-45.,dt=.1)
        for _ in range(4):state=self.feed(30.,**weak)
        self.assertTrue(state['locked'])
        self.assertGreater(state['goal_yaw'],1500)
        # The louder, shorter burst is still ranked first but lacks duration.
        self.assertLess(state['candidates'][0]['voiced_s'],.24)

    def test_unrelated_new_packets_do_not_confirm_absent_challenger(self):
        original=self.acquire(30.)['core_id']
        for _ in range(50):
            self.feed(-35.)
            if self.lock.challenger is not None:break
        self.assertIsNotNone(self.lock.challenger)
        for _ in range(4):
            state=self.feed(0.,dt=.1,speech_confidence=.55,confidence=.2,
                            doa_confidence=.2,temporal_stability=.5)
        self.assertEqual(state['core_id'],original)

    def test_a_genuinely_continuing_stronger_challenger_still_switches(self):
        original=self.acquire(30.)['core_id']
        for _ in range(55):state=self.feed(-35.)
        self.assertNotEqual(state['core_id'],original)
        self.assertTrue(state['fresh_core_observation'])

    def test_cached_core_direction_is_not_new_motion_authorization(self):
        self.acquire()
        cached=core_fixture.observation(35.,self.clock,self.sequence)
        state=self.feed(dt=.02,audio=cached)
        self.assertTrue(state['live_matches_core'])
        self.assertFalse(state['fresh_core_observation'])


class OwnershipReviewTests(unittest.TestCase):
    setUp_fixture = core_fixture.CoreRuntimeTests.setUp_fixture
    setUp = core_fixture.CoreRuntimeTests.setUp
    tearDown = core_fixture.CoreRuntimeTests.tearDown
    tick_voice = core_fixture.CoreRuntimeTests.tick_voice
    acquire = core_fixture.CoreRuntimeTests.acquire

    def no_face(self,*,reason='no_faces',ready=True):
        self.frame=replace(self.frame,target=None,targets=[],control_ready=ready,
                           binocular={'people':[],'selection_reason':reason})

    def test_two_modes_on_can_listen_with_no_usable_camera(self):
        self.runtime.features=replace(self.runtime.features,visual_enabled=True)
        self.no_face(reason='no_usable_eye',ready=False)
        state=self.acquire()
        self.assertTrue(self.commands)
        self.assertTrue(state['audio']['core_source']['motion_authorized'])
        self.assertTrue(all(p.pitch==1550 for p in self.commands))

    def test_visual_lock_during_camera_confirmation_keeps_priority(self):
        self.runtime.features=replace(self.runtime.features,visual_enabled=True)
        self.frame=replace(self.frame,control_ready=False,binocular={
            'people':[{'person_id':7}], 'selection_reason':'stale_frame',
            'target_selection':{'locked_person_id':7}})
        state=self.acquire()
        self.assertFalse(self.commands)
        self.assertFalse(state['audio']['core_source']['motion_authorized'])

    def test_visual_takeover_revokes_old_motion_but_keeps_memory(self):
        self.runtime.features=replace(self.runtime.features,visual_enabled=True)
        before=self.acquire()
        self.runtime.pose=HeadPose(1800,1550);self.head.estimated_pose=self.runtime.pose
        self.tick_voice(unknown=True)
        self.commands.clear();self.no_face()
        state=self.tick_voice(unknown=True)
        self.assertFalse(self.commands)
        self.assertTrue(state['audio']['core_source']['locked'])
        self.assertEqual(state['audio']['core_source']['goal_yaw'],before['audio']['core_source']['goal_yaw'])
        self.assertFalse(state['audio']['core_source']['motion_authorized'])

    def test_fresh_reliable_sound_can_authorize_again_after_visual_loss(self):
        self.runtime.features=replace(self.runtime.features,visual_enabled=True)
        self.acquire();self.no_face()
        state=self.tick_voice()
        self.assertTrue(state['audio']['core_source']['motion_authorized'])
        self.assertTrue(self.commands)

    def test_unknown_cannot_cancel_an_authorized_turn_during_camera_loss(self):
        self.runtime.features=replace(self.runtime.features,visual_enabled=True)
        self.no_face();self.acquire()
        self.head.is_moving=True;self.no_face(reason='no_usable_eye',ready=False)
        self.head.cancel.reset_mock()
        state=self.tick_voice(unknown=True)
        self.head.cancel.assert_not_called()
        self.assertTrue(state['audio']['core_source']['motion_authorized'])

    def test_visual_takeover_cancels_pending_audio_trajectory_at_current_pose(self):
        self.no_face();self.acquire();self.head.is_moving=True
        self.runtime.features=replace(self.runtime.features,visual_enabled=True)
        self.frame=replace(self.frame,target=self.target,targets=[self.target],binocular={
            'people':[{'person_id':7}],'target_selection':{'locked_person_id':7}})
        self.head.cancel.reset_mock();state=self.tick_voice(unknown=True)
        self.head.cancel.assert_called_once()
        self.assertFalse(state['audio']['core_source']['motion_authorized'])

    def test_replaced_audio_stream_clears_old_position_and_authorization(self):
        self.acquire();self.commands.clear()
        self.runtime.audio=copy(self.runtime.audio)
        state=self.tick_voice(unknown=True)
        self.assertFalse(state['audio']['core_source']['locked'])
        self.assertFalse(state['audio']['core_source']['motion_authorized'])
        self.assertFalse(self.commands)

    def test_profile_change_cannot_reinterpret_old_goal_with_a_new_gain(self):
        self.acquire();self.commands.clear()
        profile='fast' if self.runtime.control.control_profile=='stable' else 'stable'
        self.runtime.control=replace(self.runtime.control,control_profile=profile)
        state=self.tick_voice(unknown=True)
        self.assertFalse(state['audio']['core_source']['locked'])
        self.assertFalse(self.commands)

    def test_channel_model_change_invalidates_previous_direction_memory(self):
        self.acquire();self.commands.clear()
        self.runtime.audio_capture=replace(self.runtime.audio_capture,ear_spacing_m=.136)
        state=self.tick_voice(unknown=True)
        self.assertFalse(state['audio']['core_source']['locked'])
        self.assertFalse(self.commands)

    def test_enabled_unknown_audio_is_distinct_from_audio_feature_off(self):
        self.acquire();self.tick_voice(unknown=True)
        self.assertEqual(self.guidance.call_args.args[0].direction,AudioDirection.UNKNOWN)
        self.runtime.features=replace(self.runtime.features,audio_enabled=False)
        self.tick_voice(unknown=True)
        self.assertIsNone(self.guidance.call_args.args[0])

    def test_unconfirmed_new_direction_is_not_presented_as_disabled_assistance(self):
        self.tick_voice()
        guidance=self.guidance.call_args.args[0]
        self.assertEqual(guidance.direction,AudioDirection.UNKNOWN)
        self.assertEqual(guidance.noise_state,'awaiting_core_confirmation')


class SpeakerPauseReviewTests(unittest.TestCase):
    def setUp(self):
        self.selector=VisualPrioritySelector()
        self.people={1:[face(1,-30,width=80)],2:[face(2,30,width=150)]}
        self.clock=10.;self.sequence=0

    def tick(self,angle=None,*,disabled=False,people=None):
        self.clock+=.1;self.sequence+=1
        audio=core_fixture.observation(angle or 0.,self.clock,self.sequence)
        if angle is None:
            audio=replace(audio,direction=AudioDirection.UNKNOWN,speech_confidence=0.,
                          confidence=0.,doa_confidence=0.,noise_state='no_voice_activity')
        return self.selector.update(self.people if people is None else people,now=self.clock,
            size=(640,360),audio=None if disabled else audio,frame_time=self.clock)

    def confirm(self):
        for _ in range(25):selected=self.tick(-30.)
        self.assertEqual(selected,1)

    def test_visible_confirmed_speaker_survives_long_speech_pause(self):
        self.confirm()
        for _ in range(100):self.assertEqual(self.tick(),1)

    def test_confirming_current_visual_person_promotes_speaker_retention(self):
        for _ in range(12):self.tick(-30.,people={1:self.people[1]})
        self.assertEqual(self.selector.lock_source,'audio_sector')
        for _ in range(12):self.assertEqual(self.tick(),1)

    def test_audio_off_restores_pure_visual_largest_face_policy(self):
        self.confirm()
        for _ in range(12):selected=self.tick(disabled=True)
        self.assertEqual(selected,2)

    def test_new_confirmed_visible_speaker_can_still_replace_retained_speaker(self):
        self.confirm()
        for _ in range(16):selected=self.tick(30.)
        self.assertEqual(selected,2)

    def test_actual_visual_loss_releases_retained_speaker(self):
        self.confirm()
        for _ in range(12):selected=self.tick(people={2:self.people[2]})
        self.assertEqual(selected,2)

    def test_offscreen_audio_does_not_replace_a_visible_confirmed_person(self):
        self.confirm()
        for _ in range(30):self.assertEqual(self.tick(80.),1)


if __name__=='__main__':unittest.main()
