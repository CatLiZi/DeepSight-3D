from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .audio import AudioDirection, AudioEstimate
from .vision import FaceTarget


class DemoMode(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    AUDIO_CANDIDATE = "audio_candidate"
    SEEK_VISUAL = "seek_visual"
    REFRAME_VISUAL = "reframe_visual"
    VISUAL_CONFIRM = "visual_confirm"
    TRACK_SPEAKER = "track_speaker"
    HOLD = "hold"
    COOLDOWN = "cooldown"
    VISUAL_SCAN = "visual_scan"
    VISUAL_CANDIDATE = "visual_candidate"
    ASD_CONFIRM = "asd_confirm"
    SPEAKER_LOCKED = "speaker_locked"
    LOCK_HOLD = "lock_hold"
    AUDIO_SEARCH = "audio_search"
    VISUAL_REACQUIRE = "visual_reacquire"
    TURNING_TO_SOUND = "seek_visual"
    VISUAL_ACQUIRE = "visual_confirm"
    TRACKING = "track_speaker"


@dataclass(frozen=True)
class DemoState:
    mode: DemoMode
    audio_direction: str = "unknown"
    visual_locked: bool = False
    target_label: str = ""
    last_event: str = ""
    audio_ready: bool = False
    target_confirmed: bool = False


class LocatorStateMachine:
    def __init__(
        self,
        *,
        lost_timeout_s: float = 0.8,
        hold_timeout_s: float = 1.0,
        audio_confidence_threshold: float = 0.30,
        speech_confidence_threshold: float = 0.25,
        doa_confidence_threshold: float = 0.12,
        visual_speaker_threshold: float = 0.30,
        mouth_evidence_threshold: float = 0.25,
        visual_audio_sync_window_s: float = 1.2,
        required_audio_hits: int = 2,
    ):
        self.lost_timeout_s = float(lost_timeout_s)
        self.hold_timeout_s = float(hold_timeout_s)
        self.audio_confidence_threshold = float(audio_confidence_threshold)
        self.speech_confidence_threshold = float(speech_confidence_threshold)
        self.doa_confidence_threshold = float(doa_confidence_threshold)
        self.visual_speaker_threshold = float(visual_speaker_threshold)
        self.mouth_evidence_threshold = float(mouth_evidence_threshold)
        self.visual_audio_sync_window_s = float(visual_audio_sync_window_s)
        self.required_audio_hits = max(1, int(required_audio_hits))
        self._mode = DemoMode.LISTENING
        self._last_target_s: float | None = None
        self._last_audio_s: float | None = None
        self._hold_started_s: float | None = None
        self._audio_direction = AudioDirection.UNKNOWN.value
        self._candidate_direction = AudioDirection.UNKNOWN
        self._candidate_hits = 0

    def _audio_is_ready(self, audio: AudioEstimate | None) -> bool:
        if audio is None or audio.motor_suppressed:
            return False
        if audio.direction == AudioDirection.UNKNOWN:
            return False
        if audio.noise_state in {"noise", "quiet", "fan_or_motor", "low_band_noise", "denoise_guard"}:
            return False
        if audio.confidence < self.audio_confidence_threshold:
            return False
        speech_confidence = audio.speech_confidence if audio.speech_confidence > 0 else audio.confidence
        doa_confidence = audio.doa_confidence if audio.doa_confidence > 0 else audio.confidence
        if speech_confidence < self.speech_confidence_threshold:
            return False
        if doa_confidence < self.doa_confidence_threshold:
            return False
        if audio.energy < 0.08:
            return False
        return True

    def _target_is_confirmed(self, target: FaceTarget | None, now_s: float) -> bool:
        if target is None:
            return False
        face_height_ratio = float(getattr(target, "face_height_ratio", 0.0))
        size_evidence = face_height_ratio >= 0.12 or target.area >= 8000.0
        if target.speaker_score < self.visual_speaker_threshold and not size_evidence:
            return False
        if target.frontal_score < 0.25 and not size_evidence:
            return False
        mouth_evidence = max(target.mouth_motion_score, target.mouth_audio_sync_score)
        if mouth_evidence < self.mouth_evidence_threshold and not size_evidence:
            return False
        return True

    def update(
        self,
        *,
        audio: AudioEstimate | None,
        target: FaceTarget | None,
        now_s: float,
    ) -> DemoState:
        audio_ready = self._audio_is_ready(audio)
        target_confirmed = self._target_is_confirmed(target, now_s)

        if target_confirmed and target is not None:
            self._mode = DemoMode.TRACK_SPEAKER
            self._last_target_s = now_s
            self._hold_started_s = None
            return DemoState(
                self._mode,
                audio_direction=self._audio_direction,
                visual_locked=True,
                target_label=target.label,
                last_event="speaker_locked",
                audio_ready=audio_ready,
                target_confirmed=True,
            )

        if self._mode == DemoMode.TRACK_SPEAKER and self._last_target_s is not None:
            if now_s - self._last_target_s > self.lost_timeout_s:
                self._mode = DemoMode.HOLD
                self._hold_started_s = now_s
                return DemoState(self._mode, audio_direction=self._audio_direction, last_event="target_lost")

        if self._mode == DemoMode.HOLD:
            if audio_ready:
                self._mode = DemoMode.LISTENING
                self._hold_started_s = None
            elif self._hold_started_s is not None and now_s - self._hold_started_s >= self.hold_timeout_s:
                self._mode = DemoMode.LISTENING
                self._last_target_s = None
                self._hold_started_s = None
                return DemoState(self._mode, audio_direction=self._audio_direction, last_event="hold_released")
            else:
                return DemoState(self._mode, audio_direction=self._audio_direction)

        if audio_ready and audio is not None:
            if audio.direction == self._candidate_direction:
                self._candidate_hits += 1
            else:
                self._candidate_direction = audio.direction
                self._candidate_hits = 1
            if self._candidate_hits < self.required_audio_hits:
                self._mode = DemoMode.AUDIO_CANDIDATE
                return DemoState(
                    self._mode,
                    audio_direction=audio.direction.value,
                    last_event="sound_candidate",
                    audio_ready=True,
                )
            self._audio_direction = audio.direction.value
            self._last_audio_s = now_s
            self._mode = DemoMode.SEEK_VISUAL
            return DemoState(self._mode, audio_direction=self._audio_direction, last_event="sound_confirmed", audio_ready=True)

        self._candidate_direction = AudioDirection.UNKNOWN
        self._candidate_hits = 0
        if self._mode in (DemoMode.AUDIO_CANDIDATE, DemoMode.SEEK_VISUAL, DemoMode.VISUAL_CONFIRM):
            self._mode = DemoMode.LISTENING
        return DemoState(self._mode, audio_direction=self._audio_direction)
