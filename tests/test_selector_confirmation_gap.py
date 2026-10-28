"""A long audio-observation gap cannot inherit old speaker-confirmation votes."""
from dataclasses import replace
import unittest
from locator_demo.av_selector import AudioVisualSelector
from locator_demo.visual_priority import VisualPrioritySelector
from locator_demo.audio_motion import qualify_audio_during_motion
from test_av_guidance import voice
from test_visual_priority import face


class ConfirmationGapTests(unittest.TestCase):
    policies = (AudioVisualSelector, VisualPrioritySelector)

    def people(self):
        return {1: [face(1, 25, width=140, score=.95)], 2: [face(2, -40, width=70, score=.8)]}

    def update(self, selector, now, seq, observed=None):
        return selector.update(self.people(), now=now, size=(640, 360), frame_time=now-.02,
            audio=voice(-40, now if observed is None else observed, seq))

    def ready(self, policy):
        s = policy(); s.update(self.people(), now=9., size=(640, 360))
        for n in range(3):
            self.assertEqual(self.update(s, 10+n*.1, 100+n), 1)
        return s

    def test_first_observation_after_a_long_gap_restarts_at_one_vote(self):
        for policy in self.policies:
            for resumed in (10.551, 11., 15.):
                with self.subTest(policy=policy.__name__, resumed=resumed):
                    s = self.ready(policy)
                    self.assertEqual(self.update(s, resumed, 103), 1)
                    self.assertEqual(s.audio_hits, 1)
                    self.assertEqual(s.audio_since, resumed)
                    self.assertTrue(s.metrics['audio_usable'])

    def test_cached_polls_after_a_gap_do_not_complete_confirmation(self):
        for policy in self.policies:
            s = self.ready(policy); self.update(s, 15., 103)
            for n in range(1, 10):
                self.assertEqual(self.update(s, 15+n*.02, 103, observed=15.), 1)
                self.assertEqual(s.audio_hits, 1)

    def test_sustained_fresh_audio_after_a_gap_can_reacquire(self):
        for policy in self.policies:
            s = self.ready(policy)
            for n in range(12):
                winner = self.update(s, 15+n*.1, 103+n)
            self.assertEqual(winner, 2)

    def test_349_and_350ms_gaps_keep_existing_confirmation(self):
        for policy in self.policies:
            for gap in (.349, .35):
                s = self.ready(policy); self.update(s, 10.2+gap, 103)
                self.assertEqual(s.audio_hits, 4)
                self.assertEqual(s.audio_since, 10.)

    def test_gap_is_measured_in_observation_time_not_poll_time(self):
        for policy in self.policies:
            s = self.ready(policy)
            # Polling stalled 400ms, but this valid observation is only 100ms
            # newer and 300ms old, inside the unchanged acoustic age budget.
            self.update(s, 10.6, 103, observed=10.3)
            self.assertEqual(s.audio_hits, 4)
            self.assertEqual(s.audio_since, 10.)

    def test_counter_skips_with_regular_observation_time_are_not_gaps(self):
        for policy in self.policies:
            s = self.ready(policy)
            for n in range(12):
                winner = self.update(s, 10.3+n*.1, 1000+n*900)
            self.assertEqual(winner, 2)
            self.assertGreaterEqual(s.audio_hits, 3)

    def test_counter_reset_still_clears_votes_before_the_gap_budget(self):
        for policy in self.policies:
            s = self.ready(policy); self.update(s, 10.3, 1)
            self.assertEqual(s.audio_hits, 1)
            self.assertEqual(s.audio_since, 10.3)

    def test_replayed_pre_gap_clock_cannot_restore_old_confirmation(self):
        for policy in self.policies:
            s = self.ready(policy); self.update(s, 15., 103)
            self.update(s, 15.02, 102, observed=10.2)
            self.assertEqual(s.audio_hits, 0)
            self.update(s, 15.1, 104)
            self.assertEqual(s.audio_hits, 1)
            self.assertEqual(s.audio_since, 15.1)

    def test_visual_only_lock_is_not_reset_by_long_scheduler_gaps(self):
        for policy in self.policies:
            s = self.ready(policy)
            for now in (15., 20., 100.):
                self.assertEqual(s.update(self.people(), now=now, size=(640, 360)), 1)
                self.assertFalse(s.metrics['audio_usable'])

    def test_continuous_moving_listener_still_uses_reliable_sound(self):
        for policy in self.policies:
            s = policy()
            for n in range(20):
                now = 10+n*.1
                yaw = (n%5)-2
                people = {1: [face(1, 25-yaw, width=140, score=.95)],
                          2: [face(2, -40-yaw, width=70, score=.8)]}
                e, context = qualify_audio_during_motion(voice(-40-yaw, now, 100+n), active=True)
                self.assertTrue(context['admitted'])
                winner = s.update(people, now=now, size=(640, 360), audio=e, frame_time=now-.02)
            self.assertEqual(winner, 2)
            self.assertTrue(s.metrics['audio_usable'])


if __name__ == '__main__':
    unittest.main()
