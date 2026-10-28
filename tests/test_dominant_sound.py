"""Dominant voiced evidence, retained finite targets and simulated runtime integration."""
from dataclasses import replace
from unittest.mock import Mock, patch
import math
import unittest

from locator_demo.audio import AudioDirection
from locator_demo.dominant_sound import DominantSoundLock
from locator_demo.head import HeadPose
import test_av_motion_coordination as coordination_fixture
from test_motion_audio_admission import speech


def observation(angle, clock, sequence, **changes):
    return speech(azimuth_deg=angle, direction=AudioDirection.LEFT if angle > 6 else
                  AudioDirection.RIGHT if angle < -6 else AudioDirection.CENTER,
                  observed_monotonic=clock, sample_sequence=sequence, **changes)


class CoreEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.lock = DominantSoundLock()
        self.sequence = 0
        self.clock = 10.

    def feed(self, angle=35., *, dt=.05, yaw=1500., audio=None, **changes):
        self.clock += dt
        self.sequence += 1
        estimate = audio or observation(angle, self.clock, self.sequence, **changes)
        return self.lock.update(estimate, now=self.clock, yaw=yaw, units_per_degree=5.,
                               left_sign=1, yaw_min=800, yaw_max=2200, command_window=340)

    def acquire(self, angle=35.):
        for _ in range(14):
            state = self.feed(angle)
        self.assertTrue(state['locked'])
        return state

    def test_one_loud_observation_does_not_acquire_a_core(self):
        state = self.feed(energy=1.)
        self.assertFalse(state['locked'])

    def test_sustained_qualified_speech_acquires_a_finite_position(self):
        state = self.acquire()
        self.assertEqual(state['goal_yaw'], 1675)
        self.assertGreater(state['current_support_s'], .24)
        self.assertTrue(state['live_matches_core'])

    def test_unknown_keeps_the_position_even_after_the_scoring_window_expires(self):
        before = self.acquire()
        unknown = observation(0., self.clock + 120., self.sequence + 1,
            noise_state='no_voice_activity', speech_confidence=0., confidence=0., doa_confidence=0.)
        unknown = replace(unknown, direction=AudioDirection.UNKNOWN)
        self.clock += 120.
        after = self.feed(audio=unknown)
        self.assertEqual(after['core_id'], before['core_id'])
        self.assertEqual(after['goal_yaw'], before['goal_yaw'])
        self.assertFalse(after['live_matches_core'])
        self.assertEqual(after['current_score'], 0.)

    def test_repeated_cached_snapshots_never_inflate_evidence(self):
        cached = observation(35., self.clock, 1)
        for _ in range(10):
            state = self.feed(dt=.02, audio=cached)
        self.assertFalse(state['locked'])
        self.assertEqual(state['candidates'][0]['observations'], 1)

    def test_one_loud_competitor_cannot_displace_sustained_voice(self):
        before = self.acquire()
        after = self.feed(-35., energy=100.)
        self.assertEqual(after['core_id'], before['core_id'])
        self.assertEqual(after['goal_yaw'], before['goal_yaw'])

    def test_sustained_stronger_competitor_eventually_switches(self):
        before = self.acquire()
        for _ in range(45):
            after = self.feed(-35.)
        self.assertNotEqual(after['core_id'], before['core_id'])
        self.assertLess(after['goal_yaw'], 1500)
        self.assertEqual(after['switches'], 1)

    def test_balanced_alternating_competitors_do_not_ping_pong(self):
        before = self.acquire()
        for n in range(120):
            after = self.feed(35. if n % 2 == 0 else -35.)
        self.assertEqual(after['core_id'], before['core_id'])
        self.assertEqual(after['switches'], 0)

    def test_movement_rebases_same_source_instead_of_creating_new_directions(self):
        before = self.acquire(40.)
        for n in range(1, 21):
            yaw = 1500. + n * 8.
            after = self.feed((1700. - yaw) / 5., yaw=yaw)
        self.assertEqual(after['core_id'], before['core_id'])
        self.assertEqual(len(after['candidates']), 1)
        self.assertAlmostEqual(after['goal_yaw'], 1700, delta=1)
        self.assertAlmostEqual(after['relative_azimuth_deg'], 8., delta=.1)

    def test_clock_replay_or_stream_counter_reset_cannot_splice_confirmation(self):
        # Two packets remain below the faster initial-acquisition threshold.
        for n in range(2):
            self.feed()
        for n in range(4):
            state = self.feed(audio=observation(35., self.clock + .05, 1))
        self.assertFalse(state['locked'])
        old = observation(35., 9.9, 900)
        state = self.feed(audio=old)
        self.assertFalse(state['locked'])

    def test_invalid_or_uncertain_inputs_never_create_a_core(self):
        for key, value in [('speech_confidence', .1), ('temporal_stability', .2), ('doa_confidence', .1),
                           ('confidence', float('nan')), ('azimuth_std_deg', 40.)]:
            self.lock.reset()
            for _ in range(20):
                state = self.feed(**{key: value})
            self.assertFalse(state['locked'], (key, value))

    def test_callback_rate_changes_do_not_turn_frame_counts_into_priority(self):
        values = []
        for dt in (.1, .02):
            self.setUp()
            for _ in range(round(1. / dt) + 1):
                state = self.feed(dt=dt)
            values.append(state['current_score'])
        self.assertAlmostEqual(values[0], values[1], delta=values[1] * .07)

    def test_goal_is_clamped_and_reset_clears_automatic_intent(self):
        for _ in range(14):
            state = self.feed(80., yaw=2150.)
        self.assertEqual(state['goal_yaw'], 2200)
        self.lock.reset()
        self.assertFalse(self.lock.snapshot()['locked'])
        self.assertIsNone(self.lock.snapshot()['goal_yaw'])


class CoreRuntimeTests(unittest.TestCase):
    setUp_fixture = coordination_fixture.CoordinationRuntimeTests.setUp
    tearDown = coordination_fixture.CoordinationRuntimeTests.tearDown

    def setUp(self):
        self.setUp_fixture()
        r = self.runtime
        r.audio_capture = replace(r.audio_capture, localization_mode='ear_pair', ear_left_channel=2,
            ear_right_channel=3, ear_mapping_confirmed=True, ear_spacing_m=.1355)
        r.features = replace(r.features, visual_enabled=False)
        r.direction = replace(r.direction, yaw_left_sign=1)
        r.logic = r._make_logic()
        r.pose = HeadPose(1500,1550)
        self.head.estimated_pose = r.pose
        self.head.cancel = Mock()
        self.now = 10.
        self.n = 0
        self.commands = []
        def move(pose, **_kwargs):
            self.commands.append(pose)
            r.pose = pose
        r.move = Mock(side_effect=move)

    def tick_voice(self, angle=35., *, unknown=False):
        self.n += 1
        self.now += .05
        self.voice = observation(angle, self.now, self.n)
        if unknown:
            self.voice = replace(self.voice, direction=AudioDirection.UNKNOWN, confidence=0.,
                speech_confidence=.01, doa_confidence=0., noise_state='no_voice_activity')
        self.frame = replace(self.frame, detection_sequence=self.n)
        self.runtime.motor_guard_until_s = 0
        with patch('locator_demo.web.app.time.time', return_value=100. + self.n * .1), patch('time.perf_counter', return_value=self.now):
            return self.runtime.tick(include_frame=False)

    def acquire(self):
        for _ in range(16):
            state = self.tick_voice()
        self.assertTrue(state['audio']['core_source']['locked'])
        return state

    def test_speaking_preview_face_cannot_block_audio_only(self):
        target = replace(self.target, mouth_motion_score=.9, active_speaker_score=.9)
        self.frame = replace(self.frame, target=target, targets=[target])
        self.acquire()
        self.assertTrue(self.commands)
        self.assertGreater(self.commands[-1].yaw,1500)
        self.assertTrue(all(p.pitch==1550 for p in self.commands))

    def test_stale_or_switching_preview_cannot_cancel_audio_only_motion(self):
        for reason in ('confirming_view','stale_frame','no_usable_eye'):
            self.frame = replace(self.frame, binocular={**self.frame.binocular,'selection_reason':reason})
            self.acquire()
        self.assertTrue(self.commands)
        self.head.cancel.assert_not_called()

    def test_unknown_after_lock_still_submits_the_confirmed_position_after_actuator_wait(self):
        self.head.is_moving = True
        state = self.acquire()
        self.assertFalse(self.commands)
        goal = state['audio']['core_source']['goal_yaw']
        self.head.is_moving = False
        state = self.tick_voice(unknown=True)
        self.assertEqual(self.commands[-1].yaw,goal)
        self.assertEqual(state['audio']['direction'],'unknown')
        self.assertTrue(state['audio']['core_source']['historical'])

    def test_unknown_never_adds_the_old_relative_angle_again_and_again(self):
        before = self.acquire()
        count = len(self.commands)
        for _ in range(80):
            after = self.tick_voice(unknown=True)
        self.assertEqual(len(self.commands),count)
        self.assertEqual(after['audio']['core_source']['goal_yaw'],before['audio']['core_source']['goal_yaw'])
        # Assistance remains enabled, but no old direction is fabricated.
        self.assertEqual(self.guidance.call_args.args[0].direction,AudioDirection.UNKNOWN)

    def test_visible_visual_target_keeps_control_priority(self):
        self.runtime.features = replace(self.runtime.features, visual_enabled=True)
        self.acquire()
        self.runtime.move.assert_not_called()
        self.assertTrue(self.runtime.last_state.target_confirmed)

    def test_stop_blocks_stored_goal_and_a_new_start_needs_new_evidence(self):
        self.head.is_moving = True
        self.acquire()
        self.runtime.halt_motion()
        self.head.is_moving = False
        state = self.tick_voice(unknown=True)
        self.assertFalse(state['audio']['core_source']['locked'])
        self.assertFalse(self.commands)
        self.runtime.running = True
        self.runtime.begin_start()
        state = self.tick_voice(unknown=True)
        self.assertFalse(state['audio']['core_source']['locked'])
        self.assertFalse(self.commands)


if __name__ == '__main__':
    unittest.main()
