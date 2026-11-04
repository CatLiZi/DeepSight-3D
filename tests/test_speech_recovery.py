import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock,patch
import numpy as np

from locator_demo.audio import AudioDirection,AudioEstimate
from locator_demo.audio_policy import AudioDecisionPolicy,explain_audio_decision
from locator_demo.ear_localization import EarPairConfig,EarPairLocalizer
from locator_demo.pair_fusion import FourMicLocalizer
from locator_demo.speech_continuity import SpeechContinuityLocalizer
from locator_demo.speech_recovery import BoundedContextLocalizer,spectral_shape_change
from locator_demo.array_audio import WasapiArrayAudio


class FakeVAD:
    def __init__(self,**kwargs):
        self.session=object();self.probability=.95;self.resets=0;self.last_end=None
    def reset(self):self.resets+=1;self.last_end=None
    def update(self,pcm,*,sample_start):
        self.last_end=sample_start+len(pcm);return self.probability


def good():return AudioEstimate(AudioDirection.LEFT,.8,.0001,.3,25,.95,.8,2.,'speech_like',temporal_stability=1.,azimuth_std_deg=8.)
BAD=AudioEstimate(AudioDirection.UNKNOWN,0,0,0,noise_state='no_voice_activity')


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        base=SpeechContinuityLocalizer(FourMicLocalizer(EarPairLocalizer(EarPairConfig('ear_pair',2,3,True,.1355)),
            EarPairConfig('ear_pair',1,4,True,.0755)))
        self.local=BoundedContextLocalizer(base,vad_factory=FakeVAD)
        self.local.baseline.estimate=Mock(return_value=BAD)
        self.local.recovery.estimate=Mock(return_value=good())
        self.local.recovery.last_ear=good()
        self.pcm=np.random.default_rng(184).integers(-1000,1000,(9216,4),dtype=np.int16)
        self.shape=patch('locator_demo.speech_recovery.spectral_shape_change',return_value=.2)
        self.shape.start();self.addCleanup(self.shape.stop)

    def step(self,end):return self.local.estimate(self.pcm,speech_probability=.05,body_speech_probability=.04,
        instantaneous_speech_probability=.01,instantaneous_body_probability=.02,sample_end=end,observed_monotonic=end/48000)

    def test_reliable_primary_identity_is_preserved(self):
        original=good();self.local.baseline.estimate.return_value=original
        for end in (9216,11136,13056):self.assertIs(self.step(end),original)
        self.assertEqual(self.local.recovered,0)

    def test_three_fresh_windows_and_elapsed_span_required(self):
        self.assertIs(self.step(9216),BAD);self.assertIs(self.step(11136),BAD)
        self.assertIs(self.step(13056),BAD)
        self.assertIsNot(self.step(14976),BAD);self.assertTrue(self.local.used_for_output)

    def test_rapid_polls_cannot_shortcut_confirmation_time(self):
        for end in (9216,9696,10176,10656):self.assertIs(self.step(end),BAD)

    def test_duplicate_pcm_does_not_confirm(self):
        for _ in range(5):self.assertIs(self.step(9216),BAD)
        self.assertFalse(self.local.used_for_output)

    def test_configuration_can_require_more_than_three_observations(self):
        self.local.policy=AudioDecisionPolicy(required_hits=5)
        for end in (9216,11136,13056,14976):self.assertIs(self.step(end),BAD)
        self.assertIsNot(self.step(16896),BAD)

    def test_one_invalid_candidate_restarts_confirmation(self):
        self.step(9216);self.step(11136)
        self.local.recovery.estimate.return_value=BAD;self.step(13056)
        self.local.recovery.estimate.return_value=good()
        self.assertIs(self.step(14976),BAD);self.assertIs(self.step(16896),BAD)

    def test_primary_evidence_resets_auxiliary_confirmation(self):
        self.step(9216);self.step(11136)
        self.local.baseline.estimate.return_value=good();self.step(13056)
        self.local.baseline.estimate.return_value=BAD
        self.assertIs(self.step(14976),BAD)

    def test_new_angle_cannot_borrow_old_confirmation(self):
        self.step(9216);self.step(11136)
        self.local.recovery.estimate.return_value=replace(good(),azimuth_deg=-25)
        self.assertIs(self.step(13056),BAD)

    def test_stationary_spectrum_cannot_create_recovery(self):
        with patch('locator_demo.speech_recovery.spectral_shape_change',return_value=.005):
            for end in (9216,11136,13056,14976):self.assertIs(self.step(end),BAD)
        self.assertEqual(self.local.metrics()['context_recovery']['state'],'stationary_spectrum')

    def test_weak_auxiliary_voice_cannot_use_primary_point5_floor(self):
        self.local.recovery.estimate.return_value=replace(good(),speech_confidence=.65)
        for end in (9216,11136,13056,14976):self.assertIs(self.step(end),BAD)

    def test_raised_user_policy_is_not_weakened(self):
        self.assertEqual(AudioDecisionPolicy(speech=.9).context_recovery_speech,.9)
        self.local.policy=self.local.recovery_policy=AudioDecisionPolicy(speech=.98)
        for end in (9216,11136,13056):self.assertIs(self.step(end),BAD)

    def test_context_renewal_does_not_reset_primary_or_synthesize_pcm(self):
        self.step(8*48000-1920);self.step(8*48000)
        self.assertEqual(self.local.renewals,1)
        self.assertEqual(self.local.voices[0].resets,1)
        self.assertEqual(self.local.baseline.estimate.call_count,2)
        np.testing.assert_array_equal(self.local.baseline.estimate.call_args.args[0],self.pcm)

    def test_original_instantaneous_scores_are_preserved(self):
        self.step(9216)
        kw=self.local.baseline.estimate.call_args.kwargs
        self.assertEqual(kw['instantaneous_speech_probability'],.01)
        self.assertNotIn('instantaneous_speech_probability',self.local.recovery.estimate.call_args.kwargs)

    def test_alternate_context_renews_half_period_apart(self):
        self.step(4*48000-1920);self.step(4*48000)
        self.assertEqual(self.local.voices[0].resets,0)
        self.assertEqual(self.local.alternate_voices[0].resets,1)

    def test_one_context_dip_does_not_erase_other_current_evidence(self):
        for model in self.local.voices:model.probability=.01
        self.step(9216);self.step(11136);self.step(13056);self.step(14976)
        self.assertTrue(self.local.used_for_output)
        self.assertEqual(self.local._scores,[.95,.95])

    def test_missing_model_has_no_recovery_or_native_dependency(self):
        self.local.voices[0].session=None
        for end in (9216,11136,13056):self.assertIs(self.step(end),BAD)
        self.local.recovery.estimate.assert_not_called()

    def test_discontinuity_clears_confirmations(self):
        self.step(9216);self.step(11136)
        self.assertIs(self.step(40000),BAD);self.assertGreater(self.local.voices[0].resets,0)

    def test_reset_clears_auxiliary_scores_and_history(self):
        self.step(9216);self.local._unknown('audio_gap')
        self.assertEqual(self.local._scores,[0.,0.]);self.assertEqual(self.local._hits,0)

    def test_invalid_clock_cannot_run_auxiliary_inference(self):
        self.local.estimate(self.pcm,sample_end=True)
        self.local.recovery.estimate.assert_not_called()

    def test_recording_toggle_does_not_rebuild_recovery(self):
        source=WasapiArrayAudio(ear_config=EarPairConfig('ear_pair',2,3,True,.1355),
            body_config=EarPairConfig('ear_pair',1,4,True,.0755),speech_vad=True)
        lane=source.four_mic;voices=lane.voices
        source.set_recording_enabled(True)
        self.assertIs(source.four_mic,lane);self.assertIs(source.four_mic.voices,voices)

    def test_vad_toggle_unwraps_and_rebuilds_exactly_one_recovery_lane(self):
        source=WasapiArrayAudio(ear_config=EarPairConfig('ear_pair',2,3,True,.1355),
            body_config=EarPairConfig('ear_pair',1,4,True,.0755),speech_vad=True)
        capture=source.capture
        source.set_vad_enabled(False);self.assertIsInstance(source.four_mic,FourMicLocalizer)
        source.set_vad_enabled(True);self.assertIsInstance(source.four_mic,BoundedContextLocalizer)
        self.assertIsInstance(source.four_mic.baseline,SpeechContinuityLocalizer);self.assertIs(source.capture,capture)

    def test_live_configuration_reaches_all_recovery_pairs(self):
        source=WasapiArrayAudio(ear_config=EarPairConfig('ear_pair',2,3,True,.1355),
            body_config=EarPairConfig('ear_pair',1,4,True,.0755),speech_vad=True)
        source.set_decision_policy(AudioDecisionPolicy(speech=.9,doa=.6))
        lane=source.four_mic
        self.assertEqual(lane.policy.speech,.9);self.assertEqual(lane.recovery_policy.speech,.9)
        for pair in (lane.recovery.ears,lane.recovery.assisted_ears,lane.recovery.body):
            self.assertEqual(pair.policy.speech,.9);self.assertEqual(pair.policy.doa,.6)


class SpectralGuardTests(unittest.TestCase):
    def test_modulated_harmonics_cannot_gain_variation_from_diffuse_noise(self):
        n=9216;t=np.arange(n)/48000
        y=sum(np.sin(2*np.pi*k*120*t+.11*k)/k for k in range(1,16))*(.5+.5*np.sin(2*np.pi*2.9*t))**2
        x=np.stack([np.roll(y*1400,k) for k in (0,4,12,6)],axis=1)
        x+=np.random.default_rng(456).normal(size=x.shape)*np.sqrt(np.mean(x*x))/np.sqrt(10)
        self.assertLess(spectral_shape_change(x,(1,2)),.01)

    def test_changing_shape_is_gain_and_dc_invariant(self):
        t=np.arange(9216)/48000
        y=np.sin(2*np.pi*(100*t+1000*t*t))*1000+np.sin(2*np.pi*900*t)*600
        pcm=np.stack([y,np.roll(y,4),np.roll(y,10),np.roll(y,18)],axis=1)
        a=spectral_shape_change(pcm,(1,2))
        self.assertGreater(a,.01)
        self.assertAlmostEqual(a,spectral_shape_change(pcm*2+50,(1,2)),places=10)

    def test_invalid_silence_and_short_windows_are_not_evidence(self):
        for x in (np.zeros((9216,4)),np.zeros((100,4)),np.full((9216,4),np.nan)):
            self.assertEqual(spectral_shape_change(x,(1,2)),0.)

    def test_auxiliary_explanation_is_read_only_and_distinguishes_pending(self):
        from locator_demo.audio_policy import DEFAULT_AUDIO_POLICY
        r=explain_audio_decision(audio=BAD,raw=BAD,capture={'context_recovery':{'voice_support':.9,'state':'confirming_fresh_recovery'}},
            policy=DEFAULT_AUDIO_POLICY,running=False,audio_enabled=True,visual_enabled=True,moving=False,fault='',core={},now=1.)
        self.assertTrue(r['voice']['detected']);self.assertEqual(r['direction']['reason'],'context_recovery_pending')
        self.assertFalse(r['direction']['usable']);self.assertEqual(r['control']['code'],'paused')


if __name__=='__main__':unittest.main()
