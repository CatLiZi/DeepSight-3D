"""Reliability and lifecycle checks independent of recorded position labels."""
from dataclasses import replace
import unittest
from unittest.mock import patch

import numpy as np

from locator_demo.audio import AudioDirection
from locator_demo.ear_localization import EarPairConfig, EarPairLocalizer
from locator_demo.spectral_delay import DelayEvidence, ReliabilityWeightedDelay


CONFIG = EarPairConfig('ear_pair', 2, 3, True, .1355)


def signal(delay=9., polarity=1., n=9216, seed=613):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=n+160)
    freq = np.fft.rfftfreq(len(x), 1/48000)
    x = np.fft.irfft(np.fft.rfft(x)*((freq>250)&(freq<5000)), n=len(x))
    x *= 2500/np.std(x)
    t = np.arange(n)+70
    pair = np.stack((x[t], polarity*np.interp(t-delay,np.arange(len(x)),x)),axis=1)
    pair += rng.normal(0,8,pair.shape)  # Independent sensor noise, including at zero delay.
    data = np.zeros((n,4),'<i2');data[:,[1,2]]=pair.astype('<i2')
    return data


class ReliabilityTests(unittest.TestCase):
    def test_fractional_delays_and_polarity_preserve_arrival_sign(self):
        for delay in (-13.4, -4.5, 0, 7.25, 16.2):
            for polarity in (-1,1):
                with self.subTest(delay=delay,polarity=polarity):
                    estimate=EarPairLocalizer(CONFIG).estimate(signal(delay,polarity))
                    self.assertNotEqual(estimate.direction,AudioDirection.UNKNOWN)
                    self.assertAlmostEqual(estimate.tdoa_s*48000,delay,delta=.35)

    def test_coherence_is_averaged_and_rejects_independent_ears(self):
        dsp=ReliabilityWeightedDelay()
        pair=np.random.default_rng(7).normal(0,.02,(9216,2))
        _,_,coherence,frames=dsp.spectra(pair)
        self.assertGreaterEqual(frames,10)
        self.assertLess(float(np.mean(coherence)),.2)
        self.assertIsNone(dsp.estimate(pair,maximum_lag_samples=19).lag_samples)

    def test_stationary_incoherent_frequency_interference_is_downweighted(self):
        data=signal(7.25)
        rng=np.random.default_rng(89)
        n=len(data);freq=np.fft.rfftfreq(n,1/48000)
        noise=rng.normal(size=(n,2));spectrum=np.fft.rfft(noise,axis=0)
        spectrum*=((freq>3000)&(freq<5500))[:,None]
        noise=np.fft.irfft(spectrum,n=n,axis=0)
        noise*=3500/np.std(noise)
        local=EarPairLocalizer(CONFIG)
        quiet=np.zeros_like(data);quiet[:,[1,2]]=noise.astype('<i2')
        for _ in range(4):local.estimate(quiet,speech_probability=.01)
        data[:,[1,2]]=(data[:,[1,2]].astype(float)+noise).astype('<i2')
        estimate=local.estimate(data,speech_probability=.95)
        self.assertNotEqual(estimate.direction,AudioDirection.UNKNOWN)
        self.assertAlmostEqual(estimate.tdoa_s*48000,7.25,delta=.6)
        self.assertGreater(local.diagnostics['spectral_coherence'],.6)

    def test_noise_model_updates_only_without_voice(self):
        local=EarPairLocalizer(CONFIG);pcm=signal()
        local.estimate(pcm,speech_probability=.9)
        self.assertEqual(local.spectral.noise_windows,0)
        local.estimate(pcm,speech_probability=.01)
        self.assertEqual(local.spectral.noise_windows,1)
        noise=local.spectral.noise_power.copy()
        local.estimate(signal(-8),speech_probability=.95)
        np.testing.assert_array_equal(local.spectral.noise_power,noise)

    def test_tone_is_not_broadband_delay_evidence_even_with_high_vad(self):
        t=np.arange(9216)/48000
        pcm=np.zeros((len(t),4),'<i2')
        pcm[:,1]=2500*np.sin(2*np.pi*700*t)
        pcm[:,2]=2500*np.sin(2*np.pi*700*(t-.00015))
        estimate=EarPairLocalizer(CONFIG).estimate(pcm,speech_probability=.99)
        self.assertEqual(estimate.noise_state,'narrowband_tone')
        self.assertEqual(estimate.direction,AudioDirection.UNKNOWN)

    def test_outside_physical_delay_is_rejected_without_clamping(self):
        for delay in (-26,26):
            with self.subTest(delay=delay):
                estimate=EarPairLocalizer(CONFIG).estimate(signal(delay))
                self.assertEqual(estimate.direction,AudioDirection.UNKNOWN)
                self.assertEqual(estimate.noise_state,'invalid_delay')

    def test_delay_offset_is_applied_before_angle_conversion(self):
        local=EarPairLocalizer(replace(CONFIG,delay_offset_us=2/48000*1e6))
        estimate=local.estimate(signal(10))
        self.assertAlmostEqual(estimate.tdoa_s*48000,8,delta=.35)
        expected=np.degrees(np.arcsin((8/48000)/(.1355/343)))
        self.assertAlmostEqual(estimate.azimuth_deg,expected,delta=1)

    def test_invalid_vad_never_becomes_direction(self):
        for probability in (float('nan'),float('inf'),-.1,1.1):
            with self.subTest(probability=probability):
                self.assertEqual(EarPairLocalizer(CONFIG).estimate(signal(),speech_probability=probability).direction,AudioDirection.UNKNOWN)

    def test_body_channels_never_enter_noise_learning_or_delay(self):
        a=signal();b=a.copy();b[:,0]=32767;b[:,3]=-32768
        left,right=EarPairLocalizer(CONFIG),EarPairLocalizer(CONFIG)
        left.estimate(a,speech_probability=.01);right.estimate(b,speech_probability=.01)
        np.testing.assert_array_equal(left.spectral.noise_power,right.spectral.noise_power)
        self.assertEqual(left.estimate(signal(-9),speech_probability=.9),right.estimate(signal(-9),speech_probability=.9))


class TemporalEvidenceTests(unittest.TestCase):
    def test_first_candidate_is_inspectable_but_cannot_trigger_motion(self):
        local=EarPairLocalizer(CONFIG)
        first=local.estimate(signal(),speech_probability=.99)
        self.assertEqual(first.direction,AudioDirection.LEFT)
        self.assertEqual(first.confidence,0)
        second=local.estimate(signal(),speech_probability=.99)
        self.assertGreater(second.confidence,.2)

    def test_short_quality_rejection_returns_unknown_without_erasing_candidate(self):
        local=EarPairLocalizer(CONFIG)
        local.estimate(signal());local.estimate(signal())
        with patch.object(local.spectral,'estimate',return_value=DelayEvidence(None,'ambiguous_delay')):
            rejected=local.estimate(signal())
        self.assertEqual(rejected.direction,AudioDirection.UNKNOWN)
        self.assertEqual(rejected.confidence,0)
        self.assertFalse(local.diagnostics['signal_valid'])
        self.assertEqual(len(local._delays),2)
        self.assertGreater(local.estimate(signal()).confidence,.2)

    def test_three_rejections_or_capture_failure_clear_evidence(self):
        local=EarPairLocalizer(CONFIG);local.estimate(signal())
        with patch.object(local.spectral,'estimate',return_value=DelayEvidence(None,'low_coherence')):
            for _ in range(3):local.estimate(signal())
        self.assertFalse(local._delays)
        local.estimate(signal(),speech_probability=.01)
        self.assertIsNotNone(local.spectral.noise_power)
        local._unknown('stale_audio')
        self.assertIsNone(local.spectral.noise_power)
        self.assertEqual(local.spectral.noise_windows,0)


if __name__=='__main__':unittest.main()
