import unittest
from types import SimpleNamespace
import numpy as np
from locator_demo.speech_vad import StreamingSpeechVAD,update_window_probability
from locator_demo.ear_localization import EarPairConfig,EarPairLocalizer
from locator_demo.pair_fusion import FourMicLocalizer
from locator_demo.speech_continuity import SpeechContinuityLocalizer
from locator_demo.audio import AudioEstimate,AudioDirection
from unittest.mock import Mock


class WindowSpeechTests(unittest.TestCase):
    def vad(self,scores):
        vad=StreamingSpeechVAD.__new__(StreamingSpeechVAD)
        vad.sample_rate=16000;vad.factor=1;vad.taps=np.ones(1)
        iterator=iter(scores)
        vad.session=SimpleNamespace(run=lambda *args:(np.full((2,1),next(iterator),np.float32),vad.state))
        vad.calls=0;vad.total_ms=0.;vad.reset()
        return vad

    def test_tail_dip_keeps_only_measured_support_in_same_window(self):
        vad=self.vad([.99,0,0,0])
        tail=vad.update(np.zeros((2048,2)),sample_start=0)
        self.assertLess(tail,.1)
        self.assertGreater(vad.probability_for_window(sample_start=0,sample_end=2048),.98)

    def test_voice_expires_when_pcm_window_leaves_voiced_chunks(self):
        vad=self.vad([.99]+[0]*11)
        vad.update(np.zeros((2048,2)),sample_start=0)
        vad.update(np.zeros((3072,2)),sample_start=2048)
        self.assertLess(vad.probability_for_window(sample_start=2048,sample_end=5120),.05)

    def test_stale_future_overlong_or_invalid_window_cannot_reuse_voice(self):
        vad=self.vad([.99]*4);vad.update(np.zeros((2048,2)),sample_start=0)
        for start,end in [(0,2049),(0,1024),(-1,2048),(True,2048),(2048,2048),(-2000,2048)]:
            self.assertEqual(vad.probability_for_window(sample_start=start,sample_end=end),0.)

    def test_reset_clears_voice(self):
        vad=self.vad([.99]*4);vad.update(np.zeros((2048,2)),sample_start=0)
        vad.reset();self.assertFalse(vad._window_evidence)

    def test_capture_gap_does_not_bridge_old_voice(self):
        vad=self.vad([.99,0]);vad.update(np.zeros((512,2)),sample_start=0)
        vad.update(np.zeros((512,2)),sample_start=2000)
        self.assertEqual(vad.probability_for_window(sample_start=2000,sample_end=2512),0.)

    def test_duplicate_update_adds_no_evidence(self):
        vad=self.vad([.99]);pcm=np.zeros((512,2));vad.update(pcm,sample_start=0)
        count=len(vad._window_evidence);vad.update(pcm,sample_start=0)
        self.assertEqual(len(vad._window_evidence),count)

    def test_unavailable_model_stays_explicit(self):
        vad=self.vad([]);vad.session=None
        self.assertIsNone(update_window_probability(vad,np.zeros((512,2)),sample_start=0))

    def test_callback_boundaries_have_identical_pcm_clock_evidence(self):
        a=self.vad([.9,.1,.3,.05]);b=self.vad([.9,.1,.3,.05])
        a.update(np.zeros((2048,2)),sample_start=0)
        for start in range(0,2048,256):b.update(np.zeros((256,2)),sample_start=start)
        self.assertEqual(list(a._window_evidence),list(b._window_evidence))

    def test_uncertain_voice_does_not_train_noise_or_emit_direction(self):
        local=EarPairLocalizer(EarPairConfig('ear_pair',2,3,True,.1355),protect_uncertain_noise=True)
        pcm=np.random.default_rng(9).integers(-1000,1000,(9216,4),dtype=np.int16)
        for p in (.11,.2,.44):
            result=local.estimate(pcm,speech_probability=p)
            self.assertEqual(result.direction.value,'unknown')
        self.assertEqual(local.spectral.noise_windows,0)
        local.estimate(pcm,speech_probability=.01)
        self.assertEqual(local.spectral.noise_windows,1)


class ContinuitySelectionTests(unittest.TestCase):
    def setUp(self):
        baseline=FourMicLocalizer(EarPairLocalizer(EarPairConfig('ear_pair',2,3,True,.1355)),
            EarPairConfig('ear_pair',1,4,True,.0755))
        self.local=SpeechContinuityLocalizer(baseline)
        self.good=AudioEstimate(AudioDirection.LEFT,.8,0,.3,25,.9,.8,2.,'speech_like',
            temporal_stability=1.,azimuth_std_deg=8.)
        self.bad=AudioEstimate(AudioDirection.UNKNOWN,0,0,0)

    def run_estimate(self,old,new):
        self.local.baseline.estimate=Mock(return_value=old)
        self.local.candidate.estimate=Mock(return_value=new)
        return self.local.estimate(np.zeros((9216,4)),speech_probability=.9,
            body_speech_probability=.8,instantaneous_speech_probability=.02,
            instantaneous_body_probability=.03,sample_end=9216)

    def test_usable_baseline_is_preserved_even_when_candidate_differs(self):
        self.assertIs(self.run_estimate(self.good,self.bad),self.good)
        self.assertEqual(self.local.baseline.estimate.call_args.kwargs['speech_probability'],.02)

    def test_current_window_can_fill_missing_baseline(self):
        self.assertIs(self.run_estimate(self.bad,self.good),self.good)
        self.assertIs(self.local.selected,self.local.candidate)

    def test_uncertain_candidate_does_not_fill_gap(self):
        self.assertIs(self.run_estimate(self.bad,self.bad),self.bad)

    def test_noise_references_and_delay_tracks_are_independent(self):
        for name in ('ears','assisted_ears','body'):
            a=getattr(self.local.baseline,name);b=getattr(self.local.candidate,name)
            self.assertIsNot(a.spectral,b.spectral)
            self.assertFalse(a.protect_uncertain_noise)
            self.assertTrue(b.protect_uncertain_noise)

    def test_capture_fault_resets_both_histories(self):
        self.local.baseline._unknown=Mock(return_value=self.bad)
        self.local.candidate._unknown=Mock(return_value=self.bad)
        self.local._unknown('audio_gap')
        self.local.baseline._unknown.assert_called_once_with('audio_gap')
        self.local.candidate._unknown.assert_called_once_with('audio_gap')


if __name__=='__main__':unittest.main()
