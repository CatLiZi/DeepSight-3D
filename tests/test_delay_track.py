"""Abrupt change admission with fresh PCM clocks; no physical device access."""
import unittest

from locator_demo.audio import AudioDirection
from locator_demo.delay_track import DelayConsensus
from locator_demo.ear_localization import EarPairLocalizer
from test_spectral_delay import CONFIG,signal


class DelayTrackTests(unittest.TestCase):
    def test_brief_opposite_peak_is_not_an_output_or_stale_extrapolation(self):
        local=EarPairLocalizer(CONFIG)
        for end in (10000,10960,11920):
            stable=local.estimate(signal(9),speech_probability=.99,sample_end=end)
        self.assertEqual(stable.direction,AudioDirection.LEFT)
        for end in (12880,13840):
            rejected=local.estimate(signal(-9),speech_probability=.99,sample_end=end)
            self.assertEqual(rejected.direction,AudioDirection.UNKNOWN)
            self.assertEqual(rejected.confidence,0.)
            self.assertEqual(rejected.noise_state,'direction_transition')
        self.assertEqual(local.estimate(signal(9),speech_probability=.99,sample_end=14800).direction,AudioDirection.LEFT)

    def test_real_persistent_direction_change_is_not_frozen_at_old_side(self):
        local=EarPairLocalizer(CONFIG)
        for end in (10000,10960,11920):local.estimate(signal(9),speech_probability=.99,sample_end=end)
        rows=[local.estimate(signal(-9),speech_probability=.99,sample_end=12880+i*960) for i in range(9)]
        self.assertTrue(all(r.direction in (AudioDirection.UNKNOWN,AudioDirection.RIGHT) for r in rows))
        self.assertEqual(rows[-1].direction,AudioDirection.RIGHT)
        self.assertGreater(rows[-1].confidence,.2)

    def test_polling_same_pcm_clock_cannot_confirm_or_refresh_a_candidate(self):
        local=EarPairLocalizer(CONFIG)
        local.estimate(signal(),speech_probability=.99,sample_end=10000)
        for _ in range(5):
            result=local.estimate(signal(),speech_probability=.99,sample_end=10000)
            self.assertEqual(result.direction,AudioDirection.UNKNOWN)
            self.assertEqual(result.noise_state,'stale_audio')

    def test_elapsed_pcm_time_controls_confirmation_across_poll_rates(self):
        for period in (.02,.033333333,.05):
            with self.subTest(period=period):
                tracker=DelayConsensus()
                tracker.accept(9/48000,0.)
                results=[(i*period,tracker.accept(-9/48000,i*period)[0]) for i in range(1,9)]
                first=next(t for t,ok in results if ok)
                self.assertGreaterEqual(first,period+.08-1e-8)
                self.assertLessEqual(first,period+.08+period+1e-8)

    def test_silence_or_capture_restart_removes_old_side_reference(self):
        for reason in ('no_voice_activity','audio_gap','audio_restarted','sample_clock_mismatch','stale_audio'):
            with self.subTest(reason=reason):
                local=EarPairLocalizer(CONFIG)
                local.estimate(signal(),speech_probability=.99,sample_end=10000)
                local._unknown(reason)
                self.assertIsNone(local.delay_track.reference)

    def test_expired_reference_does_not_delay_new_speaker_after_long_gap(self):
        tracker=DelayConsensus();tracker.accept(9/48000,1.)
        self.assertEqual(tracker.accept(-9/48000,1.5),(True,True))

    def test_large_same_side_change_keeps_coarse_evidence_available(self):
        tracker=DelayConsensus();tracker.accept(-4/48000,1.)
        self.assertEqual(tracker.accept(-16/48000,1.02),(True,False))

    def test_small_changes_through_centre_do_not_need_abrupt_flip_confirmation(self):
        tracker=DelayConsensus();tracker.accept(1/48000,1.)
        self.assertEqual(tracker.accept(-2/48000,1.02),(True,False))

    def test_raw_near_centre_sample_does_not_erase_a_reliable_side_reference(self):
        tracker=DelayConsensus();tracker.accept(-6/48000,1.)
        tracker.commit(-5/48000)
        self.assertEqual(tracker.accept(-1/48000,1.02),(True,False))
        tracker.commit(-5/48000)  # The reliable history still locates that side.
        self.assertEqual(tracker.accept(15/48000,1.04),(False,False))

    def test_inconsistent_pending_peaks_do_not_confirm_an_alternating_direction(self):
        tracker=DelayConsensus();tracker.accept(9/48000,0.)
        for i in range(1,10):
            self.assertFalse(tracker.accept((-9 if i%2 else -15)/48000,i*.02)[0])

    def test_invalid_clock_cannot_enter_spectral_or_temporal_estimator(self):
        for end in (True,1.,-1,500):
            with self.subTest(end=end):
                result=EarPairLocalizer(CONFIG).estimate(signal(),speech_probability=.99,sample_end=end)
                self.assertEqual(result.direction,AudioDirection.UNKNOWN)
                self.assertEqual(result.noise_state,'invalid_sample_clock')


if __name__=='__main__':unittest.main()
