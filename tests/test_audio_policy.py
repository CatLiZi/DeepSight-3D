from dataclasses import replace
from pathlib import Path
import tempfile,time,unittest
from unittest.mock import Mock
import numpy as np
import test_dominant_sound as runtime_fixtures
from locator_demo.audio import AudioDirection,AudioEstimate
from locator_demo.audio_policy import AudioDecisionPolicy,DEFAULT_AUDIO_POLICY,admit_audio,explain_audio_decision
from locator_demo.audio_motion import qualify_audio_during_motion
from locator_demo.av_selector import AudioVisualSelector
from locator_demo.dominant_sound import DominantSoundLock
from locator_demo.ear_localization import EarPairConfig,EarPairLocalizer
from locator_demo.pair_fusion import FourMicLocalizer,usable
from locator_demo.array_audio import WasapiArrayAudio
from locator_demo.speech_continuity import SpeechContinuityLocalizer
from locator_demo.settings import AudioProcessingConfig,SavedSettings,save_settings,load_settings


def voice(**kwargs):
    values=dict(direction=AudioDirection.LEFT,confidence=.8,tdoa_s=.0001,energy=.3,
        azimuth_deg=20.,speech_confidence=.7,doa_confidence=.7,noise_state='speech_like',
        observed_monotonic=10.,sample_sequence=1,temporal_stability=1.,azimuth_std_deg=8.)
    values.update(kwargs);return AudioEstimate(**values)


class PolicyTests(unittest.TestCase):
    def test_old_low_thresholds_are_normalized_not_silently_ignored(self):
        c=AudioProcessingConfig(speech_confidence_threshold=.15,doa_confidence_threshold=.05,audio_confidence_threshold=.1)
        self.assertEqual((c.speech_confidence_threshold,c.doa_confidence_threshold,c.audio_confidence_threshold),(.5,.2,.2))

    def test_settings_round_trip_canonical_thresholds(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'settings.json';save_settings(p,SavedSettings(audio=AudioProcessingConfig(speech_confidence_threshold=.15)))
            self.assertEqual(load_settings(p).audio.speech_confidence_threshold,.5)

    def test_stage_thresholds_share_one_configuration(self):
        p=AudioDecisionPolicy.from_config(AudioProcessingConfig(speech_confidence_threshold=.8,doa_confidence_threshold=.6))
        self.assertAlmostEqual(p.detection_speech,.75);self.assertEqual(p.fusion_doa,.6)
        self.assertEqual(p.describe()['speech'],.8)

    def test_reliability_floors_cannot_be_lowered(self):
        for values in (dict(speech=.2),dict(confidence=.1),dict(doa=.05),dict(freshness_s=3.),dict(max_spread_deg=90.)):
            with self.assertRaises(ValueError):AudioDecisionPolicy(**values)

    def test_configured_speech_requirement_matches_all_consumers(self):
        p=AudioDecisionPolicy(speech=.8);a=voice()
        admitted,reason=admit_audio(a,p);self.assertEqual(admitted.direction,AudioDirection.UNKNOWN)
        self.assertEqual(reason,'low_speech_probability');self.assertFalse(usable(a,p))
        selector=AudioVisualSelector();selector.policy=p
        self.assertEqual(selector._audio_validity(a,10.1,10.)[1],reason)
        self.assertIsNone(DominantSoundLock._quality(a,10.1,p))
        self.assertEqual(qualify_audio_during_motion(a,active=True,policy=p)[0].direction,AudioDirection.UNKNOWN)

    def test_raise_direction_threshold_rejects_consistently(self):
        p=AudioDecisionPolicy(doa=.8);a=voice()
        self.assertEqual(admit_audio(a,p)[1],'low_direction_confidence')
        selector=AudioVisualSelector();selector.policy=p
        self.assertFalse(selector._audio_validity(a,10.1,10.)[0]);self.assertFalse(usable(a,p))

    def test_high_quality_voice_remains_usable_while_moving(self):
        a=voice(speech_confidence=.95)
        p=AudioDecisionPolicy(speech=.9)
        self.assertIs(admit_audio(a,p)[0],a)
        self.assertIs(qualify_audio_during_motion(a,active=True,policy=p)[0],a)

    def test_uncertain_detection_is_not_permission_to_turn(self):
        self.assertLess(DEFAULT_AUDIO_POLICY.detection_speech,DEFAULT_AUDIO_POLICY.speech)
        self.assertEqual(admit_audio(voice(speech_confidence=.47))[0].direction,AudioDirection.UNKNOWN)

    def test_invalid_probability_does_not_pass_comparisons(self):
        for x in (float('nan'),float('inf'),True):
            self.assertEqual(admit_audio(voice(speech_confidence=x))[0].direction,AudioDirection.UNKNOWN)

    def test_none_and_unknown_do_not_create_a_direction(self):
        self.assertIsNone(admit_audio(None)[0]);a=voice(direction=AudioDirection.UNKNOWN)
        self.assertIs(admit_audio(a)[0],a)

    def test_policy_reaches_all_independent_four_mic_paths(self):
        p=AudioDecisionPolicy(speech=.85)
        local=SpeechContinuityLocalizer(FourMicLocalizer(EarPairLocalizer(EarPairConfig('ear_pair',2,3,True,.1355),policy=p),EarPairConfig('ear_pair',1,4,True,.0755)))
        for stream in (local.baseline,local.candidate):
            for pair in (stream.ears,stream.assisted_ears,stream.body):self.assertEqual(pair.policy,p)

    def test_raising_speech_requirement_does_not_learn_strong_voice_as_noise(self):
        local=EarPairLocalizer(EarPairConfig('ear_pair',2,3,True,.1355),policy=AudioDecisionPolicy(speech=.9))
        pcm=np.random.default_rng(42).integers(-1000,1000,(9216,4),dtype=np.int16)
        local.estimate(pcm,speech_probability=.7);self.assertEqual(local.spectral.noise_windows,0)

    def test_confirmation_hits_apply_to_selector_without_removing_dwell(self):
        selector=AudioVisualSelector();selector.policy=AudioDecisionPolicy(required_hits=5)
        for i in range(4):
            a=voice(sample_sequence=i+1,observed_monotonic=10.+i*.1)
            self.assertFalse(selector._confirm_audio_candidate(1,a,10.+i*.1,True))
        self.assertTrue(selector._confirm_audio_candidate(1,voice(),10.4,True))

    def test_reconfiguring_capture_reuses_device_but_resets_all_policy_histories(self):
        source=WasapiArrayAudio(ear_config=EarPairConfig('ear_pair',2,3,True,.1355),
            body_config=EarPairConfig('ear_pair',1,4,True,.0755))
        capture=source.capture;p=AudioDecisionPolicy(speech=.85,doa=.6)
        source.set_decision_policy(p)
        self.assertIs(source.capture,capture)
        self.assertEqual(source.four_mic.ears.policy,p);self.assertEqual(source.four_mic.body.policy,p)
        self.assertEqual(source._estimate.direction,AudioDirection.UNKNOWN)


class ExplanationTests(unittest.TestCase):
    def explain(self,**overrides):
        args=dict(audio=voice(),raw=voice(),capture={'speech_vad':{'speech_probability':.7}},
            policy=DEFAULT_AUDIO_POLICY,running=True,audio_enabled=True,visual_enabled=True,
            moving=False,fault='',core={'locked':False},now=10.1)
        args.update(overrides);return explain_audio_decision(**args)

    def test_reliable_audio_does_not_hide_paused_controls(self):
        r=self.explain(running=False);self.assertTrue(r['direction']['usable']);self.assertEqual(r['control']['code'],'paused')

    def test_sensor_voice_detection_and_action_admission_are_separate(self):
        r=self.explain(audio=voice(speech_confidence=.47),capture={'speech_vad':{'speech_probability':.47}})
        self.assertTrue(r['voice']['detected']);self.assertFalse(r['direction']['usable'])

    def test_stale_direction_cannot_look_ready(self):
        r=self.explain(now=12.);self.assertFalse(r['direction']['usable']);self.assertEqual(r['direction']['reason'],'stale_audio')

    def test_invalid_capture_is_explained(self):
        r=self.explain(capture={'ok':False});self.assertEqual(r['direction']['reason'],'capture_unhealthy')

    def test_fault_has_priority_over_voice(self):
        self.assertEqual(self.explain(fault='missing feedback')['control']['code'],'servo_fault')

    def test_visual_priority_is_explained_without_dropping_acoustic_display(self):
        r=self.explain(core={'motion_reason':'visual_target_has_priority'});self.assertTrue(r['direction']['usable'])
        self.assertEqual(r['control']['code'],'visual_priority')

    def test_short_silence_does_not_claim_existing_motion_must_stop(self):
        r=self.explain(audio=voice(direction=AudioDirection.UNKNOWN),moving=True,core={'motion_authorized':True,'locked':True})
        self.assertEqual(r['control']['code'],'finishing_target')

    def test_target_reached_and_search_limits_have_specific_reasons(self):
        for reason,code in [('core_goal_reached','target_reached'),('step_budget_reached','search_budget'),('yaw_limit','yaw_limit')]:
            self.assertEqual(self.explain(steering_reason=reason)['control']['code'],code)

    def test_missing_or_invalid_angle_scale_has_specific_control_reason(self):
        for reason,code in [('yaw_angle_calibration_missing','yaw_geometry_missing'),
                            ('invalid_pose_model','invalid_yaw_geometry')]:
            result=self.explain(steering_reason=reason)
            self.assertTrue(result['direction']['usable'])
            self.assertEqual(result['control']['code'],code)

    def test_staged_silent_completion_does_not_request_new_voice(self):
        result=self.explain(audio=voice(direction=AudioDirection.UNKNOWN),moving=False,
            core={'locked':True,'motion_authorized':True},steering_reason='core_step_submitted')
        self.assertFalse(result['direction']['usable'])
        self.assertEqual(result['control']['code'],'finishing_target')

    def test_stopped_state_wins_over_a_previous_staged_message(self):
        result=self.explain(running=False,core={'locked':True,'motion_authorized':True},
                            steering_reason='core_step_submitted')
        self.assertEqual(result['control']['code'],'paused')

    def test_disabled_audio_and_core_confirmation_are_distinguished(self):
        self.assertEqual(self.explain(audio_enabled=False)['control']['code'],'audio_control_disabled')
        self.assertEqual(self.explain()['control']['code'],'confirming_core')

    def test_window_voice_support_does_not_disappear_behind_low_instantaneous_tail(self):
        r=self.explain(audio=voice(direction=AudioDirection.UNKNOWN,noise_state='no_voice_activity'),
            capture={'speech_vad':{'speech_probability':.1,'analysis_window_speech_support':.8}})
        self.assertTrue(r['voice']['detected']);self.assertFalse(r['direction']['usable'])
        self.assertEqual(r['direction']['reason'],'voice_detected_without_direction')

    def test_body_voice_without_coordinate_conversion_is_explained(self):
        r=self.explain(audio=voice(direction=AudioDirection.UNKNOWN),capture={'speech_vad':{'speech_probability':.9},
            'pairs':{'body':{'usable':True},'ears':{'usable':False}},'fusion':{'reason':'head_body_pose_unavailable'}})
        self.assertEqual(r['direction']['reason'],'body_pose_unavailable')


class RuntimePolicyTests(unittest.TestCase):
    def setUp(self):
        self.fixture=runtime_fixtures.CoreRuntimeTests();self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_high_configured_speech_requirement_blocks_stationary_core_and_motion(self):
        f=self.fixture;f.runtime.audio_processing=replace(f.runtime.audio_processing,speech_confidence_threshold=.95)
        for _ in range(16):state=f.tick_voice()
        self.assertFalse(state['audio']['core_source']['locked']);self.assertFalse(f.commands)
        self.assertEqual(state['audio']['decision']['direction']['reason'],'low_speech_probability')

    def test_high_requirement_also_applies_while_head_is_moving(self):
        f=self.fixture;f.runtime.audio_processing=replace(f.runtime.audio_processing,speech_confidence_threshold=.95)
        f.head.is_moving=True
        for _ in range(8):state=f.tick_voice()
        self.assertFalse(f.commands);self.assertFalse(state['audio']['core_source']['locked'])

    def test_default_requirements_keep_original_valid_motion(self):
        f=self.fixture
        for _ in range(16):state=f.tick_voice()
        self.assertTrue(f.commands);self.assertTrue(state['audio']['core_source']['locked'])


if __name__=='__main__':unittest.main()
