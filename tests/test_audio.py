import unittest

from locator_demo.audio import (
    AudioDirection,
    AudioDirectionSmoother,
    AudioEstimate,
    classify_audio_direction,
    gcc_phat,
    gcc_phat_tdoa,
    srp_phat_front_hemisphere,
)


class AudioLogicTests(unittest.TestCase):
    def test_classifies_left_right_and_center_from_tdoa(self):
        self.assertEqual(classify_audio_direction(0.00018, 0.9).direction, AudioDirection.LEFT)
        self.assertEqual(classify_audio_direction(-0.00018, 0.9).direction, AudioDirection.RIGHT)
        self.assertEqual(classify_audio_direction(0.00001, 0.9).direction, AudioDirection.CENTER)

    def test_low_confidence_audio_is_unknown(self):
        estimate = classify_audio_direction(0.00025, 0.1)
        self.assertEqual(estimate.direction, AudioDirection.UNKNOWN)
        self.assertLess(estimate.confidence, 0.35)

    def test_gcc_phat_tdoa_detects_known_delay(self):
        left = [0.0] * 16 + [1.0] + [0.0] * 47
        right = [0.0] * 20 + [1.0] + [0.0] * 43
        tdoa = gcc_phat_tdoa(left, right, sample_rate=16000, max_tau=0.001)
        self.assertAlmostEqual(tdoa, -4 / 16000, delta=1 / 16000)

    def test_fft_gcc_phat_reports_peak_confidence(self):
        left = [0.0] * 16 + [1.0] + [0.0] * 47
        right = [0.0] * 20 + [1.0] + [0.0] * 43

        result = gcc_phat(left, right, sample_rate=16000, max_tau=0.001)

        self.assertAlmostEqual(result.tdoa_s, -4 / 16000, delta=1 / 16000)
        self.assertGreater(result.peak_ratio, 1.0)

    def test_srp_phat_searches_front_hemisphere(self):
        pulse = [0.0] * 64 + [1.0] + [0.0] * 64

        left_result = srp_phat_front_hemisphere([0.0] * 4 + pulse, pulse + [0.0] * 4, sample_rate=16000)
        right_result = srp_phat_front_hemisphere(pulse + [0.0] * 4, [0.0] * 4 + pulse, sample_rate=16000)

        self.assertGreater(left_result.azimuth_deg, 0.0)
        self.assertLess(right_result.azimuth_deg, 0.0)
        self.assertLessEqual(abs(left_result.azimuth_deg), 90.0)
        self.assertGreater(left_result.confidence, 0.12)

    def test_smoother_waits_for_stable_direction(self):
        smoother = AudioDirectionSmoother(window_size=4, min_samples=2)

        first = smoother.update(AudioEstimate(AudioDirection.RIGHT, 0.8, -0.0003, 0.8))
        second = smoother.update(AudioEstimate(AudioDirection.LEFT, 0.8, 0.0003, 0.8))
        third = smoother.update(AudioEstimate(AudioDirection.LEFT, 0.8, 0.0003, 0.8))

        self.assertEqual(first.direction, AudioDirection.UNKNOWN)
        self.assertEqual(second.direction, AudioDirection.UNKNOWN)
        self.assertEqual(third.direction, AudioDirection.LEFT)

    def test_smoother_rejects_alternating_directions(self):
        smoother = AudioDirectionSmoother(window_size=4, min_samples=2)

        smoother.update(AudioEstimate(AudioDirection.LEFT, 0.8, 0.0003, 0.8))
        smoother.update(AudioEstimate(AudioDirection.RIGHT, 0.8, -0.0003, 0.8))
        estimate = smoother.update(AudioEstimate(AudioDirection.LEFT, 0.4, 0.0003, 0.4))

        self.assertEqual(estimate.direction, AudioDirection.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
