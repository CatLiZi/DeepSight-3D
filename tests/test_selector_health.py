"""Corrupt acoustic evidence must not steer either visual selection policy.

These tests inject invalid values at the selector boundary. They are fault
simulations, not measurements of the attached microphones or speaker identity.
"""
from dataclasses import replace
import json
import unittest

import numpy as np

from locator_demo.av_selector import AudioVisualSelector
from locator_demo.visual_priority import VisualPrioritySelector
from test_av_guidance import voice
from test_visual_priority import face


class SelectorHealthTests(unittest.TestCase):
    policies = (AudioVisualSelector, VisualPrioritySelector)

    def setUp(self):
        self.people = {1: [face(1, 25, width=140, score=.95)],
                       2: [face(2, -40, width=70, score=.8)]}

    def assert_visual_fallback(self, *, changes=None, frame_time=0.):
        for policy in self.policies:
            with self.subTest(policy=policy.__name__):
                selector = policy()
                for n in range(12):
                    now = 10. + n * .1
                    audio = replace(voice(-40, now, n + 1), **(changes or {}))
                    selected = selector.update(self.people, now=now, size=(640, 360),
                                               audio=audio, frame_time=frame_time)
                    self.assertEqual(selected, 1)
                    self.assertFalse(selector.metrics['audio_usable'])
                    self.assertEqual(selector.metrics['audio_confirmation_hits'], 0)
                    self.assertIsNone(selector.metrics['azimuth_left_deg'])
                    # Invalid measurements must not leak nonstandard NaN/Inf JSON.
                    json.dumps(selector.metrics, allow_nan=False)

    def test_nonfinite_probabilities_cannot_nominate_a_smaller_face(self):
        for field in ('speech_confidence', 'confidence', 'doa_confidence', 'temporal_stability'):
            for value in (float('nan'), float('inf'), -float('inf')):
                with self.subTest(field=field, value=value):
                    self.assert_visual_fallback(changes={field: value})

    def test_out_of_range_probabilities_are_invalid_even_above_threshold(self):
        for field in ('speech_confidence', 'confidence', 'doa_confidence', 'temporal_stability'):
            for value in (-.1, 1.2):
                with self.subTest(field=field, value=value):
                    self.assert_visual_fallback(changes={field: value})

    def test_wrong_probability_types_cannot_crash_or_coerce_into_evidence(self):
        for field in ('speech_confidence', 'confidence', 'doa_confidence', 'temporal_stability'):
            for value in (None, '0.9', True):
                with self.subTest(field=field, value=value):
                    self.assert_visual_fallback(changes={field: value})

    def test_invalid_observation_clock_returns_visual_fallback_with_safe_metrics(self):
        for value in (float('nan'), float('inf'), -float('inf'), None, '10', True):
            with self.subTest(value=value):
                self.assert_visual_fallback(changes={'observed_monotonic': value})

    def test_invalid_video_clock_cannot_bypass_synchronization_gate(self):
        for value in (float('nan'), float('inf'), -1., None, '10', True):
            with self.subTest(value=value):
                self.assert_visual_fallback(frame_time=value)

    def test_nonintegral_or_negative_sequences_cannot_confirm_a_candidate(self):
        for value in (float('nan'), float('inf'), -1, 1.2, None, '1', True):
            with self.subTest(value=value):
                self.assert_visual_fallback(changes={'sample_sequence': value})

    def test_direction_and_uncertainty_must_have_physical_finite_ranges(self):
        for field, values in (('azimuth_deg', (float('nan'), float('inf'), -91., 91., None)),
                              ('azimuth_std_deg', (float('nan'), float('inf'), -.01, 26., None))):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assert_visual_fallback(changes={field: value})

    def test_invalid_decision_clock_never_produces_usable_audio(self):
        for value in (float('nan'), float('inf'), None, True):
            for policy in self.policies:
                with self.subTest(value=value, policy=policy.__name__):
                    valid, _, age = policy()._audio_validity(voice(-40, 10., 1), value, 10.)
                    self.assertFalse(valid)
                    self.assertIsNone(age)

    def test_valid_numpy_scalars_and_unspecified_video_clock_are_preserved(self):
        for policy in self.policies:
            selector = policy()
            for n in range(15):
                now = 10. + n * .1
                audio = replace(voice(-40, now, n), sample_sequence=np.int64(n),
                                confidence=np.float64(.9))
                selected = selector.update(self.people, now=now, size=(640, 360), audio=audio)
            self.assertEqual(selected, 2)
            self.assertTrue(selector.metrics['audio_usable'])

    def test_healthy_confidence_boundaries_remain_usable(self):
        audio = replace(voice(-40, 10., 0), speech_confidence=.5,
                        confidence=.2, doa_confidence=.2, temporal_stability=.5,
                        azimuth_std_deg=25.)
        for policy in self.policies:
            self.assertTrue(policy()._audio_validity(audio, 10., 10.)[0])
            self.assertTrue(policy()._audio_validity(
                replace(audio, confidence=1., speech_confidence=1.,
                        doa_confidence=1., temporal_stability=1.), 10., 10.)[0])

    def test_invalid_audio_clears_confirmation_then_fresh_audio_can_reacquire(self):
        for policy in self.policies:
            selector = policy()
            for n in range(2):
                now = 10. + n * .1
                selector.update(self.people, now=now, size=(640, 360), audio=voice(-40, now, n))
            invalid = replace(voice(-40, 10.2, 2), doa_confidence=float('nan'))
            self.assertEqual(selector.update(self.people, now=10.2, size=(640, 360), audio=invalid), 1)
            self.assertEqual(selector.audio_hits, 0)
            self.assertEqual(selector.update(self.people, now=10.3, size=(640, 360),
                                             audio=voice(-40, 10.3, 3)), 1)
            for n in range(4, 20):
                now = 10. + n * .1
                selected = selector.update(self.people, now=now, size=(640, 360),
                                           audio=voice(-40, now, n))
            self.assertEqual(selected, 2)

    def test_corrupt_sound_does_not_create_a_target_without_a_face(self):
        for policy in self.policies:
            selector = policy()
            for n in range(12):
                now = 10. + n * .1
                audio = replace(voice(-40, now, n), confidence=float('nan'))
                self.assertIsNone(selector.update({}, now=now, size=(640, 360), audio=audio))
                self.assertFalse(selector.metrics['audio_usable'])


if __name__ == '__main__':
    unittest.main()
