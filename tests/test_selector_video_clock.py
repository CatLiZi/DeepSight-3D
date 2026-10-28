"""AV pairing rejects impossible/old video clocks without muting raw sound."""
from dataclasses import asdict
import unittest
from locator_demo.av_selector import AudioVisualSelector
from locator_demo.visual_priority import VisualPrioritySelector
from locator_demo.audio_motion import qualify_audio_during_motion
from test_av_guidance import voice
from test_visual_priority import face


class VideoClockTests(unittest.TestCase):
    policies = (AudioVisualSelector, VisualPrioritySelector)

    def people(self):
        return {1: [face(1, 25, width=140, score=.95)], 2: [face(2, -40, width=70, score=.8)]}

    def test_future_video_with_a_small_pair_gap_is_rejected(self):
        for policy in self.policies:
            ok, reason, age = policy()._audio_validity(voice(-40, 20., 100), 20., 20.2)
            self.assertFalse(ok); self.assertEqual(reason, 'future_video_for_audio'); self.assertEqual(age, 0.)

    def test_old_video_and_valid_old_audio_do_not_validate_each_other(self):
        for policy in self.policies:
            e = voice(-40, 19.7, 100)
            self.assertLess(abs(19.3-e.observed_monotonic), .45)
            ok, reason, age = policy()._audio_validity(e, 20., 19.3)
            self.assertFalse(ok); self.assertEqual(reason, 'stale_video_for_audio')
            self.assertAlmostEqual(age, .3)

    def test_supplied_video_age_boundaries_have_only_numeric_tolerance(self):
        for policy in self.policies:
            for video_age, expected in ((-.020001, False), (-.02, True), (.35, True), (.350001, False)):
                with self.subTest(policy=policy.__name__, video_age=video_age):
                    self.assertEqual(policy()._audio_validity(voice(-40, 100., 100), 100., 100.-video_age)[0], expected)

    def test_legacy_unspecified_video_clock_remains_supported(self):
        for policy in self.policies:
            self.assertTrue(policy()._audio_validity(voice(-40, 20., 100), 20., 0.)[0])

    def test_stale_video_blocks_nomination_but_preserves_visual_target(self):
        for policy in self.policies:
            s = policy()
            for n in range(15):
                now = 20+n*.1
                winner = s.update(self.people(), now=now, size=(640, 360), audio=voice(-40, now-.3, 100+n), frame_time=now-.7)
                self.assertEqual(winner, 1)
                self.assertEqual(s.audio_hits, 0)
                self.assertFalse(s.metrics['audio_usable'])

    def test_video_rejection_does_not_mutate_sound_angle_or_quality(self):
        for policy in self.policies:
            e = voice(-40, 20., 100); original = asdict(e)
            policy().update(self.people(), now=20., size=(640, 360), audio=e, frame_time=20.2)
            self.assertEqual(asdict(e), original)
            self.assertEqual(e.direction.value, 'right'); self.assertFalse(e.motor_suppressed)

    def test_fresh_resynchronization_reacquires_with_normal_confirmation(self):
        for policy in self.policies:
            s = policy()
            for n in range(5):
                now = 20+n*.1
                s.update(self.people(), now=now, size=(640, 360), audio=voice(-40, now, 100+n), frame_time=now+.2)
            for n in range(15):
                now = 21+n*.1
                winner = s.update(self.people(), now=now, size=(640, 360), audio=voice(-40, now, 200+n), frame_time=now-.1)
            self.assertEqual(winner, 2); self.assertTrue(s.metrics['audio_usable'])

    def test_pure_visual_selection_ignores_unused_audio_pairing_clock(self):
        for policy in self.policies:
            s = policy()
            for n in range(10):
                now = 20+n*.1
                self.assertEqual(s.update(self.people(), now=now, size=(640, 360), frame_time=now+.2), 1)

    def test_bad_video_clears_old_audio_confirmation_before_recovery(self):
        for policy in self.policies:
            s = policy()
            for n in range(2):
                now = 20+n*.1
                s.update(self.people(), now=now, size=(640, 360), audio=voice(-40, now, 100+n), frame_time=now-.1)
            s.update(self.people(), now=20.2, size=(640, 360), audio=voice(-40, 20.2, 102), frame_time=20.4)
            self.assertEqual(s.audio_hits, 0)
            s.update(self.people(), now=20.3, size=(640, 360), audio=voice(-40, 20.3, 103), frame_time=20.2)
            self.assertEqual(s.audio_hits, 1)

    def test_moving_listener_with_fresh_video_keeps_acoustic_guidance(self):
        for policy in self.policies:
            s = policy()
            for n in range(20):
                now = 20+n*.1; yaw = (n%5)-2
                people = {1: [face(1, 25-yaw, width=140, score=.95)], 2: [face(2, -40-yaw, width=70, score=.8)]}
                e, context = qualify_audio_during_motion(voice(-40-yaw, now, 100+n), active=True)
                self.assertTrue(context['admitted'])
                winner = s.update(people, now=now, size=(640, 360), audio=e, frame_time=now-.12)
            self.assertEqual(winner, 2); self.assertTrue(s.metrics['audio_usable'])

    def test_existing_large_pair_gap_rejection_reason_is_preserved(self):
        for policy in self.policies:
            ok, reason, _ = policy()._audio_validity(voice(-40, 20., 100), 20., 19.)
            self.assertFalse(ok); self.assertEqual(reason, 'audio_video_time_gap')


if __name__ == '__main__':
    unittest.main()
