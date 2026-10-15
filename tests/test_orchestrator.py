import unittest

from locator_demo.audio import AudioDirection, AudioEstimate
from locator_demo.audio_device import FfmpegAudioLocalizer
from locator_demo.orchestrator import DemoMode, LocatorStateMachine
from locator_demo.vision import FaceTarget


class OrchestratorTests(unittest.TestCase):
    def test_audio_detection_turns_to_sound_then_visual_lock_tracks(self):
        sm = LocatorStateMachine()

        first = sm.update(audio=AudioEstimate(AudioDirection.LEFT, 0.8, 0.0002, 0.5), target=None, now_s=1.0)
        self.assertEqual(first.mode, DemoMode.AUDIO_CANDIDATE)
        self.assertEqual(first.last_event, "sound_candidate")

        first = sm.update(audio=AudioEstimate(AudioDirection.LEFT, 0.8, 0.0002, 0.5), target=None, now_s=1.1)
        self.assertEqual(first.mode, DemoMode.SEEK_VISUAL)
        self.assertEqual(first.audio_direction, "left")

        target = FaceTarget(
            300,
            100,
            420,
            260,
            label="person",
            score=0.9,
            mouth_motion_score=0.5,
            active_speaker_score=0.8,
        )
        second = sm.update(audio=None, target=target, now_s=1.2)
        self.assertEqual(second.mode, DemoMode.TRACK_SPEAKER)
        self.assertTrue(second.visual_locked)

    def test_lost_target_returns_to_listening_after_timeout(self):
        sm = LocatorStateMachine(lost_timeout_s=0.5)
        target = FaceTarget(300, 100, 420, 260, active_speaker_score=0.8, mouth_motion_score=0.8)
        sm.update(audio=AudioEstimate(AudioDirection.LEFT, 0.8, 0.0002, 0.5), target=None, now_s=0.8)
        sm.update(audio=AudioEstimate(AudioDirection.LEFT, 0.8, 0.0002, 0.5), target=None, now_s=0.9)
        sm.update(audio=None, target=target, now_s=1.0)

        state = sm.update(audio=None, target=None, now_s=2.0)

        self.assertEqual(state.mode, DemoMode.HOLD)
        self.assertFalse(state.visual_locked)

    def test_audio_candidate_resets_after_low_energy(self):
        sm = LocatorStateMachine()

        sm.update(audio=AudioEstimate(AudioDirection.RIGHT, 0.8, -0.0002, 0.5), target=None, now_s=1.0)
        sm.update(audio=AudioEstimate(AudioDirection.UNKNOWN, 0.1, 0.0, 0.1), target=None, now_s=1.1)
        state = sm.update(audio=AudioEstimate(AudioDirection.RIGHT, 0.8, -0.0002, 0.5), target=None, now_s=1.2)

        self.assertEqual(state.mode, DemoMode.AUDIO_CANDIDATE)
        self.assertEqual(state.last_event, "sound_candidate")

    def test_motor_suppressed_audio_does_not_trigger_candidate(self):
        sm = LocatorStateMachine()

        state = sm.update(
            audio=AudioEstimate(
                AudioDirection.LEFT,
                0.9,
                0.0002,
                0.7,
                speech_confidence=0.9,
                doa_confidence=0.9,
                motor_suppressed=True,
            ),
            target=None,
            now_s=1.0,
        )

        self.assertEqual(state.mode, DemoMode.LISTENING)

    def test_center_speech_counts_as_recent_audio_evidence(self):
        sm = LocatorStateMachine(required_audio_hits=1)

        state = sm.update(
            audio=AudioEstimate(
                AudioDirection.CENTER,
                0.8,
                0.0,
                0.6,
                speech_confidence=0.8,
                doa_confidence=0.4,
            ),
            target=None,
            now_s=1.0,
        )

        self.assertEqual(state.mode, DemoMode.SEEK_VISUAL)
        self.assertTrue(state.audio_ready)

    def test_denoise_double_check_rejects_noise_like_trigger(self):
        localizer = FfmpegAudioLocalizer(denoise=True)
        raw = AudioEstimate(
            AudioDirection.LEFT,
            0.8,
            0.00022,
            0.5,
            speech_confidence=0.9,
            doa_confidence=0.9,
        )
        denoised = AudioEstimate(
            AudioDirection.UNKNOWN,
            0.08,
            0.0,
            0.06,
            speech_confidence=0.05,
            doa_confidence=0.02,
        )

        gated = localizer._voice_gate(raw, denoised)

        self.assertEqual(gated.direction, AudioDirection.UNKNOWN)
        self.assertEqual(gated.noise_state, "denoise_double_check_failed")

    def test_visual_target_needs_mouth_evidence_after_audio(self):
        sm = LocatorStateMachine(required_audio_hits=1)
        sm.update(
            audio=AudioEstimate(
                AudioDirection.LEFT,
                0.9,
                0.0002,
                0.7,
                speech_confidence=0.9,
                doa_confidence=0.9,
            ),
            target=None,
            now_s=1.0,
        )

        state = sm.update(
            audio=None,
            target=FaceTarget(300, 100, 420, 260, score=0.9, active_speaker_score=0.8),
            now_s=1.2,
        )

        self.assertNotEqual(state.mode, DemoMode.TRACK_SPEAKER)
        self.assertFalse(state.target_confirmed)

    def test_configurable_mouth_threshold_allows_low_fps_mouth_motion(self):
        sm = LocatorStateMachine(
            required_audio_hits=1,
            speech_confidence_threshold=0.15,
            doa_confidence_threshold=0.05,
            mouth_evidence_threshold=0.06,
        )
        sm.update(
            audio=AudioEstimate(
                AudioDirection.LEFT,
                0.35,
                0.0002,
                0.3,
                speech_confidence=0.2,
                doa_confidence=0.08,
            ),
            target=None,
            now_s=1.0,
        )

        state = sm.update(
            audio=None,
            target=FaceTarget(
                260,
                0,
                390,
                112,
                score=0.85,
                frontal_score=0.99,
                mouth_motion_score=0.071,
                active_speaker_score=0.34,
            ),
            now_s=1.2,
        )

        self.assertEqual(state.mode, DemoMode.TRACK_SPEAKER)
        self.assertTrue(state.target_confirmed)

    def test_visual_target_without_recent_audio_still_locks_for_visual_tracking(self):
        sm = LocatorStateMachine()

        state = sm.update(
            audio=None,
            target=FaceTarget(
                300,
                100,
                420,
                260,
                score=0.9,
                mouth_motion_score=0.8,
                active_speaker_score=0.8,
            ),
            now_s=1.0,
        )

        self.assertEqual(state.mode, DemoMode.TRACK_SPEAKER)
        self.assertTrue(state.target_confirmed)


if __name__ == "__main__":
    unittest.main()
