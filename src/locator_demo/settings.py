from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .head import DirectionConfig, DEFAULT_YAW_MIN, DEFAULT_YAW_CENTER, DEFAULT_YAW_MAX
from .ear_localization import EarPairConfig


def validate_servo_ids(yaw_id, pitch_id):
    for name, value in (("yaw_id", yaw_id), ("pitch_id", pitch_id)):
        if name == "yaw_id" and value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 253:
            raise ValueError(f'{name} 必须是 0–253 的整数')
    if yaw_id is not None and yaw_id == pitch_id:
        raise ValueError('左右轴与俯仰轴不能使用相同的舵机 ID')


def validate_axis_limits(values):
    for axis in ('yaw', 'pitch'):
        lo, center, hi = (values[axis + '_' + key] for key in ('min', 'center', 'max'))
        if any(isinstance(v, bool) or not isinstance(v, int) or not 500 <= v <= 2500
               for v in (lo, center, hi)):
            raise ValueError(f'{axis} 位置必须在 500–2500 范围内；不自动截断')
        if not lo < center < hi:
            raise ValueError(f'{axis} 必须满足 下限 < 中位 < 上限；不自动交换或改写')


def _axis_value(value: int, default: int) -> int:
    try:
        return max(500, min(2500, int(value)))
    except Exception:
        return default


def _norm_offset(value: float, default: float) -> float:
    try:
        return max(-0.25, min(0.25, float(value)))
    except Exception:
        return default


def _threshold_value(value: float, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return default


def _seconds_value(value: float, default: float, *, minimum: float = 0.1, maximum: float = 5.0) -> float:
    try:
        return max(minimum, min(maximum, float(value)))
    except Exception:
        return default


def _optional_float(value: float | None, *, minimum: float, maximum: float) -> float | None:
    if value is None:
        return None
    try:
        return max(minimum, min(maximum, float(value)))
    except Exception:
        return None


def _optional_int(value: int | None, *, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    try:
        return max(minimum, min(maximum, int(value)))
    except Exception:
        return None


def _optional_fps(value: int | None) -> int | None:
    if value is None:
        return None
    try:
        return max(1, min(240, int(value)))
    except Exception:
        return None


def _output_width(value: int | None, default: int = 640) -> int:
    if value is None:
        return default
    try:
        return max(320, min(1920, int(value)))
    except Exception:
        return default


def _face_detector_backend(value: str | None, default: str = "mediapipe") -> str:
    backend = str(value or default).strip().lower()
    return backend if backend in ("mediapipe", "scrfd") else default


def _scrfd_input_size(value: int | None, default: int = 640) -> int:
    if value is None:
        return default
    try:
        size = max(320, min(1280, int(value)))
    except Exception:
        return default
    return max(320, (size // 32) * 32)


@dataclass(frozen=True)
class FeatureConfig:
    audio_enabled: bool = True
    visual_enabled: bool = True
    camera_enabled: bool = True


@dataclass(frozen=True)
class CameraConfig:
    device_name: str | None = None
    video_size: str | None = None
    crop_left_half: bool | None = None
    fps: int | None = None
    output_width: int = 1920
    face_detector_backend: str = "mediapipe"
    scrfd_model_path: str | None = None
    scrfd_threshold: float = 0.35
    scrfd_input_size: int = 640
    capture_backend: str = "stereo_mjpeg"
    input_fps: int = 60
    usb_vid: str = "1BCF"
    usb_pid: str = "0B15"
    tracking_eye: str = "auto"
    left_rotation: int = 0
    right_rotation: int = 0
    multi_scale_search: bool = True
    focus_assist: bool = True
    preview_jpeg_quality: int = 95
    stereo_calibration_path: str = "calibration/stereo_camera.json"
    stereo_geometry_enabled: bool = True

    def __post_init__(self) -> None:
        if not 70 <= int(self.preview_jpeg_quality) <= 100:
            raise ValueError("Preview JPEG quality must be 70..100")
        if self.tracking_eye not in ("auto", "left", "right") or self.left_rotation not in (0, 180) or self.right_rotation not in (0, 180):
            raise ValueError("Invalid per-eye orientation or tracking eye")
        object.__setattr__(self, "capture_backend", self.capture_backend if self.capture_backend in ("stereo_mjpeg", "legacy") else "stereo_mjpeg")
        object.__setattr__(self, "input_fps", max(1, min(240, int(self.input_fps))))
        object.__setattr__(self, "fps", _optional_fps(self.fps))
        object.__setattr__(self, "output_width", _output_width(self.output_width, 640))
        object.__setattr__(self, "face_detector_backend", _face_detector_backend(self.face_detector_backend))
        model_path = str(self.scrfd_model_path or "").strip() or None
        object.__setattr__(self, "scrfd_model_path", model_path)
        object.__setattr__(self, "scrfd_threshold", _threshold_value(self.scrfd_threshold, 0.35))
        object.__setattr__(self, "scrfd_input_size", _scrfd_input_size(self.scrfd_input_size, 640))


@dataclass(frozen=True)
class AudioMappingConfig:
    swap_channels: bool = False


@dataclass(frozen=True)
class AudioCaptureConfig:
    backend: str = "wasapi"
    device_name: str = "YDPI4MIC"
    channels: int = 4
    sample_rate: int = 48000
    sample_bits: int = 16
    block_ms: int = 20
    usb_vid: str = "EF09"
    usb_pid: str = "459D"
    wasapi_raw: bool = True
    localization_mode: str = "disabled"
    ear_left_channel: int | None = None
    ear_right_channel: int | None = None
    ear_mapping_confirmed: bool = False
    ear_spacing_m: float | None = None
    ear_delay_offset_us: float = 0.0

    def __post_init__(self):
        if self.backend not in ("wasapi", "legacy"):
            raise ValueError("Unknown audio capture backend")
        if self.backend == "wasapi" and (self.channels, self.sample_rate, self.sample_bits) != (4, 48000, 16):
            raise ValueError("Selected four-mic profile requires 4 channels / 48000 Hz / 16 bit")
        is_array = ('YDPI4MIC' in self.device_name.upper() or
                    (self.usb_vid.upper(), self.usb_pid.upper()) == ('EF09', '459D'))
        if is_array and (self.backend != 'wasapi' or not self.wasapi_raw):
            raise ValueError('YDPI4MIC requires four-channel WASAPI RAW; stereo downmix/DSP is not localization input')
        object.__setattr__(self, "block_ms", max(5, min(100, int(self.block_ms))))
        self.ear_pair_config()

    def ear_pair_config(self):
        return EarPairConfig(self.localization_mode, self.ear_left_channel, self.ear_right_channel,
                             self.ear_mapping_confirmed, self.ear_spacing_m, self.ear_delay_offset_us)


@dataclass(frozen=True)
class ControlConfig:
    control_profile: str = "stable"
    motor_guard_ms: int = 250
    smooth_motion: bool = True
    motion_hz: int = 50
    max_velocity: float = 180.0
    max_acceleration: float = 600.0
    max_jerk: float = 3000.0

    def __post_init__(self) -> None:
        profile = str(self.control_profile or "stable").strip().lower()
        if profile not in ("stable", "fast"):
            profile = "stable"
        object.__setattr__(self, "control_profile", profile)
        object.__setattr__(self, "motor_guard_ms", max(0, min(1500, int(self.motor_guard_ms))))
        object.__setattr__(self, "motion_hz", max(10, min(100, int(self.motion_hz))))
        object.__setattr__(self, "max_velocity", max(20.0, min(600.0, float(self.max_velocity))))
        object.__setattr__(self, "max_acceleration", max(50.0, min(3000.0, float(self.max_acceleration))))
        object.__setattr__(self, "max_jerk", max(200.0, min(20000.0, float(self.max_jerk))))


@dataclass(frozen=True)
class AxisLimitsConfig:
    yaw_min: int = DEFAULT_YAW_MIN
    yaw_center: int = DEFAULT_YAW_CENTER
    yaw_max: int = DEFAULT_YAW_MAX
    pitch_min: int = 1200
    pitch_center: int = 1500
    pitch_max: int = 1800

    def __post_init__(self) -> None:
        yaw_min = _axis_value(self.yaw_min, DEFAULT_YAW_MIN)
        yaw_max = _axis_value(self.yaw_max, DEFAULT_YAW_MAX)
        yaw_center = _axis_value(self.yaw_center, DEFAULT_YAW_CENTER)
        pitch_min = _axis_value(self.pitch_min, 1200)
        pitch_max = _axis_value(self.pitch_max, 1800)
        pitch_center = _axis_value(self.pitch_center, 1500)
        if yaw_min > yaw_max:
            yaw_min, yaw_max = yaw_max, yaw_min
        if pitch_min > pitch_max:
            pitch_min, pitch_max = pitch_max, pitch_min
        object.__setattr__(self, "yaw_min", yaw_min)
        object.__setattr__(self, "yaw_max", yaw_max)
        object.__setattr__(self, "yaw_center", max(yaw_min, min(yaw_max, yaw_center)))
        object.__setattr__(self, "pitch_min", pitch_min)
        object.__setattr__(self, "pitch_max", pitch_max)
        object.__setattr__(self, "pitch_center", max(pitch_min, min(pitch_max, pitch_center)))


@dataclass(frozen=True)
class AudioProcessingConfig:
    vad_enabled: bool = True
    audio_confidence_threshold: float = 0.20
    speech_confidence_threshold: float = 0.15
    doa_confidence_threshold: float = 0.05
    required_audio_hits: int = 1
    denoise_enabled: bool = False
    denoise_dry_mix: float = 0.15
    denoise_output_dir: str | None = None
    recording_enabled: bool = True
    use_denoised_for_localization: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "audio_confidence_threshold",
            _threshold_value(self.audio_confidence_threshold, 0.20),
        )
        object.__setattr__(
            self,
            "speech_confidence_threshold",
            _threshold_value(self.speech_confidence_threshold, 0.15),
        )
        object.__setattr__(
            self,
            "doa_confidence_threshold",
            _threshold_value(self.doa_confidence_threshold, 0.05),
        )
        object.__setattr__(
            self,
            "required_audio_hits",
            max(1, min(5, int(self.required_audio_hits))),
        )
        object.__setattr__(
            self,
            "denoise_enabled",
            bool(self.denoise_enabled),
        )
        object.__setattr__(
            self,
            "use_denoised_for_localization",
            bool(self.use_denoised_for_localization),
        )
        object.__setattr__(
            self,
            "denoise_dry_mix",
            _threshold_value(self.denoise_dry_mix, 0.15),
        )
        denoise_output_dir = str(self.denoise_output_dir).strip() if self.denoise_output_dir else None
        object.__setattr__(
            self,
            "denoise_output_dir",
            denoise_output_dir,
        )


@dataclass(frozen=True)
class VisionProcessingConfig:
    active_speaker_enabled: bool = True
    tracking_strategy: str = "classic_audio_first"
    audio_sector_half_width_deg: float = 15.0
    asd_backend: str = "rules"
    speaker_lock_policy: str = "turn_hold"
    target_offset_x_norm: float = 0.04
    target_offset_y_norm: float = 0.0
    visual_mirror_x: bool = False
    visual_speaker_threshold: float = 0.30
    mouth_evidence_threshold: float = 0.06
    visual_yaw_mode: str = "small"
    visual_pitch_enabled: bool = True
    visual_yaw_deadband: float | None = None
    visual_pitch_deadband: float | None = None
    visual_yaw_min_delta: int = 0
    visual_yaw_max_delta: int | None = None
    visual_pitch_min_delta: int = 0
    visual_pitch_max_delta: int | None = None
    min_face_height_ratio: float = 0.12
    keep_face_height_ratio: float = 0.09
    talknet_threshold: float = 0.55
    speaker_lock_hold_s: float = 1.2
    speaker_lost_timeout_s: float = 0.8
    audio_interrupt_enabled: bool = False
    audio_search_after_silent_visual: bool = False
    silent_visual_hold_s: float = 1.2

    def __post_init__(self) -> None:
        strategy = str(self.tracking_strategy or "classic_audio_first").strip().lower()
        if strategy not in ("classic_audio_first", "crowded_visual_first", "audio_visual_guided", "visual_priority"):
            strategy = "classic_audio_first"
        object.__setattr__(self, "tracking_strategy", strategy)
        object.__setattr__(self,'audio_sector_half_width_deg',max(5.,min(30.,float(self.audio_sector_half_width_deg))))
        backend = str(self.asd_backend or "rules").strip().lower()
        if backend not in ("rules", "talknet"):
            backend = "rules"
        object.__setattr__(self, "asd_backend", backend)
        lock_policy = str(self.speaker_lock_policy or "turn_hold").strip().lower()
        if lock_policy not in ("turn_hold", "until_lost", "interruptible"):
            lock_policy = "turn_hold"
        object.__setattr__(self, "speaker_lock_policy", lock_policy)
        object.__setattr__(self, "target_offset_x_norm", _norm_offset(self.target_offset_x_norm, 0.04))
        object.__setattr__(self, "target_offset_y_norm", _norm_offset(self.target_offset_y_norm, 0.0))
        object.__setattr__(self, "visual_mirror_x", bool(self.visual_mirror_x))
        yaw_mode = str(self.visual_yaw_mode or "small").strip().lower()
        if yaw_mode not in ("off", "small", "full"):
            yaw_mode = "small"
        object.__setattr__(self, "visual_yaw_mode", yaw_mode)
        object.__setattr__(self, "visual_pitch_enabled", bool(self.visual_pitch_enabled))
        object.__setattr__(
            self,
            "visual_yaw_deadband",
            _optional_float(self.visual_yaw_deadband, minimum=0.0, maximum=0.5),
        )
        object.__setattr__(
            self,
            "visual_pitch_deadband",
            _optional_float(self.visual_pitch_deadband, minimum=0.0, maximum=0.5),
        )
        object.__setattr__(
            self,
            "visual_yaw_min_delta",
            0 if self.visual_yaw_min_delta is None else max(0, min(200, int(self.visual_yaw_min_delta))),
        )
        object.__setattr__(
            self,
            "visual_yaw_max_delta",
            _optional_int(self.visual_yaw_max_delta, minimum=1, maximum=300),
        )
        object.__setattr__(
            self,
            "visual_pitch_min_delta",
            0 if self.visual_pitch_min_delta is None else max(0, min(200, int(self.visual_pitch_min_delta))),
        )
        object.__setattr__(
            self,
            "visual_pitch_max_delta",
            _optional_int(self.visual_pitch_max_delta, minimum=1, maximum=300),
        )
        object.__setattr__(
            self,
            "visual_speaker_threshold",
            _threshold_value(self.visual_speaker_threshold, 0.30),
        )
        object.__setattr__(
            self,
            "mouth_evidence_threshold",
            _threshold_value(self.mouth_evidence_threshold, 0.06),
        )
        min_ratio = _threshold_value(self.min_face_height_ratio, 0.12)
        keep_ratio = _threshold_value(self.keep_face_height_ratio, 0.09)
        if keep_ratio > min_ratio:
            keep_ratio = min_ratio
        object.__setattr__(self, "min_face_height_ratio", min_ratio)
        object.__setattr__(self, "keep_face_height_ratio", keep_ratio)
        object.__setattr__(self, "talknet_threshold", _threshold_value(self.talknet_threshold, 0.55))
        object.__setattr__(self, "speaker_lock_hold_s", _seconds_value(self.speaker_lock_hold_s, 1.2))
        object.__setattr__(self, "speaker_lost_timeout_s", _seconds_value(self.speaker_lost_timeout_s, 0.8))
        object.__setattr__(self, "audio_interrupt_enabled", bool(self.audio_interrupt_enabled))
        object.__setattr__(
            self,
            "audio_search_after_silent_visual",
            bool(self.audio_search_after_silent_visual),
        )
        object.__setattr__(self, "silent_visual_hold_s", _seconds_value(self.silent_visual_hold_s, 1.2))


@dataclass(frozen=True)
class SavedSettings:
    port: str | None = None
    yaw_id: int | None = None
    pitch_id: int = 2
    direction: DirectionConfig = field(default_factory=DirectionConfig)
    features: FeatureConfig = field(default_factory=FeatureConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    audio_mapping: AudioMappingConfig = field(default_factory=AudioMappingConfig)
    audio_capture: AudioCaptureConfig = field(default_factory=AudioCaptureConfig)
    control: ControlConfig = field(default_factory=ControlConfig)
    axis_limits: AxisLimitsConfig = field(default_factory=AxisLimitsConfig)
    audio: AudioProcessingConfig = field(default_factory=AudioProcessingConfig)
    vision: VisionProcessingConfig = field(default_factory=VisionProcessingConfig)


def default_settings_path() -> Path:
    return Path(__file__).resolve().parents[2] / "locator_demo_settings.json"


def load_settings(path: Path | str | None) -> SavedSettings:
    if path is None:
        return SavedSettings()
    settings_path = Path(path)
    if not settings_path.exists():
        return SavedSettings()
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f'无法读取配置 {settings_path}；请检查或恢复备份，不会自动退回旧中位：{exc}') from exc
    if not isinstance(data, dict):
        raise ValueError(f'配置 {settings_path} 必须为 JSON 对象，不会使用默认中位替代')
    for section in ('direction', 'features', 'camera', 'audio_mapping', 'audio_capture',
                    'control', 'axis_limits', 'audio', 'vision'):
        if section in data and not isinstance(data[section], dict):
            raise ValueError(f'配置 {settings_path} 中 {section} 必须为对象')
    validate_servo_ids(data.get('yaw_id'), data.get('pitch_id', 2))
    # Validate explicit persisted bounds before the legacy normalizer can
    # silently clamp or exchange them. Missing keys retain migration defaults.
    validate_axis_limits({**asdict(AxisLimitsConfig()), **(data.get('axis_limits') or {})})
    direction_data = data.get("direction") or {}
    features_data = data.get("features") or {}
    camera_data = data.get("camera") or {}
    audio_mapping_data = data.get("audio_mapping") or {}
    audio_capture_data = data.get("audio_capture") or {}
    control_data = data.get("control") or {}
    axis_limits_data = data.get("axis_limits") or {}
    audio_data = data.get("audio") or {}
    vision_data = data.get("vision") or {}
    crop_left_half = camera_data.get("crop_left_half")
    return SavedSettings(
        port=data.get("port") or None,
        yaw_id=data.get("yaw_id"),
        pitch_id=int(data.get("pitch_id", 2)),
        direction=DirectionConfig(
            yaw_left_sign=int(direction_data.get("yaw_left_sign", -1)),
            pitch_up_sign=int(direction_data.get("pitch_up_sign", -1)),
            manual_step=int(direction_data.get("manual_step", 60)),
        ),
        features=FeatureConfig(
            audio_enabled=bool(features_data.get("audio_enabled", True)),
            visual_enabled=bool(features_data.get("visual_enabled", True)),
            camera_enabled=bool(features_data.get("camera_enabled", True)),
        ),
        camera=CameraConfig(
            device_name=(camera_data.get("device_name") or None),
            video_size=(camera_data.get("video_size") or None),
            crop_left_half=None if crop_left_half is None else bool(crop_left_half),
            fps=camera_data.get("fps"),
            output_width=int(camera_data.get("output_width", 1920)),
            face_detector_backend=str(camera_data.get("face_detector_backend", "mediapipe")),
            scrfd_model_path=(camera_data.get("scrfd_model_path") or None),
            scrfd_threshold=float(camera_data.get("scrfd_threshold", 0.35)),
            scrfd_input_size=int(camera_data.get("scrfd_input_size", 640)),
            capture_backend=str(camera_data.get("capture_backend", "stereo_mjpeg")),
            input_fps=int(camera_data.get("input_fps", 60)),
            usb_vid=str(camera_data.get("usb_vid", "1BCF")),
            usb_pid=str(camera_data.get("usb_pid", "0B15")),
            tracking_eye=str(camera_data.get("tracking_eye", "auto")),
            left_rotation=int(camera_data.get("left_rotation", 0)),
            right_rotation=int(camera_data.get("right_rotation", 0)),
            multi_scale_search=bool(camera_data.get("multi_scale_search", True)),
            focus_assist=bool(camera_data.get("focus_assist", True)),
            preview_jpeg_quality=int(camera_data.get("preview_jpeg_quality", 95)),
            stereo_calibration_path=str(camera_data.get("stereo_calibration_path", "calibration/stereo_camera.json")),
            stereo_geometry_enabled=bool(camera_data.get("stereo_geometry_enabled", True)),
        ),
        audio_mapping=AudioMappingConfig(
            swap_channels=bool(audio_mapping_data.get("swap_channels", False)),
        ),
        audio_capture=AudioCaptureConfig(**{key: value for key, value in audio_capture_data.items()
                                            if key in AudioCaptureConfig.__dataclass_fields__}),
        control=ControlConfig(
            control_profile=str(control_data.get("control_profile", "stable")),
            motor_guard_ms=int(control_data.get("motor_guard_ms", 250)),
            smooth_motion=bool(control_data.get("smooth_motion", True)),
            motion_hz=int(control_data.get("motion_hz", 50)),
            max_velocity=float(control_data.get("max_velocity", 180)),
            max_acceleration=float(control_data.get("max_acceleration", 600)),
            max_jerk=float(control_data.get("max_jerk", 3000)),
        ),
        axis_limits=AxisLimitsConfig(
            yaw_min=int(axis_limits_data.get("yaw_min", DEFAULT_YAW_MIN)),
            yaw_center=int(axis_limits_data.get("yaw_center", DEFAULT_YAW_CENTER)),
            yaw_max=int(axis_limits_data.get("yaw_max", DEFAULT_YAW_MAX)),
            pitch_min=int(axis_limits_data.get("pitch_min", 1200)),
            pitch_center=int(axis_limits_data.get("pitch_center", 1500)),
            pitch_max=int(axis_limits_data.get("pitch_max", 1800)),
        ),
        audio=AudioProcessingConfig(
            vad_enabled=bool(audio_data.get("vad_enabled", True)),
            audio_confidence_threshold=float(audio_data.get("audio_confidence_threshold", 0.20)),
            speech_confidence_threshold=float(audio_data.get("speech_confidence_threshold", 0.15)),
            doa_confidence_threshold=float(audio_data.get("doa_confidence_threshold", 0.05)),
            required_audio_hits=int(audio_data.get("required_audio_hits", 1)),
            denoise_enabled=bool(audio_data.get("denoise_enabled", False)),
            denoise_dry_mix=float(audio_data.get("denoise_dry_mix", 0.15)),
            denoise_output_dir=(audio_data.get("denoise_output_dir") or None),
            recording_enabled=bool(audio_data.get("recording_enabled", True)),
            use_denoised_for_localization=bool(audio_data.get("use_denoised_for_localization", False)),
        ),
        vision=VisionProcessingConfig(
            active_speaker_enabled=bool(vision_data.get("active_speaker_enabled", True)),
            tracking_strategy=str(vision_data.get("tracking_strategy", "classic_audio_first")),
            audio_sector_half_width_deg=float(vision_data.get('audio_sector_half_width_deg',15.)),
            asd_backend=str(vision_data.get("asd_backend", "rules")),
            speaker_lock_policy=str(vision_data.get("speaker_lock_policy", "turn_hold")),
            target_offset_x_norm=float(vision_data.get("target_offset_x_norm", 0.04)),
            target_offset_y_norm=float(vision_data.get("target_offset_y_norm", 0.0)),
            visual_mirror_x=bool(vision_data.get("visual_mirror_x", False)),
            visual_speaker_threshold=float(vision_data.get("visual_speaker_threshold", 0.30)),
            mouth_evidence_threshold=float(vision_data.get("mouth_evidence_threshold", 0.06)),
            visual_yaw_mode=str(vision_data.get("visual_yaw_mode", "small")),
            visual_pitch_enabled=bool(vision_data.get("visual_pitch_enabled", True)),
            visual_yaw_deadband=vision_data.get("visual_yaw_deadband"),
            visual_pitch_deadband=vision_data.get("visual_pitch_deadband"),
            visual_yaw_min_delta=int(vision_data.get("visual_yaw_min_delta") or 0),
            visual_yaw_max_delta=vision_data.get("visual_yaw_max_delta"),
            visual_pitch_min_delta=int(vision_data.get("visual_pitch_min_delta") or 0),
            visual_pitch_max_delta=vision_data.get("visual_pitch_max_delta"),
            min_face_height_ratio=float(vision_data.get("min_face_height_ratio", 0.12)),
            keep_face_height_ratio=float(vision_data.get("keep_face_height_ratio", 0.09)),
            talknet_threshold=float(vision_data.get("talknet_threshold", 0.55)),
            speaker_lock_hold_s=float(vision_data.get("speaker_lock_hold_s", 1.2)),
            speaker_lost_timeout_s=float(vision_data.get("speaker_lost_timeout_s", 0.8)),
            audio_interrupt_enabled=bool(vision_data.get("audio_interrupt_enabled", False)),
            audio_search_after_silent_visual=bool(vision_data.get("audio_search_after_silent_visual", False)),
            silent_visual_hold_s=float(vision_data.get("silent_visual_hold_s", 1.2)),
        ),
    )


def save_settings(path: Path | str | None, settings: SavedSettings) -> None:
    if path is None:
        return
    settings_path = Path(path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(settings)
    validate_servo_ids(settings.yaw_id, settings.pitch_id)
    validate_axis_limits(payload['axis_limits'])
    content = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    temporary = None
    try:
        # Same-volume replacement keeps the previous complete file if a
        # write/flush fails, rather than leaving a truncated calibration.
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.tmp',
                prefix=settings_path.name + '.', dir=settings_path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, settings_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
