from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..asd import RuleBasedAsdBackend, make_asd_backend
from ..audio import (
    AudioDirection,
    AudioEstimate,
    apply_audio_channel_mapping,
    azimuth_from_tdoa,
    classify_audio_direction,
    with_motor_suppression,
)
from ..audio_device import FfmpegAudioLocalizer, SimulatedAudioLocalizer
from ..camera import FfmpegCameraSnapshot, SimulatedVisionTracker, VisionStatus
from ..devices import list_dshow_devices
from ..head import AxisConfig, DirectionConfig, HeadControllerLogic, HeadPose
from ..orchestrator import DemoMode, DemoState, LocatorStateMachine
from ..servo_bus import TwoAxisHeadHardware, ZPServoBus, choose_servo_port, list_serial_ports
from ..settings import (
    AudioMappingConfig,
    AudioProcessingConfig,
    AxisLimitsConfig,
    CameraConfig,
    ControlConfig,
    FeatureConfig,
    SavedSettings,
    VisionProcessingConfig,
    default_settings_path,
    load_settings,
    save_settings,
)
from ..vision import FaceTarget, choose_active_speaker_target, normalized_center_error


class ScanRequest(BaseModel):
    ids: list[int] = Field(default_factory=lambda: list(range(0, 16)))


class ServoReadRequest(BaseModel):
    servo_id: int = 2
    samples: int = 10
    interval_ms: int = 100


class ServoReadRawRequest(BaseModel):
    servo_id: int = 2
    samples: int = 10
    interval_ms: int = 100


class ServoDirectMoveRequest(BaseModel):
    servo_id: int = 2
    position: int = 1500
    time_ms: int = 1000
    arm: bool = False


class MoveRequest(BaseModel):
    yaw: int = 1500
    pitch: int = 1500
    time_ms: int = 1000


class VisualCalibrationRequest(BaseModel):
    yaw: int | None = None
    pitch: int | None = None
    note: str = ""


class JogRequest(BaseModel):
    direction: str
    amount: int | None = None
    time_ms: int = 900


class ServoConfigRequest(BaseModel):
    yaw_id: int | None = None
    pitch_id: int = 2
    port: str | None = None


class DirectionConfigRequest(BaseModel):
    yaw_left_sign: int = -1
    pitch_up_sign: int = -1
    manual_step: int = 60


class FeatureConfigRequest(BaseModel):
    audio_enabled: bool = True
    visual_enabled: bool = True
    camera_enabled: bool = True


class CameraConfigRequest(BaseModel):
    device_name: str | None = None
    video_size: str | None = None
    crop_left_half: bool | None = None
    fps: int | None = Field(None, ge=1, le=30)
    output_width: int = Field(640, ge=320, le=1920)
    face_detector_backend: str = "mediapipe"
    scrfd_model_path: str | None = None
    scrfd_threshold: float = Field(0.35, ge=0.0, le=1.0)
    scrfd_input_size: int = Field(640, ge=320, le=1280)


class AudioMappingConfigRequest(BaseModel):
    swap_channels: bool = False


class ControlConfigRequest(BaseModel):
    control_profile: str = "stable"
    motor_guard_ms: int = 250


class AxisLimitsConfigRequest(BaseModel):
    yaw_min: int = 1200
    yaw_center: int = 1500
    yaw_max: int = 1800
    pitch_min: int = 1200
    pitch_center: int = 1500
    pitch_max: int = 1800


class AudioProcessingConfigRequest(BaseModel):
    vad_enabled: bool = True
    audio_confidence_threshold: float = Field(0.20, ge=0.0, le=1.0)
    speech_confidence_threshold: float = Field(0.15, ge=0.0, le=1.0)
    doa_confidence_threshold: float = Field(0.05, ge=0.0, le=1.0)
    required_audio_hits: int = Field(1, ge=1, le=5)
    denoise_enabled: bool = False
    denoise_dry_mix: float = Field(0.15, ge=0.0, le=1.0)
    denoise_output_dir: str | None = None
    recording_enabled: bool = True
    use_denoised_for_localization: bool = False


class VisionProcessingConfigRequest(BaseModel):
    active_speaker_enabled: bool = True
    tracking_strategy: str = "classic_audio_first"
    asd_backend: str = "rules"
    speaker_lock_policy: str = "turn_hold"
    target_offset_x_norm: float = Field(0.04, ge=-0.25, le=0.25)
    target_offset_y_norm: float = Field(0.0, ge=-0.25, le=0.25)
    visual_mirror_x: bool = False
    visual_speaker_threshold: float = Field(0.30, ge=0.0, le=1.0)
    mouth_evidence_threshold: float = Field(0.06, ge=0.0, le=1.0)
    visual_yaw_mode: str = "small"
    visual_pitch_enabled: bool = True
    visual_yaw_deadband: float | None = Field(None, ge=0.0, le=0.5)
    visual_pitch_deadband: float | None = Field(None, ge=0.0, le=0.5)
    visual_yaw_min_delta: int = Field(0, ge=0, le=200)
    visual_yaw_max_delta: int | None = Field(None, ge=1, le=300)
    visual_pitch_min_delta: int = Field(0, ge=0, le=200)
    visual_pitch_max_delta: int | None = Field(None, ge=1, le=300)
    min_face_height_ratio: float = Field(0.12, ge=0.0, le=1.0)
    keep_face_height_ratio: float = Field(0.09, ge=0.0, le=1.0)
    talknet_threshold: float = Field(0.55, ge=0.0, le=1.0)
    speaker_lock_hold_s: float = Field(1.2, ge=0.1, le=5.0)
    speaker_lost_timeout_s: float = Field(0.8, ge=0.1, le=5.0)
    audio_interrupt_enabled: bool = False
    audio_search_after_silent_visual: bool = False
    silent_visual_hold_s: float = Field(1.2, ge=0.1, le=5.0)


@dataclass
class RuntimeConfig:
    port: str = "COM14"
    yaw_id: int | None = None
    pitch_id: int = 2
    simulated: bool = False
    settings_path: Path | None = None


class SimulatedBus(ZPServoBus):
    def __init__(self):
        super().__init__(port="SIM")
        self.positions = {2: 1500}
        self.moves: list[tuple[int, int, int]] = []

    @property
    def connected(self) -> bool:
        return True

    def connect(self):
        return None

    def close(self) -> None:
        pass

    def set_mode_1(self, servo_id: int) -> str:
        return "#OK!"

    def move(self, servo_id: int, position: int, time_ms: int, *, force_arm: bool = False) -> None:
        self.positions[int(servo_id)] = int(position)
        self.moves.append((int(servo_id), int(position), int(time_ms)))

    def move_position_only(self, servo_id: int, position: int, time_ms: int) -> None:
        self.positions[int(servo_id)] = int(position)
        self.moves.append((int(servo_id), int(position), int(time_ms)))

    def read_position(self, servo_id: int) -> int | None:
        return self.positions.get(int(servo_id))

    def read_position_response(self, servo_id: int) -> str:
        position = self.read_position(servo_id)
        return f"#{int(servo_id):03d}P{int(position):04d}!" if position is not None else ""

    def probe(self, servo_id: int) -> bool:
        return int(servo_id) in self.positions


class DemoRuntime:
    def __init__(self, config: RuntimeConfig):
        self.config = config
        saved = load_settings(config.settings_path)
        if saved.port:
            self.config.port = saved.port
        self.config.yaw_id = saved.yaw_id
        self.config.pitch_id = saved.pitch_id
        self.direction = saved.direction
        self.features = saved.features
        self.camera = saved.camera
        self.audio_mapping = saved.audio_mapping
        self.control = saved.control
        self.axis_limits = saved.axis_limits
        self.audio_processing = saved.audio
        self.vision_processing = saved.vision
        if not config.simulated:
            self.config.port = choose_servo_port(config.port)
        self.logic = self._make_logic()
        self.bus = SimulatedBus() if config.simulated else ZPServoBus(config.port)
        self.head = self._make_head()
        self.audio = (
            SimulatedAudioLocalizer()
            if config.simulated
            else FfmpegAudioLocalizer(
                denoise=self.audio_processing.denoise_enabled,
                denoise_dry_mix=self.audio_processing.denoise_dry_mix,
                denoise_output_dir=self.audio_processing.denoise_output_dir,
                use_denoised_for_localization=self.audio_processing.use_denoised_for_localization,
            )
        )
        self.vision = self._make_vision()
        self.state_machine = self._make_state_machine()
        self.running = False
        self.pose = self.logic.center_pose
        self.last_audio = AudioEstimate(AudioDirection.UNKNOWN, 0.0, 0.0, 0.0)
        self.last_target = None
        self.last_targets = []
        self.last_body_target = None
        self.locked_speaker_id: int | None = None
        self.locked_speaker_score = 0.0
        self._locked_speaker_seen_s = 0.0
        self._locked_speaker_spoke_s = 0.0
        self._silent_visual_candidate_id: int | None = None
        self._silent_visual_candidate_since_s = 0.0
        self.audio_search_allowed = True
        self.asd_backend = make_asd_backend(self.vision_processing.asd_backend)
        self.last_state = self.state_machine.update(audio=None, target=None, now_s=time.time())
        self.events: list[dict[str, Any]] = []
        self.last_frame_b64 = ""
        self.last_frame_width = 640
        self.last_frame_height = 400
        self.last_vision_status = (
            self.vision.read().status
            if config.simulated and self.features.camera_enabled
            else VisionStatus(False, self.camera.device_name or "off", "camera off", fps=0.0)
        )
        self.last_motion_s = 0.0
        self.motion_interval_s = self._profile_params()["motion_interval_s"]
        self.motor_guard_until_s = 0.0
        self._smoothed_error: tuple[float, float] | None = None
        self._pitch_error_direction = 0
        self._pitch_error_hits = 0
        self._audio_seek_origin_yaw: int | None = None
        self._audio_seek_started_s = 0.0
        self._audio_seek_steps = 0
        self._last_audio_seek_abs_azimuth: float | None = None
        self._last_audio_seek_direction = ""
        self._reframe_origin_yaw: int | None = None
        self._reframe_started_s = 0.0
        self._reframe_step = 0
        self._last_reframe_step_s = 0.0
        self._last_reframe_audio_s = 0.0
        self._reframe_active = False
        self.visual_calibration_samples: list[dict[str, Any]] = []
        self._tick_lock = threading.Lock()

    def _profile_params(self) -> dict[str, Any]:
        if self.control.control_profile == "fast":
            return {
                "motion_interval_s": 0.18,
                "yaw_deadband": 0.06,
                "pitch_deadband": 0.10,
                "max_yaw_delta": 120,
                "max_pitch_delta": 40,
                "visual_yaw_small_deadband": 0.18,
                "visual_yaw_small_max_delta": 35,
                "ema_alpha": 0.60,
                "move_time_ms": 450,
                "audio_seek_move_time_ms": 250,
                "audio_seek_window": 340,
                "audio_seek_max_step": 220,
                "audio_seek_min_step": 80,
                "audio_seek_full_scale_deg": 45.0,
                "audio_deadband_deg": 7.0,
                "audio_seek_max_steps": 4,
                "audio_seek_timeout_s": 2.0,
                "reframe_interval_s": 0.30,
                "reframe_timeout_s": 2.5,
                "reframe_yaw_micro_step": 30,
                "reframe_pitch_steps": [0, 40, -40, 75, 0],
                "reframe_move_time_ms": 450,
            }
        return {
            "motion_interval_s": 0.22,
            "yaw_deadband": 0.06,
            "pitch_deadband": 0.10,
            "max_yaw_delta": 120,
            "max_pitch_delta": 30,
            "visual_yaw_small_deadband": 0.18,
            "visual_yaw_small_max_delta": 30,
            "ema_alpha": 0.50,
            "move_time_ms": 500,
            "audio_seek_move_time_ms": 250,
            "audio_seek_window": 280,
            "audio_seek_max_step": 160,
            "audio_seek_min_step": 60,
            "audio_seek_full_scale_deg": 45.0,
            "audio_deadband_deg": 7.0,
            "audio_seek_max_steps": 3,
            "audio_seek_timeout_s": 2.0,
            "reframe_interval_s": 0.35,
            "reframe_timeout_s": 2.5,
            "reframe_yaw_micro_step": 30,
            "reframe_pitch_steps": [0, 40, -40, 75, 0],
            "reframe_move_time_ms": 420,
        }

    def stop(self) -> None:
        try:
            self.audio.stop()
        except Exception:
            pass
        try:
            self.vision.stop()
        except Exception:
            pass
        try:
            self.bus.close()
        except Exception:
            pass

    def _visual_control_params(self, params: dict[str, Any]) -> dict[str, Any]:
        visual_yaw_mode = self.vision_processing.visual_yaw_mode
        if visual_yaw_mode == "off":
            yaw_deadband = 1.0
            max_yaw_delta = 1
        elif visual_yaw_mode == "small":
            yaw_deadband = float(params["visual_yaw_small_deadband"])
            max_yaw_delta = int(params["visual_yaw_small_max_delta"])
        else:
            yaw_deadband = float(params["yaw_deadband"])
            max_yaw_delta = int(params["max_yaw_delta"])
        if self.vision_processing.visual_yaw_deadband is not None:
            yaw_deadband = float(self.vision_processing.visual_yaw_deadband)
        if self.vision_processing.visual_yaw_max_delta is not None:
            max_yaw_delta = int(self.vision_processing.visual_yaw_max_delta)
        pitch_deadband = (
            float(self.vision_processing.visual_pitch_deadband)
            if self.vision_processing.visual_pitch_deadband is not None
            else float(params["pitch_deadband"])
        )
        max_pitch_delta = (
            int(self.vision_processing.visual_pitch_max_delta)
            if self.vision_processing.visual_pitch_max_delta is not None
            else int(params["max_pitch_delta"])
        )
        return {
            "yaw_deadband": yaw_deadband,
            "max_yaw_delta": max_yaw_delta,
            "min_yaw_delta": int(self.vision_processing.visual_yaw_min_delta),
            "pitch_deadband": pitch_deadband,
            "max_pitch_delta": max_pitch_delta,
            "min_pitch_delta": int(self.vision_processing.visual_pitch_min_delta),
        }

    def _reset_audio_seek(self) -> None:
        self._audio_seek_origin_yaw = None
        self._audio_seek_started_s = 0.0
        self._audio_seek_steps = 0
        self._last_audio_seek_abs_azimuth = None
        self._last_audio_seek_direction = ""

    def _reset_reframe(self) -> None:
        self._reframe_origin_yaw = None
        self._reframe_started_s = 0.0
        self._reframe_step = 0
        self._last_reframe_step_s = 0.0
        self._reframe_active = False

    def _ensure_reframe_session(self, now_s: float, timeout_s: float) -> None:
        if (
            self._reframe_origin_yaw is None
            or now_s - self._reframe_started_s > timeout_s
            or self._reframe_step >= len(self._profile_params()["reframe_pitch_steps"])
        ):
            self._reframe_origin_yaw = self.pose.yaw
            self._reframe_started_s = now_s
            self._reframe_step = 0
            self._last_reframe_step_s = 0.0

    def _pose_reframe_target(self, now_s: float, params: dict[str, Any]) -> tuple[HeadPose, bool]:
        timeout_s = float(params["reframe_timeout_s"])
        interval_s = float(params["reframe_interval_s"])
        self._ensure_reframe_session(now_s, timeout_s)
        if now_s - self._last_reframe_step_s < interval_s:
            return self.pose, False
        steps = list(params["reframe_pitch_steps"])
        if self._reframe_step >= len(steps):
            return self.pose, False
        step = int(steps[self._reframe_step])
        pitch = self.logic.pitch.center + self.direction.pitch_up_sign * step
        target = self.logic.clamp_pose(HeadPose(self._reframe_origin_yaw or self.pose.yaw, pitch))
        if target == self.pose:
            self._reframe_step += 1
            self._last_reframe_step_s = now_s
            return self.pose, False
        return target, True

    def _body_reframe_target(
        self,
        now_s: float,
        params: dict[str, Any],
        body_target: Any,
        *,
        allow_yaw: bool = False,
    ) -> tuple[HeadPose, bool]:
        timeout_s = float(params["reframe_timeout_s"])
        interval_s = float(params["reframe_interval_s"])
        self._ensure_reframe_session(now_s, timeout_s)
        if now_s - self._last_reframe_step_s < interval_s:
            return self.pose, False
        error_x, error_y = normalized_center_error(
            body_target,
            frame_width=self.last_frame_width,
            frame_height=self.last_frame_height,
            target_offset_x_norm=self.vision_processing.target_offset_x_norm,
            target_offset_y_norm=self.vision_processing.target_offset_y_norm,
        )
        if self.vision_processing.visual_mirror_x:
            error_x = -error_x
        candidate = self.logic.apply_visual_error(
            self.pose,
            error_x,
            error_y,
            yaw_deadband=float(params["yaw_deadband"]),
            pitch_deadband=0.10,
            max_yaw_delta=int(params["reframe_yaw_micro_step"]),
            max_pitch_delta=int(params["max_pitch_delta"]),
        )
        origin_yaw = self._reframe_origin_yaw if self._reframe_origin_yaw is not None else self.pose.yaw
        yaw_window = int(params["reframe_yaw_micro_step"])
        yaw = max(origin_yaw - yaw_window, min(origin_yaw + yaw_window, candidate.yaw)) if allow_yaw else self.pose.yaw
        candidate = self.logic.clamp_pose(HeadPose(yaw, candidate.pitch))
        return candidate, candidate != self.pose

    def _target_matches_audio_region(self, target: Any) -> bool:
        if target is None:
            return False
        if len(self.last_targets) <= 1:
            return True
        direction = self.last_audio.direction
        if direction not in (AudioDirection.LEFT, AudioDirection.RIGHT):
            return True
        center_x, _center_y = target.tracking_center
        is_left_half = center_x < self.last_frame_width / 2.0
        if self.vision_processing.visual_mirror_x:
            is_left_half = not is_left_half
        return (direction == AudioDirection.LEFT and is_left_half) or (
            direction == AudioDirection.RIGHT and not is_left_half
        )

    def _target_has_acquire_evidence(self, target: Any) -> bool:
        if target is None:
            return False
        mouth_evidence = max(
            float(getattr(target, "mouth_motion_score", 0.0)),
            float(getattr(target, "mouth_audio_sync_score", 0.0)),
        )
        mouth_gate = max(0.03, self.vision_processing.mouth_evidence_threshold * 0.5)
        active_score = float(getattr(target, "active_speaker_score", 0.0))
        face_height_ratio = float(getattr(target, "face_height_ratio", 0.0))
        if face_height_ratio <= 0.0 and self.last_frame_height > 0:
            face_height_ratio = max(0.0, float(target.y2 - target.y1) / float(self.last_frame_height))
        size_evidence = face_height_ratio >= 0.12 or float(getattr(target, "area", 0.0)) >= 8000.0
        return mouth_evidence >= mouth_gate or active_score > self.vision_processing.visual_speaker_threshold + 0.02 or size_evidence

    def _reset_speaker_lock(self) -> None:
        self.locked_speaker_id = None
        self.locked_speaker_score = 0.0
        self._locked_speaker_seen_s = 0.0
        self._locked_speaker_spoke_s = 0.0

    def _reset_silent_visual_candidate(self) -> None:
        self._silent_visual_candidate_id = None
        self._silent_visual_candidate_since_s = 0.0

    def _silent_visual_elapsed_s(self, candidate: FaceTarget, now_s: float) -> float:
        if self._silent_visual_candidate_id != candidate.face_id:
            self._silent_visual_candidate_id = candidate.face_id
            self._silent_visual_candidate_since_s = now_s
            return 0.0
        if self._silent_visual_candidate_since_s <= 0.0:
            self._silent_visual_candidate_since_s = now_s
            return 0.0
        return max(0.0, now_s - self._silent_visual_candidate_since_s)

    def _effective_asd_threshold(self) -> float:
        status = self.asd_backend.status
        if status.actual == "talknet" and status.available:
            return self.vision_processing.talknet_threshold
        return self.vision_processing.visual_speaker_threshold

    def _crowded_target_rank(self, target: FaceTarget) -> tuple[float, float, float]:
        center_x, _center_y = target.tracking_center
        aim_x = self.last_frame_width * (0.5 + self.vision_processing.target_offset_x_norm)
        center_bonus = 1.0 - min(1.0, abs(center_x - aim_x) / max(1.0, self.last_frame_width / 2.0))
        return (float(target.asd_score), float(target.face_height_ratio), center_bonus)

    def _annotate_crowded_targets(self, targets: list[FaceTarget]) -> list[FaceTarget]:
        annotated: list[FaceTarget] = []
        threshold = self._effective_asd_threshold()
        audio_ready = self.state_machine._audio_is_ready(self.last_audio) if self.features.audio_enabled else False
        recent_frames = (
            self.vision.recent_frames(seconds=2.5)
            if hasattr(self.vision, "recent_frames")
            else None
        )
        sample_rate = None
        audio_samples = None
        if hasattr(self.audio, "recent_mono_audio"):
            sample_rate, audio_samples = self.audio.recent_mono_audio(seconds=2.5)
        for target in targets:
            face_height_ratio = max(0.0, float(target.y2 - target.y1) / max(1.0, float(self.last_frame_height)))
            min_ratio = (
                self.vision_processing.keep_face_height_ratio
                if self.locked_speaker_id is not None and target.face_id == self.locked_speaker_id
                else self.vision_processing.min_face_height_ratio
            )
            near_candidate = face_height_ratio >= min_ratio
            try:
                asd_score = self.asd_backend.score(
                    target,
                    audio=self.last_audio,
                    frame_width=self.last_frame_width,
                    frame_height=self.last_frame_height,
                    visual_mirror_x=self.vision_processing.visual_mirror_x,
                    frames=recent_frames,
                    audio_samples=audio_samples,
                    sample_rate=sample_rate,
                )
            except Exception as exc:
                error_text = str(exc)
                if self.asd_backend.status.actual == "talknet" and (
                    "recent face frames" in error_text or "window is too short" in error_text
                ):
                    asd_score = 0.0
                    self.log("asd_pending", {"backend": "talknet", "error": error_text})
                else:
                    self.asd_backend = RuleBasedAsdBackend(
                        requested=self.vision_processing.asd_backend,
                        fallback_message=error_text,
                    )
                    self.log("asd_fallback", {"error": error_text})
                    asd_score = self.asd_backend.score(
                        target,
                        audio=self.last_audio,
                        frame_width=self.last_frame_width,
                        visual_mirror_x=self.vision_processing.visual_mirror_x,
                    )
            mouth_evidence = max(float(target.mouth_motion_score), float(target.mouth_audio_sync_score))
            rules_mouth_ready = mouth_evidence >= max(0.12, self.vision_processing.mouth_evidence_threshold)
            backend_status = self.asd_backend.status
            active_candidate = near_candidate and asd_score >= threshold and (
                backend_status.actual == "talknet" or (audio_ready and rules_mouth_ready)
            )
            annotated.append(
                replace(
                    target,
                    face_height_ratio=face_height_ratio,
                    near_candidate=near_candidate,
                    asd_score=asd_score,
                    active_candidate=active_candidate,
                    locked=False,
                    specific_speaker=False,
                    too_far=not near_candidate,
                    backend=backend_status.actual,
                )
            )
        if annotated:
            winner = max(
                annotated,
                key=lambda t: (
                    float((t.x2 - t.x1) * (t.y2 - t.y1)),
                    float(t.score),
                    float(t.face_height_ratio),
                    float(t.area),
                ),
                default=None,
            )
            if winner is not None:
                annotated = [
                    replace(
                        t,
                        active_candidate=False,
                        locked=(t.face_id == winner.face_id),
                        specific_speaker=(t.face_id == winner.face_id),
                    )
                    for t in annotated
                ]
            else:
                annotated = [replace(t, active_candidate=False, locked=False, specific_speaker=False) for t in annotated]
        return annotated

    def _select_crowded_target(self, targets: list[FaceTarget], now_s: float) -> tuple[FaceTarget | None, DemoState]:
        threshold = self._effective_asd_threshold()
        audio_ready = self.state_machine._audio_is_ready(self.last_audio) if self.features.audio_enabled else False
        if targets:
            visual_winner = max(
                targets,
                key=lambda target: ((target.x2 - target.x1) * (target.y2 - target.y1), target.score, target.face_height_ratio),
            )
            marked_targets = []
            for target in targets:
                is_selected = target.face_id == visual_winner.face_id
                marked_targets.append(
                    replace(
                        target,
                        active_candidate=False,
                        locked=is_selected,
                        specific_speaker=is_selected,
                    )
                )
            self.last_targets = marked_targets
            self.last_target = visual_winner
            self.audio_search_allowed = bool(self.vision_processing.audio_interrupt_enabled)
            return visual_winner, DemoState(
                DemoMode.SPEAKER_LOCKED,
                audio_direction=self.last_audio.direction.value,
                visual_locked=True,
                target_label=visual_winner.label,
                last_event="visual_target_locked",
                audio_ready=audio_ready,
                target_confirmed=True,
            )
        near_targets = [target for target in targets if target.near_candidate]
        active_targets = [target for target in near_targets if target.active_candidate]
        locked_target = None
        if self.locked_speaker_id is not None:
            locked_target = next((target for target in targets if target.face_id == self.locked_speaker_id), None)
            if locked_target is not None and locked_target.near_candidate:
                self._locked_speaker_seen_s = now_s
                if locked_target.active_candidate:
                    self._locked_speaker_spoke_s = now_s
                    self.locked_speaker_score = locked_target.asd_score
            lost = locked_target is None or not locked_target.near_candidate
            stopped_speaking = now_s - self._locked_speaker_spoke_s > self.vision_processing.speaker_lock_hold_s
            lost_too_long = now_s - self._locked_speaker_seen_s > self.vision_processing.speaker_lost_timeout_s
            policy = self.vision_processing.speaker_lock_policy
            release_for_silence = policy in ("turn_hold", "interruptible") and stopped_speaking
            release_for_loss = lost and lost_too_long
            if release_for_loss or release_for_silence:
                self._reset_speaker_lock()
                locked_target = None
            elif policy == "interruptible" and active_targets:
                challengers = [target for target in active_targets if target.face_id != self.locked_speaker_id]
                if challengers:
                    challenger = max(challengers, key=self._crowded_target_rank)
                    current_score = float(locked_target.asd_score) if locked_target is not None else self.locked_speaker_score
                    if challenger.asd_score >= current_score + 0.15 and self._target_matches_audio_region(challenger):
                        locked_target = challenger
                        self.locked_speaker_id = challenger.face_id
                        self.locked_speaker_score = challenger.asd_score
                        self._locked_speaker_seen_s = now_s
                        self._locked_speaker_spoke_s = now_s
        if self.locked_speaker_id is None and active_targets:
            locked_target = max(active_targets, key=self._crowded_target_rank)
            self.locked_speaker_id = locked_target.face_id
            self.locked_speaker_score = locked_target.asd_score
            self._locked_speaker_seen_s = now_s
            self._locked_speaker_spoke_s = now_s
        if self.locked_speaker_id is not None:
            self._reset_silent_visual_candidate()
            marked_targets = []
            selected = None
            for target in targets:
                is_locked = False
                if selected is None:
                    if locked_target is not None:
                        is_locked = target is locked_target
                    else:
                        is_locked = target.face_id == self.locked_speaker_id
                marked = replace(target, locked=is_locked, specific_speaker=is_locked)
                marked_targets.append(marked)
                if is_locked:
                    selected = marked
            self.last_targets = marked_targets
            if selected is not None:
                self.last_target = selected
                self.audio_search_allowed = bool(self.vision_processing.audio_interrupt_enabled)
                return selected, DemoState(
                    DemoMode.SPEAKER_LOCKED,
                    audio_direction=self.last_audio.direction.value,
                    visual_locked=True,
                    target_label=selected.label,
                    last_event="specific_speaker_locked",
                    audio_ready=audio_ready,
                    target_confirmed=True,
                )
        self.last_targets = targets
        if active_targets:
            self._reset_silent_visual_candidate()
            candidate = max(active_targets, key=self._crowded_target_rank)
            self.last_target = candidate
            self.audio_search_allowed = True
            return None, DemoState(
                DemoMode.ASD_CONFIRM,
                audio_direction=self.last_audio.direction.value,
                target_label=candidate.label,
                last_event="active_candidate",
                audio_ready=audio_ready,
                target_confirmed=False,
            )
        if near_targets:
            candidate = max(near_targets, key=lambda target: (target.face_height_ratio, target.score))
            silent_elapsed_s = self._silent_visual_elapsed_s(candidate, now_s)
            allow_audio_search = (
                self.vision_processing.audio_search_after_silent_visual
                and audio_ready
                and silent_elapsed_s >= self.vision_processing.silent_visual_hold_s
            )
            self.last_target = candidate
            self.audio_search_allowed = allow_audio_search
            if allow_audio_search:
                return None, DemoState(
                    DemoMode.AUDIO_SEARCH,
                    audio_direction=self.last_audio.direction.value,
                    target_label=candidate.label,
                    last_event="silent_visual_audio_search",
                    audio_ready=audio_ready,
                    target_confirmed=False,
                )
            return None, DemoState(
                DemoMode.VISUAL_CANDIDATE,
                audio_direction=self.last_audio.direction.value,
                target_label=candidate.label,
                last_event="near_visual_candidate_hold" if audio_ready else "near_visual_candidate",
                audio_ready=audio_ready,
                target_confirmed=False,
            )
        self._reset_silent_visual_candidate()
        self.last_target = max(targets, key=lambda target: target.area) if targets else None
        self.audio_search_allowed = True
        mode = DemoMode.AUDIO_SEARCH if audio_ready else DemoMode.VISUAL_SCAN
        return None, DemoState(
            mode,
            audio_direction=self.last_audio.direction.value,
            last_event="audio_search_allowed" if mode == DemoMode.AUDIO_SEARCH else "visual_scan",
            audio_ready=audio_ready,
            target_confirmed=False,
        )

    def _mark_reframe_move(self, now_s: float) -> None:
        self._reframe_step += 1
        self._last_reframe_step_s = now_s

    def _stable_pitch_error(self, error_y: float, deadband: float) -> float:
        if abs(error_y) <= deadband:
            self._pitch_error_direction = 0
            self._pitch_error_hits = 0
            return 0.0
        direction = 1 if error_y > 0 else -1
        if direction != self._pitch_error_direction:
            self._pitch_error_direction = direction
            self._pitch_error_hits = 1
            return error_y
        self._pitch_error_hits += 1
        return error_y if self._pitch_error_hits >= 1 else 0.0

    def _make_logic(self) -> HeadControllerLogic:
        return HeadControllerLogic(
            yaw=AxisConfig(
                # `or` would swallow a legitimate id of 0 and fall back to 3,
                # which is a real servo id on these buses. Compare against None.
                servo_id=self.config.yaw_id if self.config.yaw_id is not None else 3,
                center=self.axis_limits.yaw_center,
                minimum=self.axis_limits.yaw_min,
                maximum=self.axis_limits.yaw_max,
            ),
            pitch=AxisConfig(
                servo_id=self.config.pitch_id,
                center=self.axis_limits.pitch_center,
                minimum=self.axis_limits.pitch_min,
                maximum=self.axis_limits.pitch_max,
            ),
            direction=self.direction,
        )

    def _make_head(self) -> TwoAxisHeadHardware:
        yaw_axis = (
            AxisConfig(
                servo_id=self.config.yaw_id,
                center=self.axis_limits.yaw_center,
                minimum=self.axis_limits.yaw_min,
                maximum=self.axis_limits.yaw_max,
            )
            if self.config.yaw_id is not None
            else None
        )
        pitch_axis = AxisConfig(
            servo_id=self.config.pitch_id,
            center=self.axis_limits.pitch_center,
            minimum=self.axis_limits.pitch_min,
            maximum=self.axis_limits.pitch_max,
        )
        return TwoAxisHeadHardware(self.bus, yaw=yaw_axis, pitch=pitch_axis)

    def _make_vision(self):
        if self.config.simulated:
            return SimulatedVisionTracker()
        return FfmpegCameraSnapshot(
            device_name=self.camera.device_name,
            video_size=self.camera.video_size,
            crop_left_half=self.camera.crop_left_half,
            fps=self.camera.fps,
            output_width=self.camera.output_width,
            face_detector_backend=self.camera.face_detector_backend,
            scrfd_model_path=self.camera.scrfd_model_path,
            scrfd_threshold=self.camera.scrfd_threshold,
            scrfd_input_size=self.camera.scrfd_input_size,
        )

    def _make_state_machine(self) -> LocatorStateMachine:
        return LocatorStateMachine(
            audio_confidence_threshold=self.audio_processing.audio_confidence_threshold,
            speech_confidence_threshold=self.audio_processing.speech_confidence_threshold,
            doa_confidence_threshold=self.audio_processing.doa_confidence_threshold,
            required_audio_hits=self.audio_processing.required_audio_hits,
            visual_speaker_threshold=self.vision_processing.visual_speaker_threshold,
            mouth_evidence_threshold=self.vision_processing.mouth_evidence_threshold,
        )

    def _persist_settings(self) -> None:
        save_settings(
            self.config.settings_path,
            SavedSettings(
                port=self.config.port,
                yaw_id=self.config.yaw_id,
                pitch_id=self.config.pitch_id,
                direction=self.direction,
                features=self.features,
                camera=self.camera,
                audio_mapping=self.audio_mapping,
                control=self.control,
                axis_limits=self.axis_limits,
                audio=self.audio_processing,
                vision=self.vision_processing,
            ),
        )

    def log(self, event: str, detail: dict[str, Any] | None = None) -> None:
        self.events.append({"ts": round(time.time(), 3), "event": event, "detail": detail or {}})
        self.events = self.events[-80:]

    def session(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "simulated": self.config.simulated,
            "servo": {
                "port": self.config.port,
                "yaw_id": self.config.yaw_id,
                "pitch_id": self.config.pitch_id,
                "safe_min": min(self.axis_limits.yaw_min, self.axis_limits.pitch_min),
                "safe_max": max(self.axis_limits.yaw_max, self.axis_limits.pitch_max),
                "axis_limits": asdict(self.axis_limits),
            },
            "audio": asdict(self.audio.status()),
            "direction": asdict(self.direction),
            "features": asdict(self.features),
            "camera": asdict(self.camera),
            "audio_config": asdict(self.audio_mapping),
            "control": asdict(self.control),
            "axis_limits": asdict(self.axis_limits),
            "audio_processing": asdict(self.audio_processing),
            "vision_processing": asdict(self.vision_processing),
        }

    def update_servo_config(self, *, yaw_id: int | None, pitch_id: int) -> dict[str, Any]:
        self.config.yaw_id = yaw_id
        self.config.pitch_id = int(pitch_id)
        self.logic = self._make_logic()
        self.head = self._make_head()
        self._persist_settings()
        self.log("servo_config", {"yaw_id": yaw_id, "pitch_id": pitch_id})
        return self.session()

    def update_hardware_config(self, *, port: str | None, yaw_id: int | None, pitch_id: int) -> dict[str, Any]:
        old_port = self.config.port
        self.config.port = choose_servo_port(port or self.config.port)
        self.config.yaw_id = yaw_id
        self.config.pitch_id = int(pitch_id)
        self.bus.close()
        if self.config.simulated:
            self.bus = SimulatedBus()
        elif self.config.port != old_port:
            self.bus = ZPServoBus(self.config.port)
        self.logic = self._make_logic()
        self.head = self._make_head()
        self._persist_settings()
        self.log("hardware_config", {"port": self.config.port, "yaw_id": yaw_id, "pitch_id": pitch_id})
        return self.session()

    def update_direction_config(self, payload: DirectionConfigRequest) -> dict[str, Any]:
        self.direction = DirectionConfig(
            yaw_left_sign=payload.yaw_left_sign,
            pitch_up_sign=payload.pitch_up_sign,
            manual_step=payload.manual_step,
        )
        self.logic = self._make_logic()
        self._persist_settings()
        self.log("direction_config", asdict(self.direction))
        return self.session()

    def _clear_visual_state(self, status: VisionStatus) -> None:
        """Drop cached visual data, reframe state and speaker lock.

        Used both when the camera is switched off and when it is switched back
        on, so neither transition leaves stale data behind for the dashboard.
        """
        self.last_frame_b64 = ""
        self.last_target = None
        self.last_targets = []
        self.last_body_target = None
        self.last_vision_status = status
        self._reset_reframe()
        self._reset_speaker_lock()
        self._reset_silent_visual_candidate()

    def update_feature_config(self, payload: FeatureConfigRequest) -> dict[str, Any]:
        camera_was_enabled = self.features.camera_enabled
        self.features = FeatureConfig(
            audio_enabled=payload.audio_enabled,
            visual_enabled=payload.visual_enabled,
            camera_enabled=payload.camera_enabled,
        )
        camera_source = self.camera.device_name or "off"
        if not self.features.camera_enabled and hasattr(self.vision, "stop"):
            self.vision.stop()
            self._clear_visual_state(VisionStatus(False, camera_source, "camera off", fps=0.0))
        elif self.features.camera_enabled and not camera_was_enabled:
            # Switching the camera back on. The capture itself restarts lazily
            # inside FfmpegCameraSnapshot.read(), so there is nothing to start
            # here -- but the "camera off" placeholder left by the branch above
            # has to go. The dashboard reads this status directly, and ticks may
            # not resume straight away (the whole loop is driven by the
            # WebSocket), which would leave it reporting a stopped camera.
            self._clear_visual_state(VisionStatus(False, camera_source, "starting", fps=0.0))
        self._persist_settings()
        self.log("feature_config", asdict(self.features))
        return self.session()

    def update_camera_config(self, payload: CameraConfigRequest) -> dict[str, Any]:
        if hasattr(self.vision, "stop"):
            self.vision.stop()
        self.camera = CameraConfig(
            device_name=(payload.device_name or "").strip() or None,
            video_size=(payload.video_size or "").strip() or None,
            crop_left_half=payload.crop_left_half,
            fps=payload.fps,
            output_width=payload.output_width,
            face_detector_backend=payload.face_detector_backend,
            scrfd_model_path=(payload.scrfd_model_path or "").strip() or None,
            scrfd_threshold=payload.scrfd_threshold,
            scrfd_input_size=payload.scrfd_input_size,
        )
        self.vision = self._make_vision()
        self.last_frame_b64 = ""
        self.last_target = None
        self.last_targets = []
        self.last_body_target = None
        self.last_vision_status = None
        self._reset_reframe()
        self._reset_speaker_lock()
        self._reset_silent_visual_candidate()
        self._persist_settings()
        self.log("camera_config", asdict(self.camera))
        return self.session()

    def update_audio_config(self, payload: AudioMappingConfigRequest) -> dict[str, Any]:
        self.audio_mapping = AudioMappingConfig(swap_channels=payload.swap_channels)
        self._persist_settings()
        self.log("audio_config", asdict(self.audio_mapping))
        return self.session()

    def update_control_config(self, payload: ControlConfigRequest) -> dict[str, Any]:
        self.control = ControlConfig(
            control_profile=payload.control_profile,
            motor_guard_ms=payload.motor_guard_ms,
        )
        self.motion_interval_s = self._profile_params()["motion_interval_s"]
        self._persist_settings()
        self.log("control_config", asdict(self.control))
        return self.session()

    def update_axis_limits_config(self, payload: AxisLimitsConfigRequest) -> dict[str, Any]:
        self.axis_limits = AxisLimitsConfig(
            yaw_min=payload.yaw_min,
            yaw_center=payload.yaw_center,
            yaw_max=payload.yaw_max,
            pitch_min=payload.pitch_min,
            pitch_center=payload.pitch_center,
            pitch_max=payload.pitch_max,
        )
        self.logic = self._make_logic()
        self.head = self._make_head()
        self.pose = self.logic.clamp_pose(self.pose)
        self._persist_settings()
        self.log("axis_limits_config", asdict(self.axis_limits))
        return self.session()

    def update_audio_processing_config(self, payload: AudioProcessingConfigRequest) -> dict[str, Any]:
        self.audio_processing = AudioProcessingConfig(
            vad_enabled=payload.vad_enabled,
            audio_confidence_threshold=payload.audio_confidence_threshold,
            speech_confidence_threshold=payload.speech_confidence_threshold,
            doa_confidence_threshold=payload.doa_confidence_threshold,
            required_audio_hits=payload.required_audio_hits,
            denoise_enabled=payload.denoise_enabled,
            denoise_dry_mix=payload.denoise_dry_mix,
            denoise_output_dir=payload.denoise_output_dir or "denoise_output",
            recording_enabled=True,
            use_denoised_for_localization=payload.use_denoised_for_localization,
        )
        self.state_machine = self._make_state_machine()
        self._persist_settings()
        self.log("audio_processing_config", asdict(self.audio_processing))
        return self.session()

    def update_vision_processing_config(self, payload: VisionProcessingConfigRequest) -> dict[str, Any]:
        self.vision_processing = VisionProcessingConfig(
            active_speaker_enabled=payload.active_speaker_enabled,
            tracking_strategy=payload.tracking_strategy,
            asd_backend=payload.asd_backend,
            speaker_lock_policy=payload.speaker_lock_policy,
            target_offset_x_norm=payload.target_offset_x_norm,
            target_offset_y_norm=payload.target_offset_y_norm,
            visual_mirror_x=payload.visual_mirror_x,
            visual_speaker_threshold=payload.visual_speaker_threshold,
            mouth_evidence_threshold=payload.mouth_evidence_threshold,
            visual_yaw_mode=payload.visual_yaw_mode,
            visual_pitch_enabled=payload.visual_pitch_enabled,
            visual_yaw_deadband=payload.visual_yaw_deadband,
            visual_pitch_deadband=payload.visual_pitch_deadband,
            visual_yaw_min_delta=payload.visual_yaw_min_delta,
            visual_yaw_max_delta=payload.visual_yaw_max_delta,
            visual_pitch_min_delta=payload.visual_pitch_min_delta,
            visual_pitch_max_delta=payload.visual_pitch_max_delta,
            min_face_height_ratio=payload.min_face_height_ratio,
            keep_face_height_ratio=payload.keep_face_height_ratio,
            talknet_threshold=payload.talknet_threshold,
            speaker_lock_hold_s=payload.speaker_lock_hold_s,
            speaker_lost_timeout_s=payload.speaker_lost_timeout_s,
            audio_interrupt_enabled=payload.audio_interrupt_enabled,
            audio_search_after_silent_visual=payload.audio_search_after_silent_visual,
            silent_visual_hold_s=payload.silent_visual_hold_s,
        )
        self.asd_backend = make_asd_backend(self.vision_processing.asd_backend)
        self._reset_speaker_lock()
        self._reset_silent_visual_candidate()
        self.state_machine = self._make_state_machine()
        self._persist_settings()
        self.log("vision_processing_config", asdict(self.vision_processing))
        return self.session()

    def snapshot(self, *, include_frame: bool = True) -> dict[str, Any]:
        mode = DemoMode.REFRAME_VISUAL.value if self._reframe_active else self.last_state.mode.value
        audio_comparison = self.audio.comparison_metrics() if hasattr(self.audio, "comparison_metrics") else None
        return {
            "running": self.running,
            "mode": mode,
            "strategy": self.vision_processing.tracking_strategy,
            "specific_speaker_detected": self.locked_speaker_id is not None,
            "locked_speaker_id": self.locked_speaker_id,
            "locked_speaker_score": round(self.locked_speaker_score, 3),
            "audio_search_allowed": self.audio_search_allowed,
            "asd_backend": self.vision_processing.asd_backend,
            "asd_backend_status": asdict(self.asd_backend.status),
            "state": {
                "audio_ready": self.last_state.audio_ready,
                "target_confirmed": self.last_state.target_confirmed,
                "audio_direction": self.last_state.audio_direction,
                "last_event": self.last_state.last_event,
            },
            "features": asdict(self.features),
            "pose": asdict(self.pose),
            "audio": {
                "direction": self.last_audio.direction.value,
                "confidence": round(self.last_audio.confidence, 3),
                "tdoa_s": round(self.last_audio.tdoa_s, 6),
                "azimuth_deg": round(self.last_audio.azimuth_deg, 2),
                "energy": round(self.last_audio.energy, 3),
                "speech_confidence": round(self.last_audio.speech_confidence, 3),
                "doa_confidence": round(self.last_audio.doa_confidence, 3),
                "peak_ratio": round(self.last_audio.peak_ratio, 3),
                "noise_state": self.last_audio.noise_state,
                "motor_suppressed": self.last_audio.motor_suppressed,
                "comparison": audio_comparison,
                "processing": asdict(self.audio_processing),
                "status": asdict(self.audio.status()),
            },
            "visual": {
                "visible": self.last_target is not None,
                "locked": self.last_state.target_confirmed,
                "target": asdict(self.last_target) if self.last_target else None,
                "targets": [asdict(target) for target in self.last_targets],
                "body_target": asdict(self.last_body_target) if self.last_body_target else None,
                "frame": self.last_frame_b64 if include_frame else "",
                "frame_width": self.last_frame_width,
                "frame_height": self.last_frame_height,
                "processing": asdict(self.vision_processing),
                "status": asdict(self.last_vision_status) if self.last_vision_status else None,
                "specific_speaker_detected": self.locked_speaker_id is not None,
                "locked_speaker_id": self.locked_speaker_id,
                "locked_speaker_score": round(self.locked_speaker_score, 3),
                "audio_search_allowed": self.audio_search_allowed,
                "asd_backend": self.vision_processing.asd_backend,
                "asd_backend_status": asdict(self.asd_backend.status),
            },
            "events": self.events[-20:],
        }

    def scan(self, ids: list[int]) -> list[int]:
        found = self.bus.scan_ids(ids)
        self.log("servo_scan", {"ids": ids, "found": found})
        return found

    def read_servo_positions(self, *, servo_id: int, samples: int, interval_ms: int) -> dict[str, Any]:
        servo_id = int(servo_id)
        samples = max(1, min(100, int(samples)))
        interval_s = max(0.0, min(1.0, int(interval_ms) / 1000.0))
        positions = []
        for index in range(samples):
            positions.append(self.bus.read_position(servo_id))
            if index + 1 < samples and interval_s > 0:
                time.sleep(interval_s)
        valid = [position for position in positions if position is not None]
        result = {
            "servo_id": servo_id,
            "positions": positions,
            "valid_count": len(valid),
            "min": min(valid) if valid else None,
            "max": max(valid) if valid else None,
            "span": (max(valid) - min(valid)) if valid else None,
        }
        self.log("servo_read", result)
        return result

    def read_servo_raw_positions(self, *, servo_id: int, samples: int, interval_ms: int) -> dict[str, Any]:
        servo_id = int(servo_id)
        samples = max(1, min(100, int(samples)))
        interval_s = max(0.0, min(1.0, int(interval_ms) / 1000.0))
        records = []
        for index in range(samples):
            response = self.bus.read_position_response(servo_id)
            position = None
            prefix = f"#{servo_id:03d}P"
            if response.startswith(prefix) and response.endswith("!"):
                value = response[len(prefix) : -1]
                if value.isdigit():
                    position = int(value)
            records.append({"raw": response, "position": position})
            if index + 1 < samples and interval_s > 0:
                time.sleep(interval_s)
        valid = [record["position"] for record in records if record["position"] is not None]
        result = {
            "servo_id": servo_id,
            "records": records,
            "valid_count": len(valid),
            "min": min(valid) if valid else None,
            "max": max(valid) if valid else None,
            "span": (max(valid) - min(valid)) if valid else None,
        }
        self.log("servo_read_raw", result)
        return result

    def direct_servo_move(self, *, servo_id: int, position: int, time_ms: int, arm: bool) -> dict[str, Any]:
        servo_id = int(servo_id)
        position = max(500, min(2500, int(position)))
        time_ms = max(20, min(10000, int(time_ms)))
        if arm:
            self.bus.move(servo_id, position, time_ms, force_arm=True)
        else:
            self.bus.move_position_only(servo_id, position, time_ms)
        result = {"servo_id": servo_id, "position": position, "time_ms": time_ms, "arm": bool(arm)}
        self.log("servo_direct_move", result)
        return result

    def visual_calibration_sample(
        self,
        *,
        yaw: int | None = None,
        pitch: int | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        target = self.last_target
        if target is None:
            result = {
                "ok": False,
                "error": "no visual target",
                "pose": asdict(self.pose),
                "frame_width": self.last_frame_width,
                "frame_height": self.last_frame_height,
            }
            self.log("visual_calibration_error", result)
            return result

        frame_width = max(1, int(self.last_frame_width))
        frame_height = max(1, int(self.last_frame_height))
        aligned_pose = self.logic.clamp_pose(
            HeadPose(
                self.pose.yaw if yaw is None else int(yaw),
                self.pose.pitch if pitch is None else int(pitch),
            )
        )
        bbox_center_x, bbox_center_y = target.center
        tracking_x, tracking_y = target.tracking_center
        aim_x = frame_width * (0.5 + self.vision_processing.target_offset_x_norm)
        aim_y = frame_height * (0.5 + self.vision_processing.target_offset_y_norm)
        raw_error_x, raw_error_y = normalized_center_error(
            target,
            frame_width=frame_width,
            frame_height=frame_height,
            target_offset_x_norm=self.vision_processing.target_offset_x_norm,
            target_offset_y_norm=self.vision_processing.target_offset_y_norm,
        )
        effective_error_x = -raw_error_x if self.vision_processing.visual_mirror_x else raw_error_x
        effective_error_y = raw_error_y
        params = self._profile_params()
        visual_limits = self._visual_control_params(params)
        control_error_x = 0.0 if self.vision_processing.visual_yaw_mode == "off" else effective_error_x
        control_error_y = effective_error_y if self.vision_processing.visual_pitch_enabled else 0.0
        suggested_pose = self.logic.apply_visual_error(
            aligned_pose,
            control_error_x,
            control_error_y,
            yaw_deadband=float(visual_limits["yaw_deadband"]),
            pitch_deadband=float(visual_limits["pitch_deadband"]),
            max_yaw_delta=int(visual_limits["max_yaw_delta"]),
            max_pitch_delta=int(visual_limits["max_pitch_delta"]),
            min_yaw_delta=int(visual_limits["min_yaw_delta"]),
            min_pitch_delta=int(visual_limits["min_pitch_delta"]),
        )
        result = {
            "ok": True,
            "ts": time.time(),
            "note": str(note or "")[:120],
            "frame_width": frame_width,
            "frame_height": frame_height,
            "pose": asdict(self.pose),
            "aligned_pose": asdict(aligned_pose),
            "target": asdict(target),
            "bbox_center": {"x": bbox_center_x, "y": bbox_center_y},
            "tracking_center": {"x": tracking_x, "y": tracking_y, "source": target.tracking_source},
            "aim": {"x": aim_x, "y": aim_y},
            "pixel_error": {"x": tracking_x - aim_x, "y": tracking_y - aim_y},
            "normalized_error": {"x": raw_error_x, "y": raw_error_y},
            "effective_error": {"x": effective_error_x, "y": effective_error_y},
            "offset_needed_for_tracking": {
                "x_norm": max(-0.25, min(0.25, tracking_x / frame_width - 0.5)),
                "y_norm": max(-0.25, min(0.25, tracking_y / frame_height - 0.5)),
            },
            "offset_needed_for_bbox_center": {
                "x_norm": max(-0.25, min(0.25, bbox_center_x / frame_width - 0.5)),
                "y_norm": max(-0.25, min(0.25, bbox_center_y / frame_height - 0.5)),
            },
            "visual_config": asdict(self.vision_processing),
            "model_suggested_pose": asdict(suggested_pose),
            "model_delta_from_aligned": {
                "yaw": suggested_pose.yaw - aligned_pose.yaw,
                "pitch": suggested_pose.pitch - aligned_pose.pitch,
            },
        }
        self.visual_calibration_samples.append(result)
        self.visual_calibration_samples = self.visual_calibration_samples[-50:]
        self.log(
            "visual_calibration_sample",
            {
                "aligned_pose": result["aligned_pose"],
                "pixel_error": result["pixel_error"],
                "model_delta_from_aligned": result["model_delta_from_aligned"],
                "offset_needed_for_tracking": result["offset_needed_for_tracking"],
            },
        )
        return result

    def move(self, pose: HeadPose, time_ms: int, *, source: str = "manual_move") -> HeadPose:
        safe_pose = self.logic.clamp_pose(pose)
        previous_pose = self.pose
        self.head.move_to(safe_pose, time_ms=time_ms, current=previous_pose)
        self.pose = safe_pose
        now_s = time.time()
        self.last_motion_s = now_s
        self.motor_guard_until_s = now_s + max(0, int(time_ms)) / 1000.0 + self.control.motor_guard_ms / 1000.0
        changed_axes = []
        if previous_pose.yaw != safe_pose.yaw:
            changed_axes.append("yaw")
        if previous_pose.pitch != safe_pose.pitch:
            changed_axes.append("pitch")
        event_name = "auto_move" if source == "auto_move" else "manual_move"
        self.log(
            event_name,
            {
                "from": asdict(previous_pose),
                "target": asdict(safe_pose),
                "changed_axes": changed_axes,
                "time_ms": int(time_ms),
            },
        )
        return safe_pose

    def jog(self, direction: str, amount: int | None, time_ms: int) -> HeadPose:
        target = self.logic.jog(self.pose, direction, amount)
        safe_pose = self.logic.clamp_pose(target)
        self.head.move_to(safe_pose, time_ms=time_ms, current=self.pose)
        self.pose = safe_pose
        now_s = time.time()
        self.last_motion_s = now_s
        self.motor_guard_until_s = now_s + max(0, int(time_ms)) / 1000.0 + self.control.motor_guard_ms / 1000.0
        self.log("manual_jog", {"direction": direction, **asdict(safe_pose)})
        return safe_pose

    def tick(self, *, include_frame: bool = True) -> dict[str, Any]:
        if not self._tick_lock.acquire(blocking=False):
            return self.snapshot(include_frame=include_frame)
        try:
            return self._tick_locked(include_frame=include_frame)
        finally:
            self._tick_lock.release()

    def _tick_locked(self, *, include_frame: bool = True) -> dict[str, Any]:
        now = time.time()
        raw_audio = apply_audio_channel_mapping(
            self.audio.read_estimate(),
            swap_channels=self.audio_mapping.swap_channels,
        )
        self.last_audio = with_motor_suppression(raw_audio, suppressed=now < self.motor_guard_until_s)
        if not self.audio_processing.vad_enabled and not self.last_audio.motor_suppressed:
            self.last_audio = classify_audio_direction(
                self.last_audio.tdoa_s,
                self.last_audio.energy,
                min_energy=0.06,
                speech_confidence=max(self.last_audio.speech_confidence, self.last_audio.confidence, 0.6),
                doa_confidence=max(self.last_audio.doa_confidence, self.last_audio.confidence, 0.25),
                peak_ratio=self.last_audio.peak_ratio,
                noise_state="vad_disabled",
                azimuth_deg=self.last_audio.azimuth_deg,
            )
        control_target = None
        crowded_state = None
        if self.features.camera_enabled:
            frame = self.vision.read()
            self.last_frame_b64 = frame.jpeg_base64
            self.last_frame_width = frame.frame_width
            self.last_frame_height = frame.frame_height
            self.last_targets = list(getattr(frame, "targets", []) or ([] if frame.target is None else [frame.target]))
            self.last_body_target = getattr(frame, "body_target", None)
            use_crowded = (
                self.vision_processing.tracking_strategy == "crowded_visual_first"
                and self.vision_processing.active_speaker_enabled
            )
            if use_crowded:
                self.last_targets = self._annotate_crowded_targets(self.last_targets)
                control_target, crowded_state = self._select_crowded_target(self.last_targets, now)
            elif self.vision_processing.active_speaker_enabled:
                self.audio_search_allowed = True
                self._reset_speaker_lock()
                active_target = choose_active_speaker_target(
                    self.last_targets,
                    audio_direction=self.last_audio.direction.value,
                    frame_width=frame.frame_width,
                    min_score=self.vision_processing.visual_speaker_threshold,
                )
                self.last_target = active_target or frame.target
                control_target = active_target
            else:
                self.last_target = (
                    replace(frame.target, active_speaker_score=1.0, mouth_motion_score=1.0)
                    if frame.target is not None
                    else None
                )
                control_target = self.last_target
                self.audio_search_allowed = True
                self._reset_speaker_lock()
            self.last_vision_status = frame.status
        else:
            self.last_frame_b64 = ""
            self.last_target = None
            self.last_targets = []
            self.last_body_target = None
            self.last_vision_status = VisionStatus(False, self.camera.device_name or "off", "camera off", fps=0.0)
            self.audio_search_allowed = True
            self._reset_speaker_lock()
        audio_for_control = self.last_audio if self.features.audio_enabled else None
        target_for_control = control_target if self.features.visual_enabled else None
        self.last_state = (
            crowded_state
            if crowded_state is not None and self.features.visual_enabled
            else self.state_machine.update(audio=audio_for_control, target=target_for_control, now_s=now)
        )
        self._reframe_active = False
        if self.last_state.audio_ready:
            self._last_reframe_audio_s = now
        next_pose = self.pose
        audio_seek_commanded = False
        audio_seek_attempted = False
        reframe_commanded = False
        effective_audio_azimuth = self.last_audio.azimuth_deg
        if abs(effective_audio_azimuth) < 1.0 and self.last_audio.direction in (AudioDirection.LEFT, AudioDirection.RIGHT):
            effective_audio_azimuth = azimuth_from_tdoa(self.last_audio.tdoa_s)
            if abs(effective_audio_azimuth) < 1.0:
                effective_audio_azimuth = 35.0 if self.last_audio.direction == AudioDirection.LEFT else -35.0
        audio_seek_abs_azimuth = abs(effective_audio_azimuth)
        audio_seek_direction = self.last_audio.direction.value
        move_time_ms = int(self._profile_params()["move_time_ms"])
        if self.running:
            motor_settling = now < self.motor_guard_until_s
            if motor_settling:
                self._smoothed_error = None
                self._pitch_error_direction = 0
                self._pitch_error_hits = 0
            elif self.features.visual_enabled and target_for_control is not None and self.last_state.target_confirmed:
                error_x, error_y = normalized_center_error(
                    target_for_control,
                    frame_width=frame.frame_width,
                    frame_height=frame.frame_height,
                    target_offset_x_norm=self.vision_processing.target_offset_x_norm,
                    target_offset_y_norm=self.vision_processing.target_offset_y_norm,
                )
                if self.vision_processing.visual_mirror_x:
                    error_x = -error_x
                params = self._profile_params()
                if self._smoothed_error is None:
                    self._smoothed_error = (error_x, error_y)
                else:
                    alpha = float(params["ema_alpha"])
                    self._smoothed_error = (
                        alpha * error_x + (1.0 - alpha) * self._smoothed_error[0],
                        alpha * error_y + (1.0 - alpha) * self._smoothed_error[1],
                    )
                visual_limits = self._visual_control_params(params)
                control_error_x = 0.0 if self.vision_processing.visual_yaw_mode == "off" else self._smoothed_error[0]
                if self.vision_processing.visual_pitch_enabled:
                    control_error_y = self._stable_pitch_error(
                        self._smoothed_error[1],
                        deadband=float(visual_limits["pitch_deadband"]),
                    )
                else:
                    self._pitch_error_direction = 0
                    self._pitch_error_hits = 0
                    control_error_y = 0.0
                next_pose = self.logic.apply_visual_error(
                    self.pose,
                    control_error_x,
                    control_error_y,
                    yaw_deadband=float(visual_limits["yaw_deadband"]),
                    pitch_deadband=float(visual_limits["pitch_deadband"]),
                    max_yaw_delta=int(visual_limits["max_yaw_delta"]),
                    max_pitch_delta=int(visual_limits["max_pitch_delta"]),
                    min_yaw_delta=int(visual_limits["min_yaw_delta"]),
                    min_pitch_delta=int(visual_limits["min_pitch_delta"]),
                )
                self._reset_audio_seek()
                self._reset_reframe()
            elif not self.last_state.target_confirmed:
                self._smoothed_error = None
                self._pitch_error_direction = 0
                self._pitch_error_hits = 0
            no_visible_person = not self.last_targets
            params = self._profile_params()
            visible_acquire_target = (
                self.last_target
                if self.last_target is not None
                and not self.last_state.target_confirmed
                and self._target_matches_audio_region(self.last_target)
                and self._target_has_acquire_evidence(self.last_target)
                else None
            )
            if (
                self.vision_processing.tracking_strategy == "crowded_visual_first"
                and self.last_targets
                and not self.last_state.target_confirmed
            ):
                visible_acquire_target = None
            visual_reframe_available = self.features.visual_enabled and self.features.camera_enabled and (
                no_visible_person or visible_acquire_target is not None
            )
            audio_seek_ready = visible_acquire_target is None and (
                self.last_state.mode == DemoMode.SEEK_VISUAL or (no_visible_person and self.last_state.audio_ready)
                or self.last_state.mode == DemoMode.AUDIO_SEARCH
            )
            audio_seek_step_limit = int(params["audio_seek_max_steps"])
            max_audio_seek_steps = (
                audio_seek_step_limit
                if self.last_state.mode == DemoMode.AUDIO_SEARCH
                else (1 if visual_reframe_available else audio_seek_step_limit)
            )
            if (
                self.features.audio_enabled
                and self.audio_search_allowed
                and audio_seek_ready
                and not self.last_state.target_confirmed
            ):
                self._smoothed_error = None
                direction_changed = (
                    visual_reframe_available
                    and self._last_audio_seek_direction in ("left", "right")
                    and audio_seek_direction in ("left", "right")
                    and audio_seek_direction != self._last_audio_seek_direction
                )
                if (
                    self._audio_seek_origin_yaw is None
                    or direction_changed
                    or (
                        not visual_reframe_available
                        and (
                            now - self._audio_seek_started_s > float(params["audio_seek_timeout_s"])
                            or self._audio_seek_steps >= int(params["audio_seek_max_steps"])
                        )
                    )
                ):
                    self._audio_seek_origin_yaw = self.pose.yaw
                    self._audio_seek_started_s = now
                    self._audio_seek_steps = 0
                    self._last_audio_seek_abs_azimuth = None
                    self._last_audio_seek_direction = ""
                    self._reset_reframe()
                should_move = self._audio_seek_steps < max_audio_seek_steps
                if should_move and self._last_audio_seek_abs_azimuth is not None and self._audio_seek_steps > 0:
                    same_direction = audio_seek_direction == self._last_audio_seek_direction
                    diverged = same_direction and audio_seek_abs_azimuth > self._last_audio_seek_abs_azimuth + 6.0
                    same_direction_repeat = (
                        same_direction
                        and abs(audio_seek_abs_azimuth - self._last_audio_seek_abs_azimuth) < 6.0
                        and self._audio_seek_steps >= 1
                    )
                    crossed_center = (
                        not same_direction
                        and self._last_audio_seek_direction in ("left", "right")
                        and audio_seek_direction in ("left", "right")
                        and audio_seek_abs_azimuth <= float(params["audio_deadband_deg"]) * 1.5
                    )
                    should_move = not diverged and not crossed_center and not same_direction_repeat
                if should_move:
                    window = int(params["audio_seek_window"])
                    seek_min_yaw = self._audio_seek_origin_yaw - window
                    seek_max_yaw = self._audio_seek_origin_yaw + window
                    next_pose = self.logic.apply_audio_azimuth(
                        self.pose,
                        effective_audio_azimuth,
                        deadband_deg=float(params["audio_deadband_deg"]),
                        max_yaw_delta=int(params["audio_seek_max_step"]),
                        min_yaw_delta=int(params["audio_seek_min_step"]),
                        full_scale_deg=float(params["audio_seek_full_scale_deg"]),
                        seek_min_yaw=seek_min_yaw,
                        seek_max_yaw=seek_max_yaw,
                    )
                    audio_seek_attempted = True
                    audio_seek_commanded = next_pose != self.pose
                    if audio_seek_commanded:
                        self._reset_reframe()
                        move_time_ms = int(params["audio_seek_move_time_ms"])
                if audio_seek_attempted and not audio_seek_commanded and visual_reframe_available:
                    self._audio_seek_steps = max(self._audio_seek_steps, 1)
            elif self.last_state.mode == DemoMode.LISTENING:
                self._reset_audio_seek()
                self._reset_reframe()
            if self.last_state.target_confirmed:
                self._reset_audio_seek()
                self._reset_reframe()
            audio_recent_for_reframe = (
                self._last_reframe_audio_s > 0
                and now - self._last_reframe_audio_s <= float(params["reframe_timeout_s"])
            )
            yaw_aligned_for_pitch_scan = (
                self.last_state.audio_ready
                and abs(effective_audio_azimuth) <= max(float(params["audio_deadband_deg"]) * 2.0, 15.0)
            )
            audio_seek_can_continue = (
                self.last_state.mode == DemoMode.AUDIO_SEARCH
                and self.audio_search_allowed
                and self.last_state.audio_ready
                and not (audio_seek_attempted and not audio_seek_commanded)
                and self._audio_seek_steps < max_audio_seek_steps
            )
            reframe_ready = (
                self.features.audio_enabled
                and visual_reframe_available
                and not audio_seek_commanded
                and not audio_seek_can_continue
                and (audio_recent_for_reframe or yaw_aligned_for_pitch_scan)
                and (self._audio_seek_steps >= 1 or visible_acquire_target is not None)
                and now >= self.motor_guard_until_s
            )
            if reframe_ready:
                self._reframe_active = True
                if visible_acquire_target is not None:
                    next_pose, reframe_commanded = self._body_reframe_target(
                        now,
                        params,
                        visible_acquire_target,
                        allow_yaw=False,
                    )
                elif self.last_body_target is not None and getattr(self.last_body_target, "score", 0.0) >= 0.25:
                    next_pose, reframe_commanded = self._body_reframe_target(
                        now,
                        params,
                        self.last_body_target,
                        allow_yaw=False,
                    )
                else:
                    next_pose, reframe_commanded = self._pose_reframe_target(now, params)
                if reframe_commanded:
                    move_time_ms = int(params["reframe_move_time_ms"])
            elif not audio_recent_for_reframe:
                self._reset_reframe()
            if next_pose != self.pose and now - self.last_motion_s >= self.motion_interval_s:
                self.move(next_pose, time_ms=move_time_ms, source="auto_move")
                if audio_seek_commanded:
                    self._audio_seek_steps += 1
                    self._last_audio_seek_abs_azimuth = audio_seek_abs_azimuth
                    self._last_audio_seek_direction = audio_seek_direction
                if reframe_commanded:
                    self._mark_reframe_move(now)
        return self.snapshot(include_frame=include_frame)


def create_app(*, simulated: bool = False, settings_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="Locator Robot Demo")
    if settings_path is None and not simulated:
        settings_path = default_settings_path()
    runtime = DemoRuntime(RuntimeConfig(simulated=simulated, settings_path=settings_path))
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.on_event("shutdown")
    async def shutdown_event() -> None:
        runtime.stop()

    @app.get("/", response_class=HTMLResponse)
    async def root():
        return (static_dir / "index.html").read_text(encoding="utf-8")

    @app.get("/api/session")
    async def session():
        return runtime.session()

    @app.get("/api/state")
    async def state():
        return runtime.snapshot()

    @app.get("/api/devices")
    async def devices():
        return {"dshow": [asdict(device) for device in list_dshow_devices()]}

    @app.post("/api/servo/scan")
    async def scan(payload: ScanRequest):
        try:
            return {"found_ids": runtime.scan(payload.ids), "error": None}
        except Exception as exc:
            runtime.log("servo_scan_error", {"error": str(exc), "port": runtime.config.port})
            return {"found_ids": [], "error": str(exc)}

    @app.post("/api/servo/read")
    async def read_servo(payload: ServoReadRequest):
        try:
            result = await asyncio.to_thread(
                runtime.read_servo_positions,
                servo_id=payload.servo_id,
                samples=payload.samples,
                interval_ms=payload.interval_ms,
            )
            return {**result, "error": None}
        except Exception as exc:
            runtime.log("servo_read_error", {"error": str(exc), "servo_id": payload.servo_id})
            return {"servo_id": payload.servo_id, "positions": [], "error": str(exc)}

    @app.post("/api/servo/read-raw")
    async def read_servo_raw(payload: ServoReadRawRequest):
        try:
            result = await asyncio.to_thread(
                runtime.read_servo_raw_positions,
                servo_id=payload.servo_id,
                samples=payload.samples,
                interval_ms=payload.interval_ms,
            )
            return {**result, "error": None}
        except Exception as exc:
            runtime.log("servo_read_raw_error", {"error": str(exc), "servo_id": payload.servo_id})
            return {"servo_id": payload.servo_id, "records": [], "error": str(exc)}

    @app.post("/api/servo/direct-move")
    async def direct_servo_move(payload: ServoDirectMoveRequest):
        try:
            result = await asyncio.to_thread(
                runtime.direct_servo_move,
                servo_id=payload.servo_id,
                position=payload.position,
                time_ms=payload.time_ms,
                arm=payload.arm,
            )
            return {**result, "error": None}
        except Exception as exc:
            runtime.log("servo_direct_move_error", {"error": str(exc), "servo_id": payload.servo_id})
            return {"servo_id": payload.servo_id, "position": payload.position, "time_ms": payload.time_ms, "error": str(exc)}

    @app.post("/api/head/move")
    async def move(payload: MoveRequest):
        try:
            pose = runtime.move(HeadPose(payload.yaw, payload.pitch), payload.time_ms)
            return {"target": asdict(pose), "running": runtime.running, "error": None}
        except Exception as exc:
            runtime.log("manual_move_error", {"error": str(exc), "port": runtime.config.port})
            return {"target": asdict(runtime.pose), "running": runtime.running, "error": str(exc)}

    @app.post("/api/head/jog")
    async def jog(payload: JogRequest):
        try:
            pose = runtime.jog(payload.direction, payload.amount, payload.time_ms)
            return {"target": asdict(pose), "running": runtime.running, "error": None}
        except Exception as exc:
            runtime.log("manual_jog_error", {"error": str(exc), "direction": payload.direction})
            return {"target": asdict(runtime.pose), "running": runtime.running, "error": str(exc)}

    @app.get("/api/debug/visual-calibration")
    async def get_visual_calibration_samples():
        return {"samples": runtime.visual_calibration_samples[-20:]}

    @app.post("/api/debug/visual-calibration")
    async def visual_calibration_sample(payload: VisualCalibrationRequest):
        return runtime.visual_calibration_sample(yaw=payload.yaw, pitch=payload.pitch, note=payload.note)

    @app.post("/api/config/servos")
    async def config_servos(payload: ServoConfigRequest):
        return runtime.update_hardware_config(port=payload.port, yaw_id=payload.yaw_id, pitch_id=payload.pitch_id)

    @app.get("/api/config/direction")
    async def get_direction_config():
        return asdict(runtime.direction)

    @app.post("/api/config/direction")
    async def config_direction(payload: DirectionConfigRequest):
        return runtime.update_direction_config(payload)

    @app.get("/api/config/features")
    async def get_feature_config():
        return asdict(runtime.features)

    @app.post("/api/config/features")
    async def config_features(payload: FeatureConfigRequest):
        return runtime.update_feature_config(payload)

    @app.get("/api/config/camera")
    async def get_camera_config():
        return asdict(runtime.camera)

    @app.post("/api/config/camera")
    async def config_camera(payload: CameraConfigRequest):
        return runtime.update_camera_config(payload)

    @app.get("/api/config/audio")
    async def get_audio_config():
        return asdict(runtime.audio_mapping)

    @app.post("/api/config/audio")
    async def config_audio(payload: AudioMappingConfigRequest):
        return runtime.update_audio_config(payload)

    @app.get("/api/config/control")
    async def get_control_config():
        return asdict(runtime.control)

    @app.post("/api/config/control")
    async def config_control(payload: ControlConfigRequest):
        return runtime.update_control_config(payload)

    @app.get("/api/config/axis-limits")
    async def get_axis_limits_config():
        return asdict(runtime.axis_limits)

    @app.post("/api/config/axis-limits")
    async def config_axis_limits(payload: AxisLimitsConfigRequest):
        return runtime.update_axis_limits_config(payload)

    @app.get("/api/config/audio-processing")
    async def get_audio_processing_config():
        return asdict(runtime.audio_processing)

    @app.post("/api/config/audio-processing")
    async def config_audio_processing(payload: AudioProcessingConfigRequest):
        return runtime.update_audio_processing_config(payload)

    @app.get("/api/config/vision-processing")
    async def get_vision_processing_config():
        return asdict(runtime.vision_processing)

    @app.post("/api/config/vision-processing")
    async def config_vision_processing(payload: VisionProcessingConfigRequest):
        return runtime.update_vision_processing_config(payload)

    @app.get("/api/serial")
    async def serial_ports():
        return {"ports": [asdict(port) for port in list_serial_ports()], "selected": runtime.config.port}

    @app.post("/api/demo/start")
    async def start_demo():
        runtime.running = True
        runtime.log("demo_start")
        return {"running": runtime.running}

    @app.post("/api/demo/stop")
    async def stop_demo():
        runtime.running = False
        runtime.log("demo_stop")
        return {"running": runtime.running}

    @app.post("/api/demo/tick")
    async def tick():
        return await asyncio.to_thread(runtime.tick)

    @app.websocket("/ws")
    async def websocket(websocket: WebSocket):
        await websocket.accept()
        next_frame_s = 0.0
        try:
            while True:
                now_s = time.monotonic()
                include_frame = now_s >= next_frame_s
                snapshot = await asyncio.to_thread(runtime.tick, include_frame=include_frame)
                await websocket.send_json(snapshot)
                if include_frame:
                    # next_frame_s = now_s + 0.5
                    next_frame_s = now_s + 0.1
                await asyncio.sleep(0.1)
        except WebSocketDisconnect:
            return

    app.state.runtime = runtime
    return app
