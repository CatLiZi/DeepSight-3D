"""Only new audio observations can confirm a visible acoustic nominee."""
from dataclasses import replace
import unittest

from locator_demo.av_selector import AudioVisualSelector
from locator_demo.visual_priority import VisualPrioritySelector
from test_av_guidance import voice
from test_visual_priority import face


class SelectorSequenceTests(unittest.TestCase):
    policies = (AudioVisualSelector, VisualPrioritySelector)

    def setUp(self):
        self.people = {1: [face(1, 25, width=140, score=.95)],
                       2: [face(2, -40, width=70, score=.8)]}

    def ready(self, policy):
        selector = policy()
        self.assertEqual(selector.update(self.people, now=9., size=(640, 360)), 1)
        return selector

    def update(self, selector, now, seq, *, observed=None):
        return selector.update(self.people, now=now, size=(640, 360),
            audio=voice(-40, now if observed is None else observed, seq))

    def test_alternating_counter_values_cannot_accumulate_a_speaker_switch(self):
        for policy in self.policies:
            with self.subTest(policy=policy.__name__):
                s = self.ready(policy)
                for n in range(15):
                    self.assertEqual(self.update(s, 10.+n*.1, 7+n%2), 1)
                    self.assertLess(s.audio_hits, 3)

    def test_replay_of_two_old_packets_never_confirms_three_observations(self):
        for policy in self.policies:
            s = self.ready(policy)
            for n in range(15):
                now = 10.+n*.02;seq = 100+n%2;observed = 10.+(n%2)*.02
                self.assertEqual(self.update(s, now, seq, observed=observed), 1)
                self.assertLess(s.audio_hits, 3)

    def test_increasing_counter_with_unchanged_receive_clock_is_not_new(self):
        for policy in self.policies:
            s = self.ready(policy)
            for n in range(15):
                self.update(s, 10.+n*.02, 100+n, observed=10.)
                self.assertLess(s.audio_hits, 3)

    def test_older_timestamp_with_larger_sequence_cannot_complete_confirmation(self):
        for policy in self.policies:
            s = self.ready(policy)
            self.update(s, 10., 100)
            self.update(s, 10.1, 101)
            self.assertEqual(self.update(s, 10.2, 102, observed=10.05), 1)
            self.assertEqual(s.audio_hits, 0)
            self.assertFalse(s.metrics['audio_usable'])

    def test_lower_sequence_and_older_time_is_replay_not_a_restart(self):
        for policy in self.policies:
            s = self.ready(policy)
            self.update(s, 10., 100)
            self.update(s, 10.1, 101)
            self.update(s, 10.2, 100, observed=10.)
            self.assertEqual(s.audio_hits, 0)
            self.assertFalse(s.metrics['audio_usable'])

    def test_newer_time_with_reset_counter_starts_confirmation_again(self):
        for policy in self.policies:
            s = self.ready(policy)
            self.update(s, 10., 100)
            self.update(s, 10.1, 101)
            self.assertEqual(self.update(s, 10.4, 1), 1)
            self.assertEqual(s.audio_hits, 1)
            self.assertEqual(s.audio_since, 10.4)

    def test_reused_counter_after_restart_does_not_keep_old_confirmation(self):
        for policy in self.policies:
            s = self.ready(policy)
            self.update(s, 10., 100)
            self.update(s, 10.1, 101)
            self.update(s, 10.4, 101)
            self.assertEqual(s.audio_hits, 1)
            self.assertEqual(s.audio_since, 10.4)

    def test_genuine_restart_recovers_after_fresh_sustained_audio(self):
        for policy in self.policies:
            s = self.ready(policy)
            self.update(s, 10., 100);self.update(s, 10.1, 101)
            for n in range(12): selected = self.update(s, 10.4+n*.1, n+1)
            self.assertEqual(selected, 2)
            self.assertTrue(s.metrics['audio_usable'])

    def test_canonical_cached_snapshot_does_not_reset_or_add_hits(self):
        for policy in self.policies:
            s = self.ready(policy)
            for n in range(3): self.update(s, 10.+n*.1, 100+n)
            hits = s.audio_hits
            self.update(s, 10.22, 102, observed=10.2)
            self.assertEqual(s.audio_hits, hits)
            self.assertTrue(s.metrics['audio_usable'])
            self.assertEqual(s.audio_since, 10.)

    def test_duplicate_after_invalid_audio_cannot_seed_a_new_vote(self):
        for policy in self.policies:
            s = self.ready(policy)
            self.update(s, 10., 100)
            invalid = replace(voice(-40, 10.01, 101), speech_confidence=.01)
            s.update(self.people, now=10.01, size=(640, 360), audio=invalid)
            self.update(s, 10.02, 100, observed=10.)
            self.assertEqual(s.audio_hits, 0)
            self.update(s, 10.1, 102)
            self.assertEqual(s.audio_hits, 1)
            self.assertEqual(s.audio_since, 10.1)

    def test_out_of_order_packet_does_not_move_the_watermark_back(self):
        for policy in self.policies:
            s = self.ready(policy)
            self.update(s, 10., 100);self.update(s, 10.1, 101)
            self.update(s, 10.12, 100, observed=10.)
            self.update(s, 10.14, 101, observed=10.1)
            self.assertEqual(s.audio_hits, 0)
            self.update(s, 10.2, 102)
            self.assertEqual(s.audio_hits, 1)

    def test_counter_skips_and_normal_new_audio_can_still_choose_visible_face(self):
        for policy in self.policies:
            s = self.ready(policy)
            for n in range(15): selected = self.update(s, 10.+n*.1, 100+n*5)
            self.assertEqual(selected, 2)
            self.assertGreaterEqual(s.audio_hits, 3)


if __name__ == '__main__': unittest.main()
