"""Known-arrival band conflicts and AV fallback; no physical device access."""
from dataclasses import replace
import json
from pathlib import Path
import unittest
import numpy as np

from locator_demo.audio import AudioDirection
from locator_demo.audio_motion import qualify_audio_during_motion
from locator_demo.ear_localization import EarPairLocalizer
from locator_demo.spectral_delay import ReliabilityWeightedDelay
from locator_demo.visual_priority import VisualPrioritySelector
from test_spectral_delay import CONFIG, signal
from test_visual_priority import face

FIXTURE = Path(__file__).parent / 'fixtures/band_delay_known_truth.npz'
MAX_LAG = .1355 / 343 * 48000


def fixture_pair(name):
    with np.load(FIXTURE, allow_pickle=False) as f:
        return f[name].copy(), float(f['truth_samples'])


def four_channels(pair):
    pcm = np.zeros((len(pair), 4), '<i2')
    pcm[:, [1, 2]] = np.rint(pair * 32768).astype('<i2')
    return pcm


class BandDelayTests(unittest.TestCase):
    def test_coherent_centred_interference_cannot_override_two_agreeing_bands(self):
        pair, truth = fixture_pair('interference')
        e = ReliabilityWeightedDelay().estimate(pair, maximum_lag_samples=MAX_LAG)
        self.assertIsNone(e.lag_samples)
        self.assertEqual(e.reason, 'ambiguous_delay')
        self.assertTrue(e.band_disagreement)
        self.assertLess(abs(e.bands[0]['lag_samples']), 1.)
        for band in e.bands[1:]:
            self.assertTrue(band['qualified'])
            self.assertAlmostEqual(band['lag_samples'], truth, delta=.25)

    def test_clean_and_polarity_inverted_arrivals_keep_their_delay(self):
        pair, truth = fixture_pair('clean')
        for sign in (-1, 1):
            with self.subTest(sign=sign):
                e = ReliabilityWeightedDelay().estimate(pair * [1, sign], maximum_lag_samples=MAX_LAG)
                self.assertAlmostEqual(e.lag_samples, truth, delta=.1)
                self.assertFalse(e.band_disagreement)

    def test_existing_peak_ambiguity_is_not_attributed_to_the_new_guard(self):
        clean, _ = fixture_pair('clean')
        interference, _ = fixture_pair('interference')
        moderate = clean + (interference - clean) / 2
        e = ReliabilityWeightedDelay().estimate(moderate, maximum_lag_samples=MAX_LAG)
        self.assertIsNone(e.lag_samples)
        self.assertLess(e.peak_ratio, 1.15)
        self.assertFalse(e.band_disagreement)

    def test_reversing_ears_reverses_every_band_and_keeps_abstention(self):
        pair, _ = fixture_pair('interference')
        a = ReliabilityWeightedDelay().estimate(pair, maximum_lag_samples=MAX_LAG)
        b = ReliabilityWeightedDelay().estimate(pair[:, ::-1], maximum_lag_samples=MAX_LAG)
        self.assertTrue(a.band_disagreement and b.band_disagreement)
        self.assertEqual(a.reason, b.reason)
        for x, y in zip(a.bands, b.bands):
            self.assertAlmostEqual(x['lag_samples'], -y['lag_samples'], places=8)

    def test_one_supported_upper_band_alone_is_insufficient_to_veto(self):
        pair, _ = fixture_pair('interference')
        freq = np.fft.rfftfreq(len(pair), 1 / 48000)
        spectrum = np.fft.rfft(pair, axis=0)
        spectrum[freq >= 2500] *= 1e-5
        limited = np.fft.irfft(spectrum, n=len(pair), axis=0)
        e = ReliabilityWeightedDelay().estimate(limited, maximum_lag_samples=MAX_LAG)
        self.assertFalse(e.bands[2]['qualified'])
        self.assertFalse(e.band_disagreement)

    def test_silence_and_tone_do_not_gain_band_or_direction_evidence(self):
        t = np.arange(9216) / 48000
        for pair in (np.zeros((9216, 2)), .03*np.column_stack((np.sin(2*np.pi*700*t), np.sin(2*np.pi*700*(t-.0002))))):
            e = ReliabilityWeightedDelay().estimate(pair, maximum_lag_samples=MAX_LAG)
            self.assertIsNone(e.lag_samples)
            self.assertFalse(e.band_disagreement)

    def test_band_diagnostics_are_finite_and_json_serializable(self):
        pair, _ = fixture_pair('interference')
        e = ReliabilityWeightedDelay().estimate(pair, maximum_lag_samples=MAX_LAG)
        json.dumps(e.bands, allow_nan=False)
        for band in e.bands:
            for name in ('coherence', 'correlation', 'weight_fraction'):
                self.assertGreaterEqual(band[name], 0.)
                self.assertLessEqual(band[name], 1.)

    def test_ear_localizer_exposes_fresh_conflict_without_a_replacement_bearing(self):
        pair, _ = fixture_pair('interference')
        local = EarPairLocalizer(CONFIG)
        e = local.estimate(four_channels(pair), speech_probability=.99, sample_end=9216)
        self.assertEqual(e.direction, AudioDirection.UNKNOWN)
        self.assertEqual(e.confidence, 0.)
        self.assertEqual(e.azimuth_deg, 0.)
        self.assertTrue(local.diagnostics['frequency_disagreement'])
        self.assertTrue(local.diagnostics['frequency_evidence_current'])

    def test_capture_gap_clears_old_frequency_evidence(self):
        pair, _ = fixture_pair('interference')
        local = EarPairLocalizer(CONFIG)
        local.estimate(four_channels(pair), speech_probability=.99, sample_end=9216)
        local._unknown('stale_audio')
        self.assertFalse(local.diagnostics['frequency_disagreement'])
        self.assertFalse(local.diagnostics['frequency_evidence_current'])

    def test_speech_end_does_not_keep_the_old_conflict_current(self):
        pair, _ = fixture_pair('interference')
        local = EarPairLocalizer(CONFIG)
        local.estimate(four_channels(pair), speech_probability=.99, sample_end=9216)
        local.estimate(signal(), speech_probability=.01, sample_end=10176)
        self.assertFalse(local.diagnostics['frequency_disagreement'])
        self.assertFalse(local.diagnostics['frequency_evidence_current'])

    def test_body_channels_are_excluded_from_new_band_logic(self):
        pair, _ = fixture_pair('interference')
        a = four_channels(pair)
        b = a.copy(); b[:, 0] = 32767; b[:, 3] = -32768
        x, y = EarPairLocalizer(CONFIG), EarPairLocalizer(CONFIG)
        self.assertEqual(x.estimate(a, speech_probability=.99), y.estimate(b, speech_probability=.99))
        self.assertEqual(x.diagnostics['frequency_reliability'], y.diagnostics['frequency_reliability'])

    def test_band_conflict_preserves_its_reason_during_movement(self):
        pair, _ = fixture_pair('interference')
        e = EarPairLocalizer(CONFIG).estimate(four_channels(pair), speech_probability=.99)
        admitted, context = qualify_audio_during_motion(e, active=True)
        self.assertEqual(admitted.direction, AudioDirection.UNKNOWN)
        self.assertEqual(admitted.noise_state, 'ambiguous_delay')
        self.assertFalse(context['admitted'])
        self.assertFalse(admitted.motor_suppressed)

    def test_visual_priority_continues_when_frequency_evidence_conflicts(self):
        pair, _ = fixture_pair('interference')
        local = EarPairLocalizer(CONFIG)
        selector = VisualPrioritySelector()
        people = {1: [face(1, 10, width=120)], 2: [face(2, 70, width=60)]}
        for n in range(8):
            e = local.estimate(four_channels(pair), speech_probability=.99, sample_end=9216+n*960)
            e = replace(e, speech_confidence=.99, observed_monotonic=10+n*.1, sample_sequence=n+1)
            winner = selector.update(people, now=10+n*.1, size=(640, 360), audio=e)
            self.assertEqual(winner, 1)
            self.assertFalse(selector.metrics['audio_usable'])
            self.assertTrue(all(c['score_mode'] == 'visual_only' for c in selector.metrics['candidates']))


if __name__ == '__main__':
    unittest.main()
