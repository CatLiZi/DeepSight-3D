from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal
from collections import deque
from functools import wraps

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from ..asd import RuleBasedAsdBackend, make_asd_backend
from ..audio import (
    AudioDirection,
    AudioEstimate,
    apply_audio_channel_mapping,
    azimuth_from_tdoa,
    classify_audio_direction,
)
from ..audio_motion import qualify_audio_during_motion
from ..dominant_sound import DominantSoundLock
from ..audio_device import FfmpegAudioLocalizer, SimulatedAudioLocalizer
from ..array_audio import WasapiArrayAudio
from ..camera import FfmpegCameraSnapshot, SimulatedVisionTracker, VisionStatus
from ..devices import list_dshow_devices, list_wasapi_inputs
from ..stereo_vision import StereoVisionTracker
from ..motion import SmoothHeadHardware
from ..head import AxisConfig, DirectionConfig, HeadControllerLogic, HeadPose, DEFAULT_YAW_MIN, DEFAULT_YAW_CENTER, DEFAULT_YAW_MAX
from ..orchestrator import DemoMode, DemoState, LocatorStateMachine
from ..servo_bus import TwoAxisHeadHardware, ZPServoBus, choose_servo_port, list_serial_ports, is_microphone_port, servo_port_candidates
from ..settings import (
    AudioMappingConfig,
    AudioCaptureConfig,
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
    validate_servo_ids,
    validate_axis_limits,
)
from ..vision import FaceTarget, choose_active_speaker_target, normalized_center_error
from ..fixation import fixation_overlay
from ..diagnostics import DiagnosticJournal, request_id
from ..visual_trigger import VisualAxisTrigger
from ..visual_servo import ImageVelocity, adaptive_visual_step
from ..capture import StereoCameraCapture


class MotionRequestCancelled(RuntimeError):
    """A stop or newer intent invalidated a pending movement request."""


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

    @model_validator(mode='after')
    def check_axes(self):
        validate_servo_ids(self.yaw_id, self.pitch_id)
        return self


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
    fps: int | None = Field(None, ge=1, le=240)
    output_width: int = Field(1920, ge=320, le=1920)
    capture_backend: Literal["stereo_mjpeg", "legacy"] = "stereo_mjpeg"
    input_fps: int = Field(60, ge=1, le=240)
    usb_vid: str = Field("1BCF", pattern=r"^[0-9a-fA-F]{4}$")
    usb_pid: str = Field("0B15", pattern=r"^[0-9a-fA-F]{4}$")
    tracking_eye: Literal["auto", "left", "right"] = "auto"
    left_rotation: Literal[0, 180] = 0
    right_rotation: Literal[0, 180] = 0
    multi_scale_search: bool = True
    focus_assist: bool = True
    preview_jpeg_quality: int = Field(95, ge=70, le=100)
    stereo_calibration_path: str = "calibration/stereo_camera.json"
    stereo_geometry_enabled: bool = True
    face_detector_backend: str = "mediapipe"
    scrfd_model_path: str | None = None
    scrfd_threshold: float = Field(0.35, ge=0.0, le=1.0)
    scrfd_input_size: int = Field(640, ge=320, le=1280)


class StereoCalibrationRequest(BaseModel):
    square_mm: float = Field(20.,ge=10.,le=50.)
    scale_confirmed: bool = False


class AudioMappingConfigRequest(BaseModel):
    swap_channels: bool = False


class AudioCaptureConfigRequest(BaseModel):
    backend: Literal["wasapi", "legacy"] = "wasapi"
    device_name: str = "YDPI4MIC"
    channels: int = Field(4, ge=4, le=4)
    sample_rate: int = Field(48000, ge=48000, le=48000)
    sample_bits: int = Field(16, ge=16, le=16)
    block_ms: int = Field(20, ge=5, le=100)
    usb_vid: str = Field("EF09", pattern=r"^[0-9a-fA-F]{4}$")
    usb_pid: str = Field("459D", pattern=r"^[0-9a-fA-F]{4}$")
    wasapi_raw: bool = True
    localization_mode: Literal["disabled", "ear_pair"] = "disabled"
    ear_left_channel: int | None = Field(None, ge=1, le=4)
    ear_right_channel: int | None = Field(None, ge=1, le=4)
    ear_mapping_confirmed: bool = False
    ear_spacing_m: float | None = Field(None, ge=.02, le=.5)
    ear_delay_offset_us: float = Field(0, ge=-1000, le=1000)

    @model_validator(mode="after")
    def check_ear_pair(self):
        AudioCaptureConfig(**self.model_dump())
        return self


class ControlConfigRequest(BaseModel):
    control_profile: str = "stable"
    motor_guard_ms: int = 250
    smooth_motion: bool = True
    motion_hz: int = Field(50, ge=10, le=100)
    max_velocity: float = Field(180, ge=20, le=600)
    max_acceleration: float = Field(600, ge=50, le=3000)
    max_jerk: float = Field(3000, ge=200, le=20000)


class AxisLimitsConfigRequest(BaseModel):
    yaw_min: int = DEFAULT_YAW_MIN
    yaw_center: int = DEFAULT_YAW_CENTER
    yaw_max: int = DEFAULT_YAW_MAX
    pitch_min: int = 1200
    pitch_center: int = 1500
    pitch_max: int = 1800

    @model_validator(mode='after')
    def check_bounds(self):
        validate_axis_limits(self.model_dump())
        return self


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
    audio_sector_half_width_deg: float = Field(15.,ge=5.,le=30.)
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
    port: str | None = "COM14"
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

    def arm(self, servo_id: int, *, force=False):
        self._armed_ids.add(int(servo_id))

    def activate_axis(self, axis, *, expected_position=None):
        self.arm(axis.servo_id)
        return self.positions.get(axis.servo_id,expected_position if expected_position is not None else axis.center)

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
        self.journal = DiagnosticJournal(config.settings_path.parent / 'logs' if config.settings_path and not config.simulated else None)
        self._events_lock = threading.Lock()
        self._intent_lock = threading.RLock()
        self._start_generation = 0
        self._motion_fault = ''
        self._feedback = {}
        self._feedback_misses = {}
        self._feedback_worker = None
        saved = load_settings(config.settings_path)
        if saved.port:
            self.config.port = saved.port
        self.config.yaw_id = saved.yaw_id
        self.config.pitch_id = saved.pitch_id
        self.direction = saved.direction
        self.features = saved.features
        self.camera = saved.camera
        self.audio_mapping = saved.audio_mapping
        self.audio_capture = saved.audio_capture
        self.control = saved.control
        self.axis_limits = saved.axis_limits
        self.audio_processing = saved.audio
        self.vision_processing = saved.vision
        if not config.simulated:
            self.config.port = choose_servo_port(config.port)
        self.logic = self._make_logic()
        self.bus = SimulatedBus() if config.simulated else ZPServoBus(config.port)
        self._attach_bus_diagnostics()
        self.head = self._make_head()
        self.audio = self._make_audio()
        self.vision = self._make_vision()
        self.state_machine = self._make_state_machine()
        self.running = False
        self._motion_pose_synced = False
        self.pose = self.logic.center_pose
        self.last_audio = AudioEstimate(AudioDirection.UNKNOWN, 0.0, 0.0, 0.0)
        self.last_raw_audio = self.last_audio
        self._audio_motion_context = {}
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
        self._set_vision_selection_policy()
        self.last_state = self.state_machine.update(audio=None, target=None, now_s=time.time())
        self.events: list[dict[str, Any]] = []
        self.last_frame_b64 = ""
        self.last_stereo_frame_b64 = ""
        self.last_focus_previews = {}
        self.last_eye_frames = {}
        self.last_eye_targets = {}
        self.last_binocular = {}
        self._last_control_eye = ""
        self._last_coordinate_frame = ""
        self._last_control_detection_sequence = 0
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
        self._last_visual_identity=None
        self._visual_approach={}
        self._visual_triggers = {axis: VisualAxisTrigger() for axis in ('yaw', 'pitch')}
        self._visual_control = {}
        self._image_velocity = ImageVelocity()
        self._last_visual_log = ('', 0.)
        self._last_audio_association_log = (None,0.)
        self._pitch_error_direction = 0
        self._pitch_error_hits = 0
        self._audio_seek_origin_yaw: int | None = None
        self._audio_seek_started_s = 0.0
        self._audio_seek_steps = 0
        self._last_audio_seek_abs_azimuth: float | None = None
        self._last_audio_seek_direction = ""
        self._last_audio_seek_observation = None
        self._audio_seek_stop_reason = ""
        self._dominant_sound = DominantSoundLock()
        self._dominant_context = None
        self._core_motion_authorized = False
        self._core_motion_reason = 'awaiting_current_core_evidence'
        self._last_core_sound_log = (None, 0.)
        self._servo_discovery = {'reason':'not_checked','port':self.config.port}
        self._stereo_preflight = {'status':'not_checked','message':'开始运行前将检查双目画面'}
        self._reframe_origin_yaw: int | None = None
        self._reframe_started_s = 0.0
        self._reframe_step = 0
        self._last_reframe_step_s = 0.0
        self._last_reframe_audio_s = 0.0
        self._reframe_active = False
        self.visual_calibration_samples: list[dict[str, Any]] = []
        self._tick_lock = threading.RLock()
        self._control_stop = threading.Event()
        self._control_worker = None
        self._control_times = deque(maxlen=300)
        self.log('runtime_started', {'port': self.config.port, 'yaw_id': self.config.yaw_id, 'pitch_id': self.config.pitch_id})

    def _attach_bus_diagnostics(self):
        if isinstance(self.bus, ZPServoBus):
            self.bus.diagnostic_sink = lambda event, detail, error=False: self.journal.record(
                event, detail, kind='serial', level='error' if error else 'info')

    def begin_start(self):
        with self._intent_lock:
            self._start_generation += 1
            return self._start_generation

    def motion_generation(self):
        with self._intent_lock:
            return self._start_generation

    def _check_motion_generation(self, generation):
        with self._intent_lock:
            if generation != self._start_generation:
                raise MotionRequestCancelled('动作请求已被停止或新请求取消；请重新操作')

    def finish_start(self, generation):
        with self._intent_lock:
            if generation != self._start_generation:
                raise RuntimeError('启动已被停止操作取消；请重新点击开始')
            if self._motion_fault:
                raise RuntimeError(self._motion_fault)
            self.running = True
        self.log('demo_start')

    def halt_motion(self, reason='', *, fault=False):
        # Invalidate pending requests before waiting for feedback or dispatch.
        with self._intent_lock:
            self._start_generation += 1
            generation = self._start_generation
            self.running = False
            self._core_motion_authorized = False
            self._core_motion_reason = 'stopped'
            if fault:
                self._motion_pose_synced = False
                self._motion_fault = reason
        if hasattr(self.head, 'cancel'):
            self.head.cancel()
        if fault:
            self.log('motion_feedback_error', {'error': reason, 'feedback': self._feedback})
        return generation

    def _feedback_loop(self):
        while not self._control_stop.wait(1.0):
            head = self.head
            metrics = head.metrics() if hasattr(head, 'metrics') else {}
            if metrics.get('error'):
                if not self._motion_fault:
                    self.halt_motion(metrics['error'], fault=True)
                continue
            if self.config.simulated or not self.bus.connected or not (self.running or metrics.get('prepared') and metrics.get('worker_active')):
                continue
            for axis in (head.yaw, head.pitch):
                if axis is None or head is not self.head:
                    continue
                try:
                    raw = self.bus.read_position_response(axis.servo_id, timeout_s=.12)
                    value = self.bus.parse_position(raw, axis.servo_id)
                    error = ''
                except Exception as exc:
                    value, raw, error = None, '', str(exc)
                # Release serial IO before taking the runtime lock. A manual
                # resync may have replaced this head while the read was pending.
                with self._tick_lock:
                    if head is not self.head:
                        break
                    valid = value is not None and axis.minimum <= value <= axis.maximum
                    self._feedback[axis.servo_id] = dict(position=value, raw=raw, ts=time.time(), valid=valid, error=error)
                    self._feedback_misses[axis.servo_id] = 0 if valid else self._feedback_misses.get(axis.servo_id, 0) + 1
                    if self._feedback_misses[axis.servo_id] >= 2:
                        self.halt_motion(f'Servo {axis.servo_id} has no valid position feedback; motion remains stopped', fault=True)
                        head.stop() if hasattr(head, 'stop') else None
                        break

    @property
    def control_loop_active(self):
        return self._control_worker is not None and self._control_worker.is_alive()

    def start_control_loop(self):
        if self.config.simulated or self.control_loop_active:
            return
        self._control_stop.clear()
        self._control_worker = threading.Thread(target=self._control_loop, daemon=True)
        self._control_worker.start()
        self._feedback_worker = threading.Thread(target=self._feedback_loop, daemon=True)
        self._feedback_worker.start()

    def _control_loop(self):
        deadline = time.perf_counter()
        while not self._control_stop.is_set():
            try:
                self.tick(include_frame=False)
                with self._tick_lock:
                    self._control_times.append(time.perf_counter())
            except Exception as exc:
                self.halt_motion(str(exc), fault=True)
                self.log("control_tick_error", {"error": str(exc)})
            deadline += 1 / 30
            now = time.perf_counter()
            if deadline < now:
                deadline = now + 1 / 30
            self._control_stop.wait(max(0, deadline - now))

    @staticmethod
    def _serialized(method):
        @wraps(method)
        def call(self, *args, **kwargs):
            with self._tick_lock:
                return method(self, *args, **kwargs)
        return call

    @staticmethod
    def _motion_serialized(method):
        @wraps(method)
        def call(self, *args, generation=None, **kwargs):
            # Capture before waiting for the runtime lock. HTTP callers also
            # capture before submitting work to the thread pool.
            generation = self.motion_generation() if generation is None else generation
            try:
                with self._tick_lock:
                    self._check_motion_generation(generation)
                    return method(self, *args, generation=generation, **kwargs)
            except MotionRequestCancelled:
                self.log('motion_request_cancelled', {'method': method.__name__, 'generation': generation})
                raise
        return call

    def _submit_pose(self, pose, *, time_ms, current, generation):
        self._check_motion_generation(generation)
        head = self.head
        # Never hold the intent lock across backend IO. Stop must still be
        # able to invalidate the request if a backend submission is delayed.
        head.move_to(pose, time_ms=time_ms, current=current)
        with self._intent_lock:
            cancelled = generation != self._start_generation
            if not cancelled:
                self.pose = pose
                now_s = time.time()
                self.last_motion_s = now_s
                self.motor_guard_until_s = now_s + max(0, int(time_ms))/1000.0 + self.control.motor_guard_ms/1000.0
        if cancelled:
            # A late enqueue may have arrived after Stop already cancelled
            # this head. Discard that trajectory and do not publish its goal.
            # An already transmitted driver write cannot be retracted here.
            if hasattr(head, 'cancel'):
                head.cancel()
            raise MotionRequestCancelled('动作请求已被停止或新请求取消；迟到轨迹已清除')

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
        self.halt_motion()
        self._control_stop.set()
        if self._control_worker and self._control_worker is not threading.current_thread():
            self._control_worker.join(timeout=15)
        if self._feedback_worker and self._feedback_worker is not threading.current_thread():
            self._feedback_worker.join(timeout=2)
        if hasattr(self.head, "stop"):
            self.head.stop()
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

    def shutdown(self):
        self.stop()
        self.log('runtime_stopped')
        self.journal.close()

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
            else .04 if self.vision_processing.tracking_strategy == 'visual_priority' else float(params["pitch_deadband"])
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
        self._last_audio_seek_observation = None
        self._audio_seek_stop_reason = ""

    def _record_audio_association(self, now):
        raw=self.last_raw_audio
        if raw.speech_confidence<.5:
            return
        selection=self.last_binocular.get('target_selection',{})
        key=(raw.direction.value,raw.noise_state,self.last_audio.noise_state,selection.get('selected_person_id'))
        previous,at=self._last_audio_association_log
        if now-at<.75 or (key==previous and now-at<2.):
            return
        self._last_audio_association_log=(key,now)
        candidates=[{k:row.get(k) for k in ('person_id','azimuth_left_deg','face_interval_deg',
            'in_sector','angular_gap_deg','match_score')} for row in selection.get('candidates',[])[:5]]
        self.log('voice_direction_diagnostic',{
            'raw_direction':raw.direction.value,'raw_azimuth_deg':raw.azimuth_deg,'tdoa_us':raw.tdoa_s*1e6,
            'speech_probability':raw.speech_confidence,'doa_confidence':raw.doa_confidence,
            'temporal_stability':raw.temporal_stability,'raw_reason':raw.noise_state,
            'delivered_direction':self.last_audio.direction.value,'motion_context':dict(self._audio_motion_context),
            'features':asdict(self.features),'running':self.running,'selection_mode':selection.get('mode'),
            'selection_reason':selection.get('audio_reason'),'selected_person_id':selection.get('selected_person_id'),
            'candidates':candidates,'angles_are_estimates_not_calibration_truth':True})

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
        return mouth_evidence >= mouth_gate or active_score > self.vision_processing.visual_speaker_threshold + 0.02

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
        return annotated

    def _select_crowded_target(self, targets: list[FaceTarget], now_s: float) -> tuple[FaceTarget | None, DemoState]:
        threshold = self._effective_asd_threshold()
        audio_ready = self.state_machine._audio_is_ready(self.last_audio) if self.features.audio_enabled else False
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

    def _make_head(self, *, only_axis: str | None = None) -> TwoAxisHeadHardware:
        yaw_axis = (
            AxisConfig(
                servo_id=self.config.yaw_id,
                center=self.axis_limits.yaw_center,
                minimum=self.axis_limits.yaw_min,
                maximum=self.axis_limits.yaw_max,
            )
            if self.config.yaw_id is not None and only_axis != "pitch"
            else None
        )
        pitch_axis = AxisConfig(
            servo_id=self.config.pitch_id,
            center=self.axis_limits.pitch_center,
            minimum=self.axis_limits.pitch_min,
            maximum=self.axis_limits.pitch_max,
        )
        head = TwoAxisHeadHardware(self.bus, yaw=yaw_axis, pitch=pitch_axis if only_axis != "yaw" else None)
        if self.control.smooth_motion and not self.config.simulated:
            return SmoothHeadHardware(head, initial=getattr(self, "pose", self.logic.center_pose), config=self.control)
        return head

    def _rebuild_head(self):
        previous = getattr(self, "head", None)
        estimated = getattr(previous, "estimated_pose", self.pose)
        if hasattr(previous, "stop"):
            previous.stop()
        self.pose = self.logic.clamp_pose(estimated)
        self.head = self._make_head()

    def _make_audio(self):
        if self.config.simulated:
            return SimulatedAudioLocalizer()
        if self.audio_capture.backend == "wasapi":
            source = WasapiArrayAudio(device_name=self.audio_capture.device_name, channels=self.audio_capture.channels,
                                     sample_rate=self.audio_capture.sample_rate, block_ms=self.audio_capture.block_ms,
                                     usb_vid=self.audio_capture.usb_vid, usb_pid=self.audio_capture.usb_pid,
                                     wasapi_raw=self.audio_capture.wasapi_raw,ear_config=self.audio_capture.ear_pair_config(),
                                     speech_vad=self.audio_processing.vad_enabled,
                                     recording_enabled=self.audio_processing.recording_enabled,
                                     output_dir=self.audio_processing.denoise_output_dir)
            source.diagnostic_sink = lambda event, detail: self.journal.record(event, detail,
                level='error' if event == 'audio_capture_fault' else 'info')
            return source
        return FfmpegAudioLocalizer(device_name=self.audio_capture.device_name,
                                     recording_enabled=self.audio_processing.recording_enabled,
                                     denoise=self.audio_processing.denoise_enabled,
                                     denoise_dry_mix=self.audio_processing.denoise_dry_mix,
                                     denoise_output_dir=self.audio_processing.denoise_output_dir,
                                     use_denoised_for_localization=self.audio_processing.use_denoised_for_localization)

    def _make_vision(self):
        if self.config.simulated:
            return SimulatedVisionTracker()
        tracker = StereoVisionTracker if self.camera.capture_backend == "stereo_mjpeg" else FfmpegCameraSnapshot
        capture_options = ({"input_fps": self.camera.input_fps, "usb_vid": self.camera.usb_vid,
                            "usb_pid": self.camera.usb_pid, "tracking_eye": self.camera.tracking_eye,
                            "left_rotation": self.camera.left_rotation, "right_rotation": self.camera.right_rotation}
                           if tracker is StereoVisionTracker else {})
        if tracker is StereoVisionTracker:
            capture_options['audio_visual_guided']=self.vision_processing.tracking_strategy in ('audio_visual_guided','visual_priority')
            capture_options['visual_priority']=self.vision_processing.tracking_strategy=='visual_priority'
            capture_options['audio_sector_half_width_deg']=self.vision_processing.audio_sector_half_width_deg
            capture_options.update(multi_scale_search=self.camera.multi_scale_search,
                                   focus_assist=self.camera.focus_assist,
                                   preview_jpeg_quality=self.camera.preview_jpeg_quality,
                                   stereo_calibration_path=self.camera.stereo_calibration_path,
                                   stereo_geometry_enabled=self.camera.stereo_geometry_enabled)
        result = tracker(
            **capture_options,
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
        capture = getattr(result, 'capture', None)
        if isinstance(capture, StereoCameraCapture):
            capture.diagnostic_sink = lambda event, detail: self.journal.record(event, detail, level='error')
        return result

    def _make_state_machine(self) -> LocatorStateMachine:
        return LocatorStateMachine(
            audio_confidence_threshold=self.audio_processing.audio_confidence_threshold,
            speech_confidence_threshold=self.audio_processing.speech_confidence_threshold,
            doa_confidence_threshold=self.audio_processing.doa_confidence_threshold,
            required_audio_hits=self.audio_processing.required_audio_hits,
            visual_speaker_threshold=self.vision_processing.visual_speaker_threshold,
            mouth_evidence_threshold=self.vision_processing.mouth_evidence_threshold,
            require_recent_audio=self.vision_processing.active_speaker_enabled,
        )

    def _persist_settings(self) -> None:
        settings = SavedSettings(
                port=self.config.port,
                yaw_id=self.config.yaw_id,
                pitch_id=self.config.pitch_id,
                direction=self.direction,
                features=self.features,
                camera=self.camera,
                audio_mapping=self.audio_mapping,
                audio_capture=self.audio_capture,
                control=self.control,
                axis_limits=self.axis_limits,
                audio=self.audio_processing,
                vision=self.vision_processing,
        )
        try:
            save_settings(self.config.settings_path, settings)
        except (OSError, ValueError) as exc:
            self.halt_motion()
            self.log('settings_save_error', {'error': str(exc), 'path': str(self.config.settings_path)})
            raise RuntimeError('配置保存失败，自动转动已暂停；原配置文件保留，请检查磁盘后重新保存') from exc

    def log(self, event: str, detail: dict[str, Any] | None = None) -> None:
        row = self.journal.record(event, detail, level='error' if event.endswith(('_error', '_failure')) else 'info')
        with self._events_lock:
            self.events.append(row)
            self.events = self.events[-80:]

    @_serialized
    def session(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "head_presets": {name: asdict(self.logic.pose_for_audio_direction(name))
                             for name in ('left_back', 'left_front', 'center', 'right_front', 'right_back')},
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
            "audio_capture": asdict(self.audio_capture),
            "control": asdict(self.control),
            "axis_limits": asdict(self.axis_limits),
            "audio_processing": asdict(self.audio_processing),
            "vision_processing": asdict(self.vision_processing),
        }

    @_serialized
    def update_servo_config(self, *, yaw_id: int | None, pitch_id: int) -> dict[str, Any]:
        validate_servo_ids(yaw_id, pitch_id)
        self._invalidate_motion_context()
        self.config.yaw_id = yaw_id
        self.config.pitch_id = int(pitch_id)
        self._motion_pose_synced = False
        self.logic = self._make_logic()
        self._rebuild_head()
        self._persist_settings()
        self.log("servo_config", {"yaw_id": yaw_id, "pitch_id": pitch_id})
        return self.session()

    @_serialized
    def update_hardware_config(self, *, port: str | None, yaw_id: int | None, pitch_id: int) -> dict[str, Any]:
        validate_servo_ids(yaw_id, pitch_id)
        self.halt_motion()
        self._motion_pose_synced = False
        old_port = self.config.port
        estimated = getattr(self.head, "estimated_pose", self.pose)
        if hasattr(self.head, "stop"):
            self.head.stop()
        self.pose = self.logic.clamp_pose(estimated)
        self.config.port = (port or self.config.port) if self.config.simulated else choose_servo_port(port or self.config.port)
        self.config.yaw_id = yaw_id
        self.config.pitch_id = int(pitch_id)
        self.bus.close()
        if self.config.simulated:
            self.bus = SimulatedBus()
        elif self.config.port != old_port:
            self.bus = ZPServoBus(self.config.port)
        self._attach_bus_diagnostics()
        self.logic = self._make_logic()
        self.head = self._make_head()
        self._persist_settings()
        self.log("hardware_config", {"port": self.config.port, "yaw_id": yaw_id, "pitch_id": pitch_id})
        return self.session()

    @_serialized
    def update_direction_config(self, payload: DirectionConfigRequest) -> dict[str, Any]:
        updated = DirectionConfig(
            yaw_left_sign=payload.yaw_left_sign,
            pitch_up_sign=payload.pitch_up_sign,
            manual_step=payload.manual_step,
        )
        if updated != self.direction:
            self._invalidate_motion_context()
        self.direction = updated
        self.logic = self._make_logic()
        self._persist_settings()
        self.log("direction_config", asdict(self.direction))
        return self.session()

    def _invalidate_motion_context(self, *, require_feedback=True):
        """Revoke an old-coordinate command before a configuration transition."""
        generation = self.halt_motion()
        self.pose = getattr(self.head, 'estimated_pose', self.pose)
        if require_feedback:
            self._motion_pose_synced = False
        self._dominant_sound.reset()
        self._dominant_context = None
        self._reset_audio_seek()
        self._reset_reframe()
        self._smoothed_error = None
        self._last_visual_identity = None
        self._image_velocity.reset()
        for trigger in self._visual_triggers.values():
            trigger.reset()
        return generation

    def _clear_visual_state(self, status: VisionStatus) -> None:
        """Drop cached visual data, reframe state and speaker lock.

        Used both when the camera is switched off and when it is switched back
        on, so neither transition leaves stale data behind for the dashboard.
        """
        self.last_frame_b64 = ""
        self.last_stereo_frame_b64 = ""
        self.last_target = None
        self.last_targets = []
        self.last_body_target = None
        self.last_vision_status = status
        self.last_focus_previews = {}
        self.last_eye_frames = {}
        self.last_eye_targets = {}
        self.last_binocular = {}
        self._last_control_eye = ""
        self._last_coordinate_frame = ""
        self._reset_reframe()
        self._reset_speaker_lock()
        self._reset_silent_visual_candidate()

    @_serialized
    def update_feature_config(self, payload: FeatureConfigRequest) -> dict[str, Any]:
        camera_was_enabled = self.features.camera_enabled
        updated = FeatureConfig(
            audio_enabled=payload.audio_enabled,
            visual_enabled=payload.visual_enabled,
            camera_enabled=payload.camera_enabled,
        )
        was_running = self.running
        generation = None
        if updated != self.features:
            # A mode switch revokes the old goal immediately. It can retain the
            # user's Run intent, but the new mode must supply fresh evidence.
            generation = self._invalidate_motion_context(require_feedback=False)
        self.features = updated
        camera_source = self.camera.device_name or "off"
        if not self.features.camera_enabled:
            if hasattr(self.vision, "stop"):
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
        if generation is not None:
            with self._intent_lock:
                if generation == self._start_generation and not self._motion_fault:
                    self.running = was_running and (updated.audio_enabled or
                                                     updated.visual_enabled and updated.camera_enabled)
        self.log("feature_config", asdict(self.features))
        return self.session()

    @_serialized
    def update_camera_config(self, payload: CameraConfigRequest) -> dict[str, Any]:
        # Replacing a camera invalidates the old frame/target coordinate system.
        # Cancel its queued motion before replacing any capture or detector.
        self.halt_motion()
        self._stereo_preflight={'status':'not_checked','message':'摄像头配置或对齐参数已更新；下次开始前重新检查'}
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
            capture_backend=payload.capture_backend,
            input_fps=payload.input_fps,
            usb_vid=payload.usb_vid,
            usb_pid=payload.usb_pid,
            tracking_eye=payload.tracking_eye,
            left_rotation=payload.left_rotation,
            right_rotation=payload.right_rotation,
            multi_scale_search=payload.multi_scale_search,
            focus_assist=payload.focus_assist,
            preview_jpeg_quality=payload.preview_jpeg_quality,
            stereo_calibration_path=payload.stereo_calibration_path,
            stereo_geometry_enabled=payload.stereo_geometry_enabled,
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

    @_serialized
    def update_audio_config(self, payload: AudioMappingConfigRequest) -> dict[str, Any]:
        updated = AudioMappingConfig(swap_channels=payload.swap_channels)
        if updated != self.audio_mapping:
            self._invalidate_motion_context()
        self.audio_mapping = updated
        self._persist_settings()
        self.log("audio_config", asdict(self.audio_mapping))
        return self.session()

    @_serialized
    def update_audio_capture_config(self, payload: AudioCaptureConfigRequest):
        updated = AudioCaptureConfig(**payload.model_dump())
        self._invalidate_motion_context()
        self.audio.stop()
        self.audio_capture = updated
        self.audio = self._make_audio()
        self._persist_settings()
        return self.session()

    @_serialized
    def update_control_config(self, payload: ControlConfigRequest) -> dict[str, Any]:
        self._invalidate_motion_context()
        self.control = ControlConfig(**payload.model_dump())
        self._rebuild_head()
        self.motion_interval_s = self._profile_params()["motion_interval_s"]
        self._persist_settings()
        self.log("control_config", asdict(self.control))
        return self.session()

    @_serialized
    def update_axis_limits_config(self, payload: AxisLimitsConfigRequest) -> dict[str, Any]:
        self.halt_motion()
        self.axis_limits = AxisLimitsConfig(
            yaw_min=payload.yaw_min,
            yaw_center=payload.yaw_center,
            yaw_max=payload.yaw_max,
            pitch_min=payload.pitch_min,
            pitch_center=payload.pitch_center,
            pitch_max=payload.pitch_max,
        )
        self.logic = self._make_logic()
        self._rebuild_head()
        self._motion_pose_synced = False
        self.pose = self.logic.clamp_pose(self.pose)
        self._persist_settings()
        self.log("axis_limits_config", asdict(self.axis_limits))
        return self.session()

    @_serialized
    def update_audio_processing_config(self, payload: AudioProcessingConfigRequest) -> dict[str, Any]:
        updated = AudioProcessingConfig(
            vad_enabled=payload.vad_enabled,
            audio_confidence_threshold=payload.audio_confidence_threshold,
            speech_confidence_threshold=payload.speech_confidence_threshold,
            doa_confidence_threshold=payload.doa_confidence_threshold,
            required_audio_hits=payload.required_audio_hits,
            denoise_enabled=payload.denoise_enabled,
            denoise_dry_mix=payload.denoise_dry_mix,
            denoise_output_dir=payload.denoise_output_dir or "denoise_output",
            recording_enabled=payload.recording_enabled,
            use_denoised_for_localization=payload.use_denoised_for_localization,
        )
        previous = self.audio_processing
        vad_changed = updated.vad_enabled != previous.vad_enabled
        policy_fields = ('vad_enabled', 'audio_confidence_threshold', 'speech_confidence_threshold',
                         'doa_confidence_threshold', 'required_audio_hits', 'denoise_enabled',
                         'denoise_dry_mix', 'use_denoised_for_localization')
        policy_changed = any(getattr(updated, k) != getattr(previous, k) for k in policy_fields)
        if policy_changed:
            self._invalidate_motion_context()
        self.audio_processing = updated
        if policy_changed:
            self.state_machine = self._make_state_machine()
        if isinstance(self.audio, WasapiArrayAudio):
            self.audio._output_dir = Path(self.audio_processing.denoise_output_dir) if self.audio_processing.denoise_output_dir else None
            self.audio.set_recording_enabled(self.audio_processing.recording_enabled)
            if vad_changed:
                self.audio.set_vad_enabled(self.audio_processing.vad_enabled)
        self._persist_settings()
        self.log("audio_processing_config", asdict(self.audio_processing))
        return self.session()

    def _set_vision_selection_policy(self):
        if not hasattr(self.vision,'set_guided_mode'):return
        strategy=self.vision_processing.tracking_strategy
        if strategy=='visual_priority':
            self.vision.set_guided_mode(True,visual_priority=True,
                sector_half_width_deg=self.vision_processing.audio_sector_half_width_deg)
        else:self.vision.set_guided_mode(strategy=='audio_visual_guided')

    @_serialized
    def update_vision_processing_config(self, payload: VisionProcessingConfigRequest) -> dict[str, Any]:
        self._invalidate_motion_context()
        self.vision_processing = VisionProcessingConfig(
            active_speaker_enabled=payload.active_speaker_enabled,
            tracking_strategy=payload.tracking_strategy,
            audio_sector_half_width_deg=payload.audio_sector_half_width_deg,
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
        self._set_vision_selection_policy()
        self._reset_silent_visual_candidate()
        self.state_machine = self._make_state_machine()
        self._persist_settings()
        self.log("vision_processing_config", asdict(self.vision_processing))
        return self.session()

    @_serialized
    def snapshot(self, *, include_frame: bool = True) -> dict[str, Any]:
        mode = DemoMode.REFRAME_VISUAL.value if self._reframe_active else self.last_state.mode.value
        audio_comparison = self.audio.comparison_metrics() if hasattr(self.audio, "comparison_metrics") else None
        ticks = list(self._control_times)
        tick_hz = (len(ticks) - 1) / (ticks[-1] - ticks[0]) if len(ticks) > 1 and ticks[-1] > ticks[0] else 0
        return {
            "algorithm_revision": "R43",
            "servo_connection": {'port':self.config.port,'yaw_id':self.config.yaw_id,
                'pitch_id':self.config.pitch_id,'discovery':dict(self._servo_discovery)},
            "stereo_preflight": dict(self._stereo_preflight),
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
                "capture": self.audio.capture_metrics() if hasattr(self.audio, "capture_metrics") else None,
                "raw_observation": {
                    **asdict(self.last_raw_audio),
                    "direction": self.last_raw_audio.direction.value,
                    "age_s": max(0.,time.perf_counter()-self.last_raw_audio.observed_monotonic) if self.last_raw_audio.observed_monotonic else None,
                    "scope": "acoustic observation before motor suppression; diagnostic only",
                },
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
                "motion_context": dict(self._audio_motion_context),
                "steering": {"steps":self._audio_seek_steps,"stop_reason":self._audio_seek_stop_reason,
                    "visual_primary":bool(self.features.visual_enabled and self.last_state.target_confirmed),
                    "actuator_wait":bool(time.time()<self.motor_guard_until_s and not self.features.visual_enabled)},
                "core_source": self._core_sound_status(),
                "azimuth_std_deg": self.last_audio.azimuth_std_deg,
                "temporal_stability": self.last_audio.temporal_stability,
                "speech_backend":self.last_audio.speech_backend,
                "age_s":max(0.,time.perf_counter()-self.last_audio.observed_monotonic) if self.last_audio.observed_monotonic else None,
                "comparison": audio_comparison,
                "processing": asdict(self.audio_processing),
                "status": asdict(self.audio.status()),
            },
            "visual": {
                "frame_included": include_frame,
                "capture": self.vision.capture_metrics() if hasattr(self.vision, "capture_metrics") else None,
                "stereo_frame": self.last_stereo_frame_b64 if include_frame else "",
                "focus_previews": self.last_focus_previews if include_frame else {},
                "eye_frames": self.last_eye_frames if include_frame else {},
                "eye_targets": {eye: [asdict(t) for t in targets] for eye, targets in self.last_eye_targets.items()},
                "binocular": self.last_binocular,
                "fixation": fixation_overlay(self.last_target,self.last_eye_targets,
                    (self.last_frame_width,self.last_frame_height),
                    offsets=(self.vision_processing.target_offset_x_norm,self.vision_processing.target_offset_y_norm),
                    fresh=0 <= time.perf_counter()-self.last_binocular.get('sample_captured_monotonic',0) <= .75),
                "visible": self.last_target is not None,
                "locked": self.last_state.target_confirmed,
                "target": asdict(self.last_target) if self.last_target else None,
                "targets": [asdict(target) for target in self.last_targets],
                "body_target": asdict(self.last_body_target) if self.last_body_target else None,
                "frame": self.last_frame_b64 if include_frame else "",
                "frame_width": self.last_frame_width,
                "frame_height": self.last_frame_height,
                "processing": asdict(self.vision_processing),
                "approach": dict(self._visual_approach),
                "control": dict(self._visual_control),
                "status": asdict(self.last_vision_status) if self.last_vision_status else None,
                "specific_speaker_detected": self.locked_speaker_id is not None,
                "locked_speaker_id": self.locked_speaker_id,
                "locked_speaker_score": round(self.locked_speaker_score, 3),
                "audio_search_allowed": self.audio_search_allowed,
                "asd_backend": self.vision_processing.asd_backend,
                "asd_backend_status": asdict(self.asd_backend.status),
            },
            "events": self.events[-20:],
            "diagnostics": self.journal.snapshot(),
            "motion_health": {'fault': self._motion_fault, 'pose_synced': self._motion_pose_synced,
                              'feedback': dict(self._feedback), 'position_kind': 'pose is commanded; feedback contains measured positions'},
            "motion": self.head.metrics() if hasattr(self.head, "metrics") else {"enabled": False},
            "control_loop": {"background": self.control_loop_active, "target_hz": 30, "measured_hz": round(tick_hz, 3)},
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

    @_motion_serialized
    def direct_servo_move(self, *, servo_id: int, position: int, time_ms: int, arm: bool, generation=None) -> dict[str, Any]:
        servo_id = int(servo_id)
        axis = self.logic.yaw if servo_id == self.config.yaw_id else self.logic.pitch if servo_id == self.config.pitch_id else None
        if axis is None:
            raise ValueError('Direct movement requires a configured yaw or pitch servo ID')
        position = int(position)
        if not axis.minimum <= position <= axis.maximum:
            raise ValueError(f'Servo {servo_id} target must be within configured range {axis.minimum}–{axis.maximum}')
        time_ms = max(20, min(10000, int(time_ms)))
        self._check_motion_generation(generation)
        # Diagnostic raw-position writes must not bypass the checks used by
        # ordinary arrows. `arm=False` means no torque enable, not no feedback.
        if not self.config.simulated and isinstance(self.bus,ZPServoBus):
            if self.running:raise RuntimeError('Stop automatic motion before a direct servo command')
            if hasattr(self.head,'stop'):self.head.stop()
            if hasattr(self.head,'metrics') and self.head.metrics().get('worker_active'):
                raise RuntimeError('Previous motion worker has not stopped; direct command rejected')
            self._motion_pose_synced=False
            self.bus.connect()
            session=self.bus.motion_session()
            with self.bus.guard_motion_session(session):
                before=self.bus.read_position(servo_id)
                if before is None:raise RuntimeError(f'Servo {servo_id} has no position feedback; motion remains stopped')
                if not axis.minimum<=before<=axis.maximum:
                    raise RuntimeError(f'Servo {servo_id} is outside configured limits; calibrate before motion')
                if arm:self.bus.activate_axis(axis,expected_position=before)
                elif self.bus.read_mode(servo_id)!=1:
                    raise RuntimeError(f'Servo {servo_id} mode is unknown or not 1; direct command rejected')
                self._check_motion_generation(generation)
                # Activation already happened once, when requested. No second
                # force-arm can invalidate its post-enable position check.
                self.bus.move_position_only(servo_id,position,time_ms)
        elif arm:
            self.bus.move(servo_id, position, time_ms, force_arm=False)
        else:
            self.bus.move_position_only(servo_id, position, time_ms)
        self._check_motion_generation(generation)
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

    @_motion_serialized
    def discover_servo_port(self, *, generation=None):
        """Find the adapter using fresh configured-ID replies, without activation."""
        self._check_motion_generation(generation)
        if self.running:raise RuntimeError('Stop motion before searching for the servo adapter')
        if hasattr(self.head,'stop'):self.head.stop()
        candidates=servo_port_candidates()[:8]
        trials=[];opened=[];accepted=[]
        try:
            for candidate in candidates:
                self._check_motion_generation(generation)
                bus=self.bus if candidate.device.casefold()==str(self.config.port).casefold() else ZPServoBus(candidate.device)
                if bus is not self.bus:opened.append(bus)
                readings={};error=''
                try:
                    for sid in {sid for sid in (self.config.yaw_id,self.config.pitch_id) if sid is not None}:
                        readings[str(sid)]=bus.read_position(sid)
                except Exception as exc:error=str(exc)
                identified=bool(readings) and all(v is not None for v in readings.values()) and not error
                trials.append(dict(**asdict(candidate),positions=readings,error=error,identified=identified))
                if identified:accepted.append((candidate,bus))
            preferred=[item for item in accepted if item[0].device.casefold()==str(self.config.port).casefold()]
            selected=preferred[0] if preferred else accepted[0] if len(accepted)==1 else None
            self._check_motion_generation(generation)
            if selected:
                candidate,bus=selected
                if bus is not self.bus:
                    self.bus.close();self.bus=bus;opened.remove(bus)
                    self.config.port=candidate.device;self._attach_bus_diagnostics()
                    self.head=self._make_head()
                self._motion_pose_synced=False
                self._servo_discovery=dict(ok=True,reason='configured_ids_replied',port=candidate.device,
                    device_name=candidate.description,hardware_id=candidate.hardware_id,queries_only=True,trials=trials)
                self._persist_settings()
            else:
                reason='multiple_matching_adapters' if len(accepted)>1 else 'configured_ids_not_both_replying'
                self._servo_discovery=dict(ok=False,reason=reason,port=self.config.port,queries_only=True,trials=trials)
            self.log('servo_port_discovery',self._servo_discovery)
            return dict(self._servo_discovery)
        finally:
            for bus in opened:bus.close()

    @_motion_serialized
    def sync_motion_pose(self, *, only_axis: str | None = None, generation=None):
        """Read the assembled head before enabling motion; never assume 1500."""
        if self.config.simulated:
            return
        self.running=False
        # Re-enumerate an unavailable adapter before startup/manual feedback.
        # Never replace an open session underneath a trajectory.
        ports=servo_port_candidates()
        if ports:
            present=any(p.device.casefold()==str(self.config.port).casefold() for p in ports)
            if not present and (self.config.port is not None or not self.bus.connected):
                self.discover_servo_port(generation=generation)
        if only_axis not in (None, "yaw", "pitch"):
            raise ValueError("Unknown head axis")
        self._motion_pose_synced = False
        self.running = False
        previous = self.head
        if hasattr(previous, "stop"):
            previous.stop()
        values = asdict(self.pose)
        axes = [(axis, sid) for axis, sid in (("yaw", self.config.yaw_id), ("pitch", self.config.pitch_id))
                if only_axis is None or only_axis == axis]
        errors = []
        for attempt in range(2):
            errors = []
            for axis, sid in axes:
                self._check_motion_generation(generation)
                if sid is None:
                    if only_axis == axis:
                        errors.append(f"No servo configured for {axis}")
                    continue
                value = self.bus.read_position(sid)
                self._check_motion_generation(generation)
                valid = value is not None and getattr(self.axis_limits, axis + '_min') <= value <= getattr(self.axis_limits, axis + '_max')
                self._feedback[sid] = dict(position=value, ts=time.time(), valid=valid)
                if value is None:
                    errors.append(f"Servo {sid} has no position feedback; motion remains stopped")
                elif not valid:
                    errors.append(f"Servo {sid} is outside configured limits; calibrate before motion")
                else:
                    values[axis] = value
            if not errors:
                break
            # One stopped-session recovery only. Re-read ALL requested axes;
            # never reuse a partial success from the old serial session.
            if attempt == 0 and self.bus.connected and all('feedback' in e for e in errors):
                self.log('motion_readonly_recovery', {'errors': errors, 'only_axis': only_axis})
                try:
                    self.bus.reconnect_readonly()
                    continue
                except Exception as exc:
                    errors.append(str(exc))
            break
        self._check_motion_generation(generation)
        if errors:
            with self._intent_lock:
                self._check_motion_generation(generation)
                fault = '; '.join(errors)
                self._motion_fault = fault
            self.log('motion_sync_error', {'error': fault, 'feedback': self._feedback, 'only_axis': only_axis})
            raise RuntimeError(fault)
        # Commit only the current request's feedback. This lock covers state
        # construction only; the blocking reads and stop/join happen above.
        with self._intent_lock:
            self._check_motion_generation(generation)
            self.pose = HeadPose(**values)
            self.head = self._make_head(only_axis=only_axis)
            self._motion_pose_synced = only_axis is None
            self._motion_fault = ''
            self._feedback_misses.clear()
        self.log("motion_pose_synced", {**values, "only_axis": only_axis})

    @_motion_serialized
    def move(self, pose: HeadPose, time_ms: int, *, source: str = "manual_move", generation=None) -> HeadPose:
        if self._motion_fault and source == 'auto_move':
            raise RuntimeError(self._motion_fault)
        if source == 'auto_move' and not self.running:
            raise MotionRequestCancelled('自动动作已被停止，未提交旧目标')
        if not self._motion_pose_synced:
            self.sync_motion_pose(generation=generation)
        safe_pose = self.logic.clamp_pose(pose)
        previous_pose = self.pose
        self._submit_pose(safe_pose, time_ms=time_ms, current=previous_pose, generation=generation)
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

    @_motion_serialized
    def jog(self, direction: str, amount: int | None, time_ms: int, *, generation=None) -> HeadPose:
        name = str(direction).strip().lower()
        if name not in ("left", "right", "up", "down"):
            raise ValueError("direction must be left, right, up, or down")
        if not self.config.simulated:
            # A manual arrow addresses one measured axis. Never enable or
            # move an unavailable second servo to test the working one.
            self.sync_motion_pose(only_axis="pitch" if name in ("up", "down") else "yaw", generation=generation)
        target = self.logic.jog(self.pose, direction, amount)
        safe_pose = self.logic.clamp_pose(target)
        self._submit_pose(safe_pose, time_ms=time_ms, current=self.pose, generation=generation)
        self.log("manual_jog", {"direction": direction, **asdict(safe_pose)})
        return safe_pose

    def _core_sound_status(self):
        return {**self._dominant_sound.snapshot(),
                'motion_authorized': self._core_motion_authorized,
                'motion_reason': self._core_motion_reason}

    def tick(self, *, include_frame: bool = True) -> dict[str, Any]:
        if not self._tick_lock.acquire(blocking=False):
            return self.snapshot(include_frame=include_frame)
        try:
            return self._tick_locked(include_frame=include_frame)
        except MotionRequestCancelled:
            # Cancellation is an expected control decision, not an IO fault.
            return self.snapshot(include_frame=include_frame)
        finally:
            self._tick_lock.release()

    def _tick_locked(self, *, include_frame: bool = True) -> dict[str, Any]:
        generation = self.motion_generation()
        now = time.time()
        if not self.running:
            self._image_velocity.reset()
            self._visual_control = dict(reason='stopped', automatic=False,
                                        pitch_enabled=self.vision_processing.visual_pitch_enabled)
        if getattr(self.head, "is_moving", False):
            self.motor_guard_until_s = max(self.motor_guard_until_s, now + self.control.motor_guard_ms / 1000 + .06)
        raw_audio = apply_audio_channel_mapping(
            self.audio.read_estimate(),
            swap_channels=self.audio_mapping.swap_channels,
        )
        self.last_raw_audio = raw_audio
        self.last_audio,self._audio_motion_context = qualify_audio_during_motion(
            raw_audio,active=now < self.motor_guard_until_s)
        guided=self.vision_processing.tracking_strategy in ('audio_visual_guided','visual_priority')
        if (not isinstance(self.audio, WasapiArrayAudio)
                and not self.audio_processing.vad_enabled and not self.last_audio.motor_suppressed
                and getattr(self.audio, "localization_ready", True)):
            # Legacy stereo-only override. The four-channel adapter already
            # owns VAD, direction quality and PCM timestamps, even with VAD off.
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
        dominant_mode = self.audio_capture.localization_mode == 'ear_pair' and self.audio_capture.ear_mapping_confirmed
        core_context = (generation, self.features.audio_enabled, self.features.visual_enabled,
            self.direction.yaw_left_sign, self.axis_limits.yaw_min, self.axis_limits.yaw_max, dominant_mode,
            id(self.audio), id(self.head), tuple(sorted(asdict(self.audio_capture).items())),
            self.audio_mapping.swap_channels, self.control.control_profile,
            self.vision_processing.tracking_strategy, self.vision_processing.active_speaker_enabled)
        if core_context != self._dominant_context:
            if self._core_motion_authorized and getattr(self.head, 'is_moving', False) and hasattr(self.head, 'cancel'):
                self.head.cancel()
                self.pose = getattr(self.head, 'estimated_pose', self.pose)
            self._dominant_sound.reset()
            self._core_motion_authorized = False
            self._core_motion_reason = 'awaiting_current_core_evidence'
            self._dominant_context = core_context
        if dominant_mode and self.features.audio_enabled:
            parameters = self._profile_params()
            self._dominant_sound.update(self.last_audio, now=time.perf_counter(),
                yaw=getattr(self.head, 'estimated_pose', self.pose).yaw,
                units_per_degree=float(parameters['audio_seek_max_step'])/float(parameters['audio_seek_full_scale_deg']),
                left_sign=self.direction.yaw_left_sign, yaw_min=self.axis_limits.yaw_min,
                yaw_max=self.axis_limits.yaw_max, command_window=int(parameters['audio_seek_window']))
        if hasattr(self.vision,'set_audio_guidance'):
            # Only a fresh current observation from the selected acoustic track
            # can guide a face. The stored position never masquerades as speech.
            current_core = not dominant_mode or self._dominant_sound.live_matches_core
            guidance = None
            if guided and self.features.audio_enabled and self.vision_processing.active_speaker_enabled:
                guidance = self.last_audio
                if not current_core and self.last_audio.direction != AudioDirection.UNKNOWN:
                    # Preserve the distinction between enabled-but-unconfirmed
                    # audio and explicitly disabled audio. No old angle is used.
                    guidance = replace(self.last_audio, direction=AudioDirection.UNKNOWN,
                        noise_state='awaiting_core_confirmation')
            self.vision.set_audio_guidance(guidance)
        control_target = None
        crowded_state = None
        view_confirming = False
        if self.features.camera_enabled:
            frame = self.vision.read()
            self.last_frame_b64 = frame.jpeg_base64
            self.last_stereo_frame_b64 = getattr(frame, "stereo_jpeg_base64", "")
            self.last_focus_previews = getattr(frame, "focus_previews", {})
            self.last_eye_frames = getattr(frame, "eye_frames", {})
            self.last_eye_targets = getattr(frame, "eye_targets", {})
            self.last_binocular = getattr(frame, "binocular", {})
            control_eye = getattr(frame, "control_eye", "")
            coordinate_frame=self.last_binocular.get("coordinate_frame", "selected_eye_pixels")
            view_confirming = self.features.visual_enabled and self.last_binocular.get("selection_reason") in ("confirming_view", "stale_frame", "no_usable_eye")
            if control_eye != self._last_control_eye:
                self._last_control_eye = control_eye
                self._smoothed_error = None
                self._pitch_error_direction = self._pitch_error_hits = 0
                self._last_control_detection_sequence = 0
                self._reset_speaker_lock()
                self._reset_silent_visual_candidate()
                self._reset_reframe()
            self.last_frame_width = frame.frame_width
            self.last_frame_height = frame.frame_height
            self.last_targets = list(getattr(frame, "targets", []) or ([] if frame.target is None else [frame.target]))
            self.last_body_target = getattr(frame, "body_target", None)
            use_crowded = (
                self.vision_processing.tracking_strategy == "crowded_visual_first"
                and self.vision_processing.active_speaker_enabled
            )
            if guided:
                self.last_target=frame.target
                control_target=frame.target
                selection=self.last_binocular.get('target_selection',{})
                self.audio_search_allowed=not self.features.visual_enabled or not bool(self.last_binocular.get('people',self.last_targets))
                if self.features.visual_enabled and self.vision_processing.tracking_strategy=='visual_priority' and selection.get('locked_person_id') is not None:
                    self.audio_search_allowed=False
                crowded_state=DemoState(DemoMode.TRACK_SPEAKER if control_target is not None else DemoMode.VISUAL_SCAN,
                    audio_direction=self.last_audio.direction.value,visual_locked=control_target is not None,
                    target_label=control_target.label if control_target is not None else '',
                    last_event=selection.get('switch_reason','audio_guided_lock' if selection.get('mode')=='audio_guided' else 'visual_fallback_lock'),
                    audio_ready=bool(selection.get('audio_usable',False)),target_confirmed=control_target is not None)
            elif use_crowded:
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
            actual_spatial=self.last_target is not None and self.last_target.stereo_position is not None
            actual_bearing=self.last_target is not None and self.last_target.bearing is not None
            if self.last_binocular.get("calibrated") or self.last_binocular.get("rectification_enabled"):
                coordinate_frame="rectified_camera_midpoint" if actual_spatial else self.last_target.bearing['frame'] if actual_bearing else "selected_rectified_eye_pixels"
                self.last_binocular.update(coordinate_frame=coordinate_frame,uses_two_eye_geometry=actual_spatial)
            if coordinate_frame!=self._last_coordinate_frame:
                self._last_coordinate_frame=coordinate_frame
                self._smoothed_error=None
                self._pitch_error_direction=self._pitch_error_hits=0
            if not getattr(frame, "control_ready", True):
                control_target = None
                crowded_state = None
                self._smoothed_error = None
                self._pitch_error_direction = self._pitch_error_hits = 0
        else:
            self.last_frame_b64 = ""
            self.last_target = None
            self.last_targets = []
            self.last_body_target = None
            self.last_eye_frames = {}
            self.last_eye_targets = {}
            self.last_binocular = {}
            self.last_vision_status = VisionStatus(False, self.camera.device_name or "off", "camera off", fps=0.0)
            self.audio_search_allowed = True
            self._reset_speaker_lock()
        audio_for_control = self.last_audio if self.features.audio_enabled else None
        if not self.features.visual_enabled:
            # Camera preview is not a permission gate on independent listening.
            self.audio_search_allowed = True
        target_for_control = control_target if self.features.visual_enabled else None
        self.last_state = (
            crowded_state
            if crowded_state is not None and self.features.visual_enabled
            else self.state_machine.update(audio=audio_for_control, target=target_for_control, now_s=now)
        )
        if dominant_mode:
            visual_owner = self.features.visual_enabled and (self.last_state.target_confirmed or not self.audio_search_allowed)
            if not self.running:
                self._core_motion_authorized = False
                self._core_motion_reason = 'stopped'
            elif visual_owner:
                if self._core_motion_authorized and getattr(self.head, 'is_moving', False) and hasattr(self.head, 'cancel'):
                    self.head.cancel()
                    self.pose = getattr(self.head, 'estimated_pose', self.pose)
                self._core_motion_authorized = False
                self._core_motion_reason = 'visual_target_has_priority'
            elif self._dominant_sound.fresh_core_observation:
                self._core_motion_authorized = True
                self._core_motion_reason = 'confirmed_core_motion'
            elif not self._core_motion_authorized:
                self._core_motion_reason = 'awaiting_current_core_evidence'
        acoustic_fallback = (dominant_mode and self.features.audio_enabled and self.audio_search_allowed
                             and not self.last_state.target_confirmed and self._core_motion_authorized)
        self._reframe_active = False
        if self.running and (view_confirming or not self.last_state.target_confirmed):
            self._visual_control = dict(reason='waiting_fresh_frame' if view_confirming else 'waiting_face',
                automatic=True, pitch_enabled=self.vision_processing.visual_pitch_enabled)
        if self.last_state.audio_ready:
            self._last_reframe_audio_s = now
        self._record_audio_association(now)
        next_pose = self.pose
        audio_seek_commanded = False
        dominant_commanded = False
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
        if self.running and view_confirming and not acoustic_fallback and getattr(self.head, "is_moving", False) and hasattr(self.head, "cancel"):
            self.head.cancel()
            self.pose = getattr(self.head, "estimated_pose", self.pose)
        if self.running and (not view_confirming or acoustic_fallback):
            visual_tracking = not view_confirming and self.features.visual_enabled and target_for_control is not None and self.last_state.target_confirmed
            detection_sequence = getattr(frame, "detection_sequence", 0) if self.features.camera_enabled else 0
            fresh_visual = not detection_sequence or detection_sequence != self._last_control_detection_sequence
            motor_settling = now < self.motor_guard_until_s and not visual_tracking
            if motor_settling:
                self._smoothed_error = None
                self._pitch_error_direction = 0
                self._pitch_error_hits = 0
            elif visual_tracking and fresh_visual:
                self._last_control_detection_sequence = detection_sequence
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
                if self.vision_processing.tracking_strategy=='visual_priority':
                    fixation=target_for_control.fixation
                    basis=(fixation['mode'],fixation['anchor_kind'],target_for_control.source_eye if fixation['mode']=='single_eye' else 'both') if fixation else ('legacy',)
                    identity=(target_for_control.face_id,self._last_coordinate_frame,basis)
                    if identity!=self._last_visual_identity:
                        self._smoothed_error=None
                        self._pitch_error_direction=self._pitch_error_hits=0
                        for trigger in self._visual_triggers.values():
                            trigger.reset()
                    self._last_visual_identity=identity
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
                if self.vision_processing.visual_pitch_enabled and self.vision_processing.tracking_strategy != 'visual_priority':
                    control_error_y = self._stable_pitch_error(
                        self._smoothed_error[1],
                        deadband=float(visual_limits["pitch_deadband"]),
                    )
                elif not self.vision_processing.visual_pitch_enabled:
                    self._pitch_error_direction = 0
                    self._pitch_error_hits = 0
                    control_error_y = 0.0
                else:
                    control_error_y = self._smoothed_error[1]
                if self.vision_processing.tracking_strategy=='visual_priority':
                    for axis, value in (('yaw', control_error_x), ('pitch', control_error_y)):
                        command, active_band = self._visual_triggers[axis].update(value,
                            band=visual_limits[axis+'_deadband'], sequence=detection_sequence or now, now=now)
                        visual_limits[axis+'_deadband'] = active_band
                        if axis == 'yaw':
                            control_error_x = command
                        else:
                            control_error_y = command
                    velocity=self._image_velocity.update((error_x,error_y),
                        identity=(identity,control_eye),sequence=detection_sequence or now,
                        observed_at=getattr(frame,'captured_monotonic',0.) or now)
                    next_pose,self._visual_approach=adaptive_visual_step(self.logic,
                        getattr(self.head,'estimated_pose',self.pose),self.pose,
                        (control_error_x,control_error_y),(error_x,error_y),velocity,visual_limits)
                    move_time_ms=self._visual_approach['move_time_ms']
                    self._visual_control = dict(reason='image_feedback', automatic=True,
                        raw_error=[error_x,error_y], filtered_error=list(self._smoothed_error),
                        command_error=[control_error_x,control_error_y], next_pose=asdict(next_pose),
                        yaw=self._visual_triggers['yaw'].snapshot(), pitch=self._visual_triggers['pitch'].snapshot(),
                        pitch_enabled=self.vision_processing.visual_pitch_enabled,
                        pitch_limit='upper' if next_pose.pitch == self.axis_limits.pitch_max and -self.direction.pitch_up_sign*control_error_y > 0 else
                                    'lower' if next_pose.pitch == self.axis_limits.pitch_min and -self.direction.pitch_up_sign*control_error_y < 0 else None,
                        detection_sequence=detection_sequence, deadbands={a:visual_limits[a+'_deadband'] for a in ('yaw','pitch')})
                    key = str((self._visual_control['yaw']['reason'], self._visual_control['pitch']['reason'], self._visual_control['pitch_limit']))
                    if key != self._last_visual_log[0] or now - self._last_visual_log[1] >= 1:
                        self.log('visual_control_decision', self._visual_control)
                        self._last_visual_log = (key, now)
                else:
                    next_pose = self.logic.apply_visual_error(
                        getattr(self.head, "estimated_pose", self.pose),control_error_x,control_error_y,
                        yaw_deadband=float(visual_limits["yaw_deadband"]),pitch_deadband=float(visual_limits["pitch_deadband"]),
                        max_yaw_delta=int(visual_limits["max_yaw_delta"]),max_pitch_delta=int(visual_limits["max_pitch_delta"]),
                        min_yaw_delta=int(visual_limits["min_yaw_delta"]),min_pitch_delta=int(visual_limits["min_pitch_delta"]))
                self._reset_audio_seek()
                self._reset_reframe()
            elif not self.last_state.target_confirmed:
                self._image_velocity.reset()
                for trigger in self._visual_triggers.values():
                    trigger.reset()
                self._smoothed_error = None
                self._last_visual_identity=None
                self._visual_approach={}
                self._pitch_error_direction = 0
                self._pitch_error_hits = 0
            no_visible_person = not self.features.visual_enabled or not self.last_targets
            params = self._profile_params()
            visible_acquire_target = (
                self.last_target
                if not view_confirming and self.features.visual_enabled and self.last_target is not None
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
            visual_reframe_available = not view_confirming and self.features.visual_enabled and self.features.camera_enabled and (
                no_visible_person or visible_acquire_target is not None
            )
            audio_seek_ready = not dominant_mode and self.last_state.audio_ready and not motor_settling and visible_acquire_target is None and (
                self.last_state.mode == DemoMode.SEEK_VISUAL or (no_visible_person and self.last_state.audio_ready)
                or self.last_state.mode == DemoMode.AUDIO_SEARCH
            )
            audio_seek_step_limit = int(params["audio_seek_max_steps"])
            max_audio_seek_steps = (
                audio_seek_step_limit
                if self.last_state.mode == DemoMode.AUDIO_SEARCH
                else (1 if visual_reframe_available else audio_seek_step_limit)
            )
            if (dominant_mode and self.features.audio_enabled and self.audio_search_allowed
                    and not self.last_state.target_confirmed and visible_acquire_target is None):
                core = self._dominant_sound.snapshot()
                if not core['locked']:
                    self._audio_seek_stop_reason = 'collecting_core_direction'
                elif not self._core_motion_authorized:
                    self._audio_seek_stop_reason = 'awaiting_current_core_evidence'
                elif motor_settling:
                    self._audio_seek_stop_reason = 'finishing_core_motion'
                else:
                    next_pose = self.logic.clamp_pose(HeadPose(core['goal_yaw'], self.pose.pitch))
                    audio_seek_commanded = dominant_commanded = next_pose != self.pose
                    self._audio_seek_stop_reason = ('core_goal_submitted' if dominant_commanded else
                        'yaw_limit' if core['goal_yaw'] in (self.axis_limits.yaw_min, self.axis_limits.yaw_max) else
                        'core_goal_reached' if abs(getattr(self.head, 'estimated_pose', self.pose).yaw-core['goal_yaw'])<=4 else 'finishing_core_motion')
                    if dominant_commanded:
                        self._reset_reframe()
                        move_time_ms = int(params['audio_seek_move_time_ms'])
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
                ):
                    self._audio_seek_origin_yaw = self.pose.yaw
                    self._audio_seek_started_s = now
                    self._audio_seek_steps = 0
                    self._last_audio_seek_abs_azimuth = None
                    self._last_audio_seek_direction = ""
                    self._last_audio_seek_observation = None
                    self._reset_reframe()
                observation=(self.last_audio.sample_sequence,self.last_audio.observed_monotonic)
                fresh_observation=(observation[1]>0 and observation!=self._last_audio_seek_observation)
                self._audio_seek_stop_reason = ""
                should_move = self._audio_seek_steps < max_audio_seek_steps
                if not should_move:
                    self._audio_seek_stop_reason = "step_budget_reached"
                if should_move and self._last_audio_seek_abs_azimuth is not None and self._audio_seek_steps > 0:
                    same_direction = audio_seek_direction == self._last_audio_seek_direction
                    diverged = same_direction and audio_seek_abs_azimuth > self._last_audio_seek_abs_azimuth + 6.0
                    same_direction_repeat = (
                        same_direction
                        and abs(audio_seek_abs_azimuth - self._last_audio_seek_abs_azimuth) < 6.0
                        and self._audio_seek_steps >= 1
                        and not fresh_observation
                    )
                    crossed_center = (
                        not same_direction
                        and self._last_audio_seek_direction in ("left", "right")
                        and audio_seek_direction in ("left", "right")
                        and audio_seek_abs_azimuth <= float(params["audio_deadband_deg"]) * 1.5
                    )
                    should_move = not diverged and not crossed_center and not same_direction_repeat
                    if not should_move:
                        self._audio_seek_stop_reason = "direction_diverged" if diverged else "crossed_center" if crossed_center else "awaiting_new_acoustic_observation"
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
                    if not audio_seek_commanded:
                        self._audio_seek_stop_reason = "yaw_limit" if next_pose.yaw in (self.axis_limits.yaw_min,self.axis_limits.yaw_max) else "sound_centered_or_window_limit"
                    if audio_seek_commanded:
                        self._reset_reframe()
                        move_time_ms = int(params["audio_seek_move_time_ms"])
                if audio_seek_attempted and not audio_seek_commanded and visual_reframe_available:
                    self._audio_seek_steps = max(self._audio_seek_steps, 1)
            elif self.last_state.mode == DemoMode.LISTENING and not dominant_mode:
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
            motion_interval = 0.05 if visual_tracking and self.control.smooth_motion else self.motion_interval_s
            if next_pose != self.pose and now - self.last_motion_s >= motion_interval:
                self.move(next_pose, time_ms=move_time_ms, source="auto_move", generation=generation)
                if audio_seek_commanded:
                    self._audio_seek_steps += 1
                    if not dominant_commanded:
                        self._last_audio_seek_abs_azimuth = audio_seek_abs_azimuth
                        self._last_audio_seek_direction = audio_seek_direction
                        self._last_audio_seek_observation = observation
                if reframe_commanded:
                    self._mark_reframe_move(now)
        if dominant_mode:
            core = self._core_sound_status()
            identity = (core['core_id'], core['reason'], self._audio_seek_stop_reason, self.features.visual_enabled)
            previous, recorded = self._last_core_sound_log
            if identity != previous or now - recorded >= 2.:
                self.log('core_sound_decision', {'core': core, 'steering_reason': self._audio_seek_stop_reason,
                    'running': self.running, 'features': asdict(self.features), 'pose': asdict(self.pose)})
                self._last_core_sound_log = (identity, now)
        return self.snapshot(include_frame=include_frame)


def create_app(*, simulated: bool = False, settings_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="Locator Robot Demo")
    if settings_path is None and not simulated:
        settings_path = default_settings_path()
    runtime = DemoRuntime(RuntimeConfig(simulated=simulated, settings_path=settings_path))
    app.state.runtime = runtime

    @app.middleware('http')
    async def diagnostic_request(request, call_next):
        identity = uuid.uuid4().hex[:12]
        token = request_id.set(identity)
        try:
            response = await call_next(request)
            if response.status_code >= 400:
                runtime.log('http_request_error', {'method': request.method, 'path': request.url.path, 'status': response.status_code})
            response.headers['X-Request-ID'] = identity
            return response
        except Exception as exc:
            runtime.log('http_unhandled_error', {'method': request.method, 'path': request.url.path, 'error': str(exc)})
            return JSONResponse({'detail': str(exc), 'request_id': identity}, status_code=500, headers={'X-Request-ID': identity})
        finally:
            request_id.reset(token)

    @app.exception_handler(StarletteHTTPException)
    async def diagnostic_http_error(request, exc):
        runtime.log('api_error', {'path': request.url.path, 'status': exc.status_code, 'error': exc.detail})
        return JSONResponse({'detail': exc.detail, 'request_id': request_id.get()}, status_code=exc.status_code)

    @app.get('/api/diagnostics')
    async def diagnostics():
        return {'journal': runtime.journal.snapshot(), 'motion_fault': runtime._motion_fault,
                'feedback': runtime._feedback, 'serial': await asyncio.to_thread(runtime.bus.diagnostics) if hasattr(runtime.bus, 'diagnostics') else {}}

    @app.post('/api/diagnostics/client-error')
    async def diagnostic_client_error(payload: dict):
        runtime.log('browser_error', {'message': str(payload.get('message',''))[:1000], 'page': str(payload.get('page',''))[:100]})
        return {'saved': True}

    @app.get('/api/diagnostics/log/{kind}')
    async def diagnostic_log(kind: str):
        path = runtime.journal.path(kind)
        if path is None or not path.is_file():
            raise HTTPException(404, '日志文件尚未生成')
        return FileResponse(path, filename=path.name, media_type='application/x-ndjson')

    @app.post('/api/servo/check')
    async def check_servo_communication():
        generation = runtime.halt_motion()
        try:
            await asyncio.to_thread(runtime.sync_motion_pose, generation=generation)
            return {'ok': True, 'running': False, 'feedback': runtime._feedback, 'motion': 'none; position queries only'}
        except Exception as exc:
            runtime.log('servo_check_error', {'error': str(exc)})
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/servo/discover')
    async def discover_servo():
        generation=runtime.halt_motion()
        return await asyncio.to_thread(runtime.discover_servo_port,generation=generation)

    async def stereo_preflight(generation):
        runtime._check_motion_generation(generation)
        checker=getattr(runtime.vision,'startup_health_check',None)
        if not runtime.features.camera_enabled or not callable(checker):
            result={'status':'not_applicable','message':'当前摄像头未启用或当前后端不支持该检查'}
        else:result=await asyncio.to_thread(checker)
        runtime._check_motion_generation(generation)
        runtime._stereo_preflight={**result,'checked_at':time.time()}
        runtime.log('stereo_startup_check',runtime._stereo_preflight)
        return dict(runtime._stereo_preflight)

    @app.post('/api/stereo/check')
    async def check_stereo():
        generation=runtime.halt_motion()
        return await stereo_preflight(generation)
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.on_event("startup")
    async def startup_event() -> None:
        runtime.start_control_loop()

    @app.on_event("shutdown")
    async def shutdown_event() -> None:
        runtime.shutdown()

    @app.get("/", response_class=HTMLResponse)
    async def root():
        return (static_dir / "index.html").read_text(encoding="utf-8")

    @app.get("/api/session")
    async def session():
        return runtime.session()

    @app.get("/api/state")
    async def state():
        return runtime.snapshot()

    def calibration_service():
        service=getattr(runtime.vision,"calibration",None)
        if service is None:
            raise HTTPException(status_code=400,detail="请使用真实双目采集后端")
        return service

    @app.get("/api/stereo-calibration/state")
    async def stereo_calibration_state():
        return await asyncio.to_thread(calibration_service().snapshot,True)

    @app.get("/api/stereo-calibration/board.pdf")
    async def stereo_board_pdf():
        path=Path(__file__).resolve().parents[3]/"output/calibration/stereo-charuco-A4-20mm.pdf"
        if not path.exists():
            path=Path(__file__).resolve().parents[4]/"output/calibration/stereo-charuco-A4-20mm.pdf"
        if not path.exists():
            raise HTTPException(status_code=404,detail="打印文件尚未生成")
        return FileResponse(path,media_type="application/pdf",filename=path.name)

    @app.post("/api/stereo-calibration/start")
    async def stereo_calibration_start(payload:StereoCalibrationRequest):
        service=calibration_service()
        with runtime._tick_lock:
            runtime.running=False
            if hasattr(runtime.head,"cancel"): runtime.head.cancel()
        try:
            return await asyncio.to_thread(service.start,square_mm=payload.square_mm,scale_confirmed=payload.scale_confirmed)
        except ValueError as exc:
            raise HTTPException(status_code=400,detail=str(exc)) from exc

    @app.post("/api/stereo-calibration/resume")
    async def stereo_calibration_resume():
        service=calibration_service()
        with runtime._tick_lock:
            runtime.running=False
            if hasattr(runtime.head,"cancel"): runtime.head.cancel()
        try:
            return await asyncio.to_thread(service.resume)
        except ValueError as exc:
            raise HTTPException(status_code=400,detail=str(exc)) from exc

    @app.post("/api/stereo-calibration/stop")
    async def stereo_calibration_stop():
        service=calibration_service()
        await asyncio.to_thread(service.stop)
        return service.snapshot()

    @app.post("/api/stereo-calibration/solve")
    async def stereo_calibration_solve():
        service=calibration_service()
        try:
            result=await asyncio.to_thread(service.solve)
            if result["status"]=="accepted": runtime.vision.request_calibration_reload()
            return result
        except Exception as exc:
            raise HTTPException(status_code=400,detail=str(exc)) from exc

    @app.get("/api/devices")
    async def devices():
        try:
            wasapi = list_wasapi_inputs()
        except Exception as exc:
            wasapi = [{"error": str(exc)}]
        return {"dshow": [asdict(device) for device in list_dshow_devices()], "wasapi": wasapi,
                "serial": [asdict(p) for p in list_serial_ports()],
                "bindings": {'camera_usb':f'{runtime.camera.usb_vid}:{runtime.camera.usb_pid}',
                    'microphone_usb':f'{runtime.audio_capture.usb_vid}:{runtime.audio_capture.usb_pid}',
                    'servo_port':runtime.config.port,'discovery':dict(runtime._servo_discovery)}}

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
            generation = runtime.halt_motion()
            result = await asyncio.to_thread(
                runtime.direct_servo_move,
                servo_id=payload.servo_id,
                position=payload.position,
                time_ms=payload.time_ms,
                arm=payload.arm,
                generation=generation,
            )
            return {**result, "error": None}
        except Exception as exc:
            runtime.log("servo_direct_move_error", {"error": str(exc), "servo_id": payload.servo_id})
            return {"servo_id": payload.servo_id, "position": payload.position, "time_ms": payload.time_ms, "error": str(exc)}

    @app.post("/api/head/move")
    async def move(payload: MoveRequest):
        try:
            generation = runtime.halt_motion()
            pose = await asyncio.to_thread(runtime.move, HeadPose(payload.yaw, payload.pitch), payload.time_ms, generation=generation)
            return {"target": asdict(pose), "running": runtime.running, "error": None}
        except Exception as exc:
            runtime.log("manual_move_error", {"error": str(exc), "port": runtime.config.port})
            return {"target": asdict(runtime.pose), "running": runtime.running, "error": str(exc)}

    @app.post("/api/head/jog")
    async def jog(payload: JogRequest):
        try:
            generation = runtime.halt_motion()
            pose = await asyncio.to_thread(runtime.jog, payload.direction, payload.amount, payload.time_ms, generation=generation)
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

    @app.get("/api/config/audio-capture")
    async def get_audio_capture_config():
        return asdict(runtime.audio_capture)

    @app.post("/api/config/audio-capture")
    async def config_audio_capture(payload: AudioCaptureConfigRequest):
        return runtime.update_audio_capture_config(payload)

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
        return {"ports": [{**asdict(port), "reserved_for_audio": is_microphone_port(port)} for port in list_serial_ports()], "selected": runtime.config.port}

    @app.post("/api/demo/start")
    async def start_demo():
        runtime.halt_motion()
        generation = runtime.begin_start()
        service=getattr(runtime.vision,"calibration",None)
        if service is not None and service.snapshot()["status"] in ("collecting","solving"):
            raise HTTPException(409,"标定采集中暂不启用自动转向，请先暂停采集")
        if runtime.features.audio_enabled or runtime.features.visual_enabled:
            try:
                if runtime.features.camera_enabled:
                    health=await stereo_preflight(generation)
                    if runtime.features.visual_enabled and health['status']=='misaligned':raise RuntimeError(health['message'])
                await asyncio.to_thread(runtime.sync_motion_pose, generation=generation)
            except RuntimeError as exc:
                raise HTTPException(409, str(exc)) from exc
        try:
            runtime.finish_start(generation)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"running": runtime.running,"stereo_preflight":dict(runtime._stereo_preflight)}

    @app.post("/api/demo/stop")
    async def stop_demo():
        runtime.halt_motion()
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
                method = runtime.snapshot if runtime.control_loop_active else runtime.tick
                snapshot = await asyncio.to_thread(method, include_frame=include_frame)
                await websocket.send_json(snapshot)
                if include_frame:
                    # next_frame_s = now_s + 0.5
                    next_frame_s = now_s + 0.1
                await asyncio.sleep(0.1)
        except WebSocketDisconnect:
            return

    app.state.runtime = runtime
    return app
