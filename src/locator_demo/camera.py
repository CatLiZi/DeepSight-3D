from __future__ import annotations

import base64
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path

from .devices import choose_dshow_device
from .vision import BodyTarget, FaceTarget, choose_primary_target


@dataclass(frozen=True)
class VisionStatus:
    ok: bool
    source: str
    message: str
    fps: float = 0.0
    detector: str = "unknown"


@dataclass(frozen=True)
class VisionFrameResult:
    target: FaceTarget | None
    frame_width: int
    frame_height: int
    jpeg_base64: str
    status: VisionStatus
    targets: list[FaceTarget] = field(default_factory=list)
    body_target: BodyTarget | None = None


def _repo_models_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "models"


def _prepare_mediapipe_env() -> None:
    os.environ.setdefault("GLOG_minloglevel", "3")
    os.environ.setdefault("ABSL_MIN_LOG_LEVEL", "3")
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")


class SimulatedVisionTracker:
    def __init__(self):
        self._frame = 0

    def read(self) -> VisionFrameResult:
        self._frame += 1
        x = 260 + ((self._frame % 80) - 40) * 3
        target = FaceTarget(x, 120, x + 120, 260, label="sim-person", score=0.8)
        return VisionFrameResult(
            target=target,
            frame_width=640,
            frame_height=400,
            jpeg_base64="",
            status=VisionStatus(True, "simulated", "simulated vision", fps=12.0, detector="simulated"),
            targets=[target],
            body_target=None,
        )


class FfmpegCameraSnapshot:
    def __init__(
        self,
        *,
        device_name: str | None = None,
        video_size: str | None = None,
        crop_left_half: bool | None = None,
        fps: int | None = None,
        output_width: int = 640,
        face_detector_backend: str = "mediapipe",
        scrfd_model_path: str | None = None,
        scrfd_threshold: float = 0.35,
        scrfd_input_size: int = 640,
        ffmpeg: str = "ffmpeg",
    ):
        self.requested_device_name = device_name
        self.device_name = device_name or ""
        self.video_size = video_size
        self.crop_left_half = crop_left_half
        self.fps = self._clamp_fps(fps) if fps is not None else None
        self.output_width = self._clamp_output_width(output_width)
        self.face_detector_backend = self._face_detector_backend(face_detector_backend)
        self.scrfd_model_path = str(scrfd_model_path or "").strip() or None
        self.scrfd_threshold = self._threshold(scrfd_threshold, 0.35)
        self.scrfd_input_size = self._scrfd_input_size(scrfd_input_size)
        self.ffmpeg = ffmpeg
        self._last_time = 0.0
        self._lock = threading.Lock()
        self._cascade = None
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._latest_jpeg: bytes = b""
        self._latest_target: FaceTarget | None = None
        self._latest_targets: list[FaceTarget] = []
        self._latest_body_target: BodyTarget | None = None
        self._latest_detector = "none"
        self._latest_detector_detail = ""
        self._latest_error = "stopped"
        self._latest_frame_time = 0.0
        self._recent_frames: deque[tuple[float, bytes, int, int]] = deque(maxlen=90)
        self._process_started_at = 0.0
        self._output_fps = self._clamp_fps(fps) if fps is not None else 15
        self._frame_width = self.output_width
        self._frame_height = round(self.output_width * 10 / 16)
        self._face_mesh = None
        self._face_landmarker = None
        self._scrfd_detector = None
        self._scrfd_detector_key: tuple[str, float, int] | None = None
        self._scrfd_device = "unknown"
        self._pose = None
        self._pose_landmarker = None
        self._previous_gray = None
        self._previous_mouth_open: dict[int, float] = {}
        self._tracked_targets: list[tuple[FaceTarget, float]] = []
        self._next_tracked_face_id = 1
        self._target_hold_s = 0.45
        self._target_smooth_alpha = 0.65

    @staticmethod
    def _model_path_from_env(env_name: str, filenames: tuple[str, ...]) -> Path | None:
        override = (os.environ.get(env_name) or "").strip()
        if override:
            path = Path(override).expanduser()
            return path if path.exists() else None
        model_dir = _repo_models_dir()
        for filename in filenames:
            path = model_dir / filename
            if path.exists():
                return path
        return None

    @staticmethod
    def _face_detector_backend(value: str | None) -> str:
        backend = str(value or "mediapipe").strip().lower()
        return backend if backend in ("mediapipe", "scrfd") else "mediapipe"

    @staticmethod
    def _threshold(value: float | None, default: float) -> float:
        try:
            return max(0.0, min(1.0, float(value if value is not None else default)))
        except Exception:
            return default

    @staticmethod
    def _scrfd_input_size(value: int | None) -> int:
        try:
            size = max(320, min(1280, int(value if value is not None else 640)))
        except Exception:
            return 640
        return max(320, (size // 32) * 32)

    @staticmethod
    def _missing_model_message(env_name: str, filenames: tuple[str, ...]) -> str:
        default_path = _repo_models_dir() / filenames[0]
        return f"mediapipe Tasks model missing: {default_path} or env {env_name}"

    def _close_detector(self, attribute: str) -> None:
        detector = getattr(self, attribute, None)
        if detector is None:
            return
        try:
            close = getattr(detector, "close", None)
            if callable(close):
                close()
        except Exception:
            pass
        setattr(self, attribute, None)

    def _close_mediapipe_detectors(self) -> None:
        for attribute in ("_face_landmarker", "_pose_landmarker", "_face_mesh", "_pose"):
            self._close_detector(attribute)
        self._scrfd_detector = None
        self._scrfd_detector_key = None

    def _resolve_device_name(self) -> str | None:
        if self.device_name:
            return self.device_name
        device = choose_dshow_device(
            "video",
            [self.requested_device_name, "USB Camera", "Integrated Camera"],
            ffmpeg=self.ffmpeg,
        )
        if device is None:
            self._latest_error = "no DirectShow video input found"
            return None
        self.device_name = device.name
        return self.device_name

    def _capture_options(self, device_name: str) -> tuple[str, str, bool]:
        if self.video_size:
            size = self.video_size
        elif "usb camera" in device_name.casefold():
            size = "2560x800"
        else:
            size = "1280x720"

        if self.crop_left_half is None:
            crop_left_half = size == "2560x800"
        else:
            crop_left_half = self.crop_left_half

        output_fps = self._resolved_output_fps(size, crop_left_half)
        default_input_fps = 5 if size == "2560x800" else 30
        input_fps = str(max(default_input_fps, output_fps))
        return size, input_fps, crop_left_half

    @staticmethod
    def _scaled_dimensions(video_size: str, crop_left_half: bool, output_width: int = 640) -> tuple[int, int]:
        try:
            width_text, height_text = video_size.lower().split("x", 1)
            source_width = int(width_text)
            source_height = int(height_text)
        except Exception:
            return output_width, max(1, round(output_width * 10 / 16))
        if crop_left_half:
            source_width = source_width // 2
        return output_width, max(1, round(output_width * source_height / source_width))

    @staticmethod
    def _clamp_fps(fps: int | None) -> int:
        try:
            return max(1, min(30, int(fps if fps is not None else 15)))
        except Exception:
            return 15

    @staticmethod
    def _clamp_output_width(output_width: int | None) -> int:
        try:
            return max(320, min(1920, int(output_width if output_width is not None else 640)))
        except Exception:
            return 640

    @staticmethod
    def _default_output_fps(video_size: str, crop_left_half: bool) -> int:
        return 5 if crop_left_half or video_size == "2560x800" else 15

    def _resolved_output_fps(self, video_size: str, crop_left_half: bool) -> int:
        if self.fps is not None:
            return self._clamp_fps(self.fps)
        return self._default_output_fps(video_size, crop_left_half)

    @staticmethod
    def _video_filter(
        video_size: str,
        crop_left_half: bool,
        fps: int | None = None,
        output_width: int = 640,
    ) -> str:
        output_fps = FfmpegCameraSnapshot._clamp_fps(
            fps if fps is not None else FfmpegCameraSnapshot._default_output_fps(video_size, crop_left_half)
        )
        width = FfmpegCameraSnapshot._clamp_output_width(output_width)
        if crop_left_half:
            return f"fps={output_fps},crop=1280:800:0:0,scale={width}:-1"
        return f"fps={output_fps},scale={width}:-1"

    @staticmethod
    def _stderr_tail(process: subprocess.Popen) -> str:
        if process.stderr is None:
            return ""
        return process.stderr.read().decode("utf-8", errors="replace").strip()

    @staticmethod
    def _mouth_motion_from_roi(gray, previous_gray, x: int, y: int, w: int, h: int) -> float:
        if previous_gray is None or w <= 4 or h <= 4:
            return 0.0
        x1 = max(0, x + int(w * 0.25))
        x2 = max(x1 + 1, x + int(w * 0.75))
        y1 = max(0, y + int(h * 0.58))
        y2 = max(y1 + 1, y + int(h * 0.90))
        roi = gray[y1:y2, x1:x2]
        prev = previous_gray[y1:y2, x1:x2]
        if roi.size == 0 or prev.size == 0 or roi.shape != prev.shape:
            return 0.0
        try:
            import cv2

            diff = cv2.absdiff(roi, prev)
            return min(1.0, float(diff.mean()) / 18.0)
        except Exception:
            return 0.0

    def _face_target_from_landmarks(self, face_id: int, landmarks, width: int, height: int) -> FaceTarget | None:
        if not landmarks:
            return None
        try:
            def landmark_xy(index: int) -> tuple[float, float]:
                point = landmarks[index]
                return (float(point.x) * width, float(point.y) * height)

            xs = [float(point.x) * width for point in landmarks]
            ys = [float(point.y) * height for point in landmarks]
            x1, x2 = max(0.0, min(xs)), min(float(width), max(xs))
            y1, y2 = max(0.0, min(ys)), min(float(height), max(ys))
            face_h = max(1.0, y2 - y1)
            left_eye = landmarks[33]
            right_eye = landmarks[263]
            nose = landmarks[1]
            left_eye_xy = landmark_xy(33)
            right_eye_xy = landmark_xy(263)
            nose_xy = landmark_xy(1)
            upper_lip_xy = landmark_xy(13)
            lower_lip_xy = landmark_xy(14)
            eye_mid_xy = (
                (left_eye_xy[0] + right_eye_xy[0]) * 0.5,
                (left_eye_xy[1] + right_eye_xy[1]) * 0.5,
            )
            mouth_mid_xy = (
                (upper_lip_xy[0] + lower_lip_xy[0]) * 0.5,
                (upper_lip_xy[1] + lower_lip_xy[1]) * 0.5,
            )
            tracking_x = max(
                0.0,
                min(float(width), 0.45 * eye_mid_xy[0] + 0.35 * nose_xy[0] + 0.20 * mouth_mid_xy[0]),
            )
            tracking_y = max(
                0.0,
                min(float(height), 0.30 * eye_mid_xy[1] + 0.35 * nose_xy[1] + 0.35 * mouth_mid_xy[1]),
            )
            eye_mid_x = (left_eye.x + right_eye.x) * 0.5
            eye_distance = max(0.001, abs(right_eye.x - left_eye.x))
            face_yaw_deg = max(-45.0, min(45.0, (nose.x - eye_mid_x) / eye_distance * 35.0))
            frontal_score = max(0.0, 1.0 - abs(face_yaw_deg) / 35.0)
            mouth_open = abs(landmarks[13].y - landmarks[14].y) * height / face_h
            last_open = self._previous_mouth_open.get(face_id, mouth_open)
            mouth_motion = min(1.0, abs(mouth_open - last_open) * 7.0)
            self._previous_mouth_open[face_id] = mouth_open
            active_score = min(1.0, 0.25 * frontal_score + 0.60 * mouth_motion + 0.05)
            return FaceTarget(
                x1,
                y1,
                x2,
                y2,
                label="face",
                score=0.85,
                face_id=face_id,
                face_yaw_deg=face_yaw_deg,
                frontal_score=frontal_score,
                mouth_motion_score=mouth_motion,
                mouth_audio_sync_score=mouth_motion,
                active_speaker_score=active_score,
                tracking_x=tracking_x,
                tracking_y=tracking_y,
                tracking_source="mediapipe_landmarks",
            )
        except Exception:
            return None

    def _resolve_scrfd_model_path(self) -> Path | None:
        override = (os.environ.get("LOCATOR_SCRFD_MODEL") or "").strip() or self.scrfd_model_path
        if override:
            path = Path(override).expanduser()
            candidates = [path]
            if not path.is_absolute():
                candidates.append(Path(__file__).resolve().parents[2] / path)
            for candidate in candidates:
                if candidate.exists():
                    return candidate
            return None
        return self._model_path_from_env(
            "LOCATOR_SCRFD_MODEL",
            (
                "scrfd_2.5g_kps.onnx",
                "scrfd_2.5g_bnkps.onnx",
                "scrfd_10g_kps.onnx",
                "scrfd_10g_bnkps.onnx",
                "scrfd_34g_gnkps.onnx",
                "det_10g.onnx",
            ),
        )

    def _missing_scrfd_message(self) -> str:
        default_path = _repo_models_dir() / "scrfd_2.5g_kps.onnx"
        return f"SCRFD model missing: {default_path} or env LOCATOR_SCRFD_MODEL"

    def _prepare_scrfd_detector(self):
        model_path = self._resolve_scrfd_model_path()
        if model_path is None:
            self._latest_detector = "scrfd_unavailable"
            self._latest_detector_detail = self._missing_scrfd_message()
            return None
        key = (str(model_path), round(float(self.scrfd_threshold), 4), int(self.scrfd_input_size))
        if self._scrfd_detector is not None and self._scrfd_detector_key == key:
            return self._scrfd_detector
        try:
            providers = ["CPUExecutionProvider"]
            self._scrfd_device = "cpu"
            try:
                import onnxruntime as ort

                available = set(ort.get_available_providers())
                if "CUDAExecutionProvider" in available:
                    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
                    self._scrfd_device = "cuda"
            except Exception:
                pass
            from insightface.model_zoo import get_model

            try:
                detector = get_model(str(model_path), providers=providers)
            except TypeError:
                detector = get_model(str(model_path))
            detector.prepare(
                ctx_id=0 if self._scrfd_device == "cuda" else -1,
                input_size=(int(self.scrfd_input_size), int(self.scrfd_input_size)),
                det_thresh=float(self.scrfd_threshold),
            )
            self._scrfd_detector = detector
            self._scrfd_detector_key = key
            return detector
        except Exception as exc:
            self._scrfd_detector = None
            self._scrfd_detector_key = None
            self._latest_detector = "scrfd_error"
            self._latest_detector_detail = f"SCRFD load error: {type(exc).__name__}: {exc}"
            return None

    def _face_target_from_scrfd_detection(self, face_id: int, bbox, kps, gray) -> FaceTarget | None:
        try:
            height, width = gray.shape[:2]
            values = [float(value) for value in bbox]
            if len(values) < 4:
                return None
            x1 = max(0.0, min(float(width), values[0]))
            y1 = max(0.0, min(float(height), values[1]))
            x2 = max(0.0, min(float(width), values[2]))
            y2 = max(0.0, min(float(height), values[3]))
            if x2 <= x1 or y2 <= y1:
                return None
            score = max(0.0, min(1.0, values[4] if len(values) >= 5 else 0.8))
            box_w = max(1.0, x2 - x1)
            box_h = max(1.0, y2 - y1)
            mouth_motion = self._mouth_motion_from_roi(
                gray,
                self._previous_gray,
                int(x1),
                int(y1),
                int(box_w),
                int(box_h),
            )
            tracking_x, tracking_y = (x1 + x2) * 0.5, (y1 + y2) * 0.5
            face_yaw_deg = 0.0
            frontal_score = 0.75
            tracking_source = "scrfd_bbox"
            if kps is not None and len(kps) >= 5:
                points = [(float(point[0]), float(point[1])) for point in kps[:5]]
                left_eye, right_eye, nose, left_mouth, right_mouth = points
                eye_mid = ((left_eye[0] + right_eye[0]) * 0.5, (left_eye[1] + right_eye[1]) * 0.5)
                mouth_mid = ((left_mouth[0] + right_mouth[0]) * 0.5, (left_mouth[1] + right_mouth[1]) * 0.5)
                tracking_x = max(0.0, min(float(width), 0.45 * eye_mid[0] + 0.35 * nose[0] + 0.20 * mouth_mid[0]))
                tracking_y = max(0.0, min(float(height), 0.30 * eye_mid[1] + 0.35 * nose[1] + 0.35 * mouth_mid[1]))
                eye_distance = max(1.0, abs(right_eye[0] - left_eye[0]))
                face_yaw_deg = max(-45.0, min(45.0, (nose[0] - eye_mid[0]) / eye_distance * 35.0))
                frontal_score = max(0.0, 1.0 - abs(face_yaw_deg) / 35.0)
                tracking_source = "scrfd_kps"
            active_score = min(1.0, 0.15 * frontal_score + 0.65 * mouth_motion + 0.12 * score)
            return FaceTarget(
                x1,
                y1,
                x2,
                y2,
                label="face",
                score=score,
                face_id=face_id,
                face_yaw_deg=face_yaw_deg,
                frontal_score=frontal_score,
                mouth_motion_score=mouth_motion,
                mouth_audio_sync_score=mouth_motion,
                active_speaker_score=active_score,
                tracking_x=tracking_x,
                tracking_y=tracking_y,
                tracking_source=tracking_source,
                backend="scrfd",
            )
        except Exception:
            return None

    def _scrfd_targets(self, image, gray) -> list[FaceTarget] | None:
        if self.face_detector_backend != "scrfd":
            return None
        detector = self._prepare_scrfd_detector()
        if detector is None:
            return None
        try:
            try:
                bboxes, kpss = detector.detect(image, max_num=0, metric="default")
            except TypeError:
                bboxes, kpss = detector.detect(image)
            targets: list[FaceTarget] = []
            detections = [] if bboxes is None else bboxes
            keypoints = [] if kpss is None else kpss
            for index, bbox in enumerate(detections):
                kps = None if index >= len(keypoints) else keypoints[index]
                target = self._face_target_from_scrfd_detection(index, bbox, kps, gray)
                if target is not None:
                    targets.append(target)
            self._latest_detector = f"scrfd_{self._scrfd_device}"
            self._latest_detector_detail = ""
            return targets
        except Exception as exc:
            self._latest_detector = "scrfd_error"
            self._latest_detector_detail = f"SCRFD detect error: {type(exc).__name__}: {exc}"
            return None

    def _body_target_from_landmarks(self, landmarks, width: int, height: int) -> BodyTarget | None:
        if not landmarks:
            return None

        def visible_point(index: int, threshold: float = 0.35) -> tuple[float, float, float] | None:
            point = landmarks[index]
            visibility = float(getattr(point, "visibility", 1.0))
            if visibility < threshold:
                return None
            return (float(point.x) * width, float(point.y) * height, visibility)

        nose = visible_point(0)
        left_shoulder = visible_point(11)
        right_shoulder = visible_point(12)
        visible_upper = [
            point
            for index in (0, 7, 8, 11, 12)
            if (point := visible_point(index, threshold=0.25)) is not None
        ]
        if nose is not None:
            target_x, target_y, head_visibility = nose
        elif left_shoulder is not None and right_shoulder is not None:
            shoulder_mid_x = (left_shoulder[0] + right_shoulder[0]) * 0.5
            shoulder_mid_y = (left_shoulder[1] + right_shoulder[1]) * 0.5
            shoulder_width = abs(right_shoulder[0] - left_shoulder[0])
            target_x = shoulder_mid_x
            target_y = shoulder_mid_y - max(35.0, shoulder_width * 0.55)
            head_visibility = min(left_shoulder[2], right_shoulder[2])
        else:
            return None
        if not visible_upper:
            return None
        xs = [point[0] for point in visible_upper]
        margin = max(24.0, (max(xs) - min(xs)) * 0.35)
        box_w = max(48.0, margin * 2.0)
        box_h = max(56.0, box_w * 1.15)
        x1 = max(0.0, target_x - box_w * 0.5)
        x2 = min(float(width), target_x + box_w * 0.5)
        y1 = max(0.0, target_y - box_h * 0.45)
        y2 = min(float(height), target_y + box_h * 0.55)
        visibility = sum(point[2] for point in visible_upper) / len(visible_upper)
        score = min(1.0, max(0.0, 0.55 * visibility + 0.45 * head_visibility))
        return BodyTarget(x1, y1, x2, y2, label="body", score=score, visibility=visibility)

    def _mediapipe_tasks_face_targets(self, mp, cv2, image, gray) -> list[FaceTarget] | None:
        model_path = self._model_path_from_env("LOCATOR_FACE_LANDMARKER_MODEL", ("face_landmarker.task",))
        if model_path is None:
            self._latest_detector_detail = self._missing_model_message(
                "LOCATOR_FACE_LANDMARKER_MODEL",
                ("face_landmarker.task",),
            )
            return None
        try:
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision as mp_vision

            if self._face_landmarker is None:
                base_options_cls = getattr(getattr(mp, "tasks", None), "BaseOptions", None) or mp_python.BaseOptions
                options = mp_vision.FaceLandmarkerOptions(
                    base_options=base_options_cls(model_asset_path=str(model_path)),
                    running_mode=mp_vision.RunningMode.IMAGE,
                    num_faces=4,
                    min_face_detection_confidence=0.5,
                    min_face_presence_confidence=0.5,
                    min_tracking_confidence=0.5,
                )
                self._face_landmarker = mp_vision.FaceLandmarker.create_from_options(options)
            rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            result = self._face_landmarker.detect(mp_image)
            self._latest_detector = "mediapipe_tasks"
            self._latest_detector_detail = ""
            height, width = gray.shape[:2]
            targets = []
            for face_id, landmarks in enumerate(getattr(result, "face_landmarks", []) or []):
                target = self._face_target_from_landmarks(face_id, landmarks, width, height)
                if target is not None:
                    targets.append(target)
            return targets
        except Exception as exc:
            self._latest_detector = "mediapipe_tasks_error"
            self._latest_detector_detail = f"mediapipe Tasks face error: {type(exc).__name__}: {exc}"
            self._face_landmarker = None
            return None

    def _mediapipe_tasks_body_target(self, mp, cv2, image, gray) -> BodyTarget | None:
        model_path = self._model_path_from_env(
            "LOCATOR_POSE_LANDMARKER_MODEL",
            ("pose_landmarker_lite.task", "pose_landmarker.task"),
        )
        if model_path is None:
            self._latest_detector_detail = self._missing_model_message(
                "LOCATOR_POSE_LANDMARKER_MODEL",
                ("pose_landmarker_lite.task",),
            )
            return None
        try:
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision as mp_vision

            if self._pose_landmarker is None:
                base_options_cls = getattr(getattr(mp, "tasks", None), "BaseOptions", None) or mp_python.BaseOptions
                options = mp_vision.PoseLandmarkerOptions(
                    base_options=base_options_cls(model_asset_path=str(model_path)),
                    running_mode=mp_vision.RunningMode.IMAGE,
                    num_poses=1,
                    min_pose_detection_confidence=0.5,
                    min_pose_presence_confidence=0.5,
                    min_tracking_confidence=0.5,
                    output_segmentation_masks=False,
                )
                self._pose_landmarker = mp_vision.PoseLandmarker.create_from_options(options)
            rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            result = self._pose_landmarker.detect(mp_image)
            pose_landmarks = getattr(result, "pose_landmarks", []) or []
            if not pose_landmarks:
                return None
            self._latest_detector = "mediapipe_tasks_pose"
            height, width = gray.shape[:2]
            return self._body_target_from_landmarks(pose_landmarks[0], width, height)
        except Exception as exc:
            self._latest_detector_detail = f"mediapipe Tasks pose error: {type(exc).__name__}: {exc}"
            self._pose_landmarker = None
            return None

    def _mediapipe_targets(self, image, gray) -> list[FaceTarget] | None:
        try:
            _prepare_mediapipe_env()
            import cv2
            import mediapipe as mp
        except Exception as exc:
            self._latest_detector_detail = f"mediapipe import failed: {type(exc).__name__}: {exc}"
            return None
        solutions = getattr(mp, "solutions", None)
        if solutions is None or not hasattr(solutions, "face_mesh"):
            return self._mediapipe_tasks_face_targets(mp, cv2, image, gray)
        try:
            if self._face_mesh is None:
                self._face_mesh = solutions.face_mesh.FaceMesh(
                    static_image_mode=False,
                    max_num_faces=4,
                    refine_landmarks=True,
                    min_detection_confidence=0.5,
                    min_tracking_confidence=0.5,
                )
            rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            result = self._face_mesh.process(rgb)
            self._latest_detector = "mediapipe"
            self._latest_detector_detail = ""
            if not result.multi_face_landmarks:
                return []
            height, width = gray.shape[:2]
            targets: list[FaceTarget] = []
            for face_id, landmarks in enumerate(result.multi_face_landmarks):
                target = self._face_target_from_landmarks(face_id, landmarks.landmark, width, height)
                if target is not None:
                    targets.append(target)
            return targets
        except Exception as exc:
            self._latest_detector = "mediapipe_error"
            self._latest_detector_detail = f"mediapipe face error: {type(exc).__name__}: {exc}"
            self._face_mesh = None
            return None

    def _mediapipe_body_target(self, image, gray) -> BodyTarget | None:
        try:
            _prepare_mediapipe_env()
            import cv2
            import mediapipe as mp
        except Exception as exc:
            self._latest_detector_detail = f"mediapipe import failed: {type(exc).__name__}: {exc}"
            return None
        solutions = getattr(mp, "solutions", None)
        if solutions is None or not hasattr(solutions, "pose"):
            return self._mediapipe_tasks_body_target(mp, cv2, image, gray)
        try:
            if self._pose is None:
                self._pose = solutions.pose.Pose(
                    static_image_mode=False,
                    model_complexity=0,
                    enable_segmentation=False,
                    min_detection_confidence=0.45,
                    min_tracking_confidence=0.45,
                )
            rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            result = self._pose.process(rgb)
            if not result.pose_landmarks:
                return None
            height, width = gray.shape[:2]
            return self._body_target_from_landmarks(result.pose_landmarks.landmark, width, height)
        except Exception as exc:
            self._latest_detector_detail = f"mediapipe pose error: {type(exc).__name__}: {exc}"
            self._pose = None
            return None

    def _detect_targets(self, jpeg_bytes: bytes) -> list[FaceTarget]:
        try:
            import cv2
            import numpy as np
        except Exception:
            self._latest_body_target = None
            return []
        try:
            self._latest_body_target = None
            data = np.frombuffer(jpeg_bytes, dtype=np.uint8)
            image = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if image is None:
                return []
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            self._latest_body_target = self._mediapipe_body_target(image, gray)
            scrfd_detail = ""
            if self.face_detector_backend == "scrfd":
                targets = self._scrfd_targets(image, gray)
                if targets is not None:
                    self._previous_gray = gray
                    return targets
                scrfd_detail = self._latest_detector_detail
            targets = self._mediapipe_targets(image, gray)
            if targets is not None:
                if scrfd_detail:
                    self._latest_detector = f"{self._latest_detector}_after_scrfd_fallback"
                    self._latest_detector_detail = scrfd_detail
                self._previous_gray = gray
                return targets
            if scrfd_detail and not self._latest_detector_detail:
                self._latest_detector_detail = scrfd_detail
            if self._cascade is None:
                cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
                self._cascade = cv2.CascadeClassifier(cascade_path)
            rects = self._cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))
            targets = [
                FaceTarget(
                    float(x),
                    float(y),
                    float(x + w),
                    float(y + h),
                    label="face",
                    score=0.8,
                    face_id=index,
                    frontal_score=0.8,
                    mouth_motion_score=self._mouth_motion_from_roi(gray, self._previous_gray, int(x), int(y), int(w), int(h)),
                    mouth_audio_sync_score=self._mouth_motion_from_roi(gray, self._previous_gray, int(x), int(y), int(w), int(h)),
                )
                for index, (x, y, w, h) in enumerate(rects)
            ]
            scored_targets = [
                FaceTarget(
                    target.x1,
                    target.y1,
                    target.x2,
                    target.y2,
                    target.label,
                    target.score,
                    target.face_id,
                    target.face_yaw_deg,
                    target.frontal_score,
                    target.mouth_motion_score,
                    target.mouth_audio_sync_score,
                    min(1.0, 0.25 * target.frontal_score + 0.65 * target.mouth_motion_score + 0.08 * target.score),
                )
                for target in targets
            ]
            self._previous_gray = gray
            if scored_targets:
                self._latest_detector = "haar_after_mediapipe_unavailable" if self._latest_detector_detail else "haar"
            elif not self._latest_detector_detail:
                self._latest_detector = "none"
            return scored_targets
        except Exception:
            return []

    def _detect_faces(self, jpeg_bytes: bytes) -> FaceTarget | None:
        return choose_primary_target(self._detect_targets(jpeg_bytes))

    @staticmethod
    def _lerp(a: float, b: float, alpha: float) -> float:
        return alpha * b + (1.0 - alpha) * a

    def _target_match_distance(self, a: FaceTarget, b: FaceTarget) -> float:
        ax, ay = a.tracking_center
        bx, by = b.tracking_center
        return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5

    def _smooth_target(self, previous: FaceTarget, current: FaceTarget) -> FaceTarget:
        alpha = float(self._target_smooth_alpha)
        tracking_x = current.tracking_x
        tracking_y = current.tracking_y
        if previous.tracking_x is not None and current.tracking_x is not None:
            tracking_x = self._lerp(previous.tracking_x, current.tracking_x, alpha)
        if previous.tracking_y is not None and current.tracking_y is not None:
            tracking_y = self._lerp(previous.tracking_y, current.tracking_y, alpha)
        return replace(
            current,
            x1=self._lerp(previous.x1, current.x1, alpha),
            y1=self._lerp(previous.y1, current.y1, alpha),
            x2=self._lerp(previous.x2, current.x2, alpha),
            y2=self._lerp(previous.y2, current.y2, alpha),
            face_id=previous.face_id,
            tracking_x=tracking_x,
            tracking_y=tracking_y,
        )

    def _held_target(self, target: FaceTarget) -> FaceTarget:
        source = target.tracking_source or "detector"
        return replace(
            target,
            score=min(float(target.score), 0.45),
            mouth_motion_score=0.0,
            mouth_audio_sync_score=0.0,
            active_speaker_score=0.0,
            tracking_source=f"{source}_held",
        )

    def _assign_new_tracked_face_id(self, target: FaceTarget, used_ids: set[int]) -> FaceTarget:
        while self._next_tracked_face_id in used_ids:
            self._next_tracked_face_id += 1
        face_id = self._next_tracked_face_id
        self._next_tracked_face_id += 1
        return replace(target, face_id=face_id)

    def _stabilize_targets(self, targets: list[FaceTarget], now_s: float | None = None) -> list[FaceTarget]:
        now = time.time() if now_s is None else float(now_s)
        previous = [(target, seen_s) for target, seen_s in self._tracked_targets if now - seen_s <= self._target_hold_s]
        if not targets:
            held = [self._held_target(target) for target, _seen_s in previous]
            self._tracked_targets = previous
            return held

        previous_face_ids = {previous_target.face_id for previous_target, _seen_s in previous}
        used_face_ids: set[int] = set()
        stabilized: list[FaceTarget] = []
        new_tracked: list[tuple[FaceTarget, float]] = []
        used_previous: set[int] = set()
        match_threshold = max(40.0, min(220.0, 0.15 * ((self._frame_width**2 + self._frame_height**2) ** 0.5)))
        for target in targets:
            best_index = None
            best_distance = match_threshold
            for index, (previous_target, _seen_s) in enumerate(previous):
                if index in used_previous:
                    continue
                distance = self._target_match_distance(previous_target, target)
                if distance < best_distance:
                    best_index = index
                    best_distance = distance
            if best_index is None:
                if target.face_id in previous_face_ids or target.face_id in used_face_ids:
                    stabilized_target = self._assign_new_tracked_face_id(target, previous_face_ids | used_face_ids)
                else:
                    stabilized_target = target
            else:
                used_previous.add(best_index)
                stabilized_target = self._smooth_target(previous[best_index][0], target)
            used_face_ids.add(stabilized_target.face_id)
            stabilized.append(stabilized_target)
            new_tracked.append((stabilized_target, now))

        for index, (previous_target, seen_s) in enumerate(previous):
            if index not in used_previous and now - seen_s <= self._target_hold_s:
                held_target = self._held_target(previous_target)
                if held_target.face_id in used_face_ids:
                    held_target = self._assign_new_tracked_face_id(held_target, previous_face_ids | used_face_ids)
                    previous_target = replace(previous_target, face_id=held_target.face_id)
                used_face_ids.add(held_target.face_id)
                stabilized.append(held_target)
                new_tracked.append((previous_target, seen_s))
        self._tracked_targets = new_tracked
        return stabilized

    def _start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        device_name = self._resolve_device_name()
        if device_name is None:
            return
        video_size, input_fps, crop_left_half = self._capture_options(device_name)
        output_fps = self._resolved_output_fps(video_size, crop_left_half)
        self._output_fps = output_fps
        self._frame_width, self._frame_height = self._scaled_dimensions(video_size, crop_left_half, self.output_width)
        vf = self._video_filter(video_size, crop_left_half, output_fps, self.output_width)
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "dshow",
            "-video_size",
            video_size,
            "-framerate",
            input_fps,
            "-vcodec",
            "mjpeg",
            "-i",
            f"video={device_name}",
            "-an",
            "-vf",
            vf,
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-q:v",
            "5",
            "-",
        ]
        try:
            self._process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as exc:
            self._process = None
            self._latest_error = f"camera error: {exc}"
            return
        self._process_started_at = time.time()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._latest_error = "starting"

    def _read_loop(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        buffer = bytearray()
        while process.poll() is None:
            chunk = process.stdout.read(4096)
            if not chunk:
                time.sleep(0.01)
                continue
            buffer.extend(chunk)
            while True:
                start = buffer.find(b"\xff\xd8")
                end = buffer.find(b"\xff\xd9", start + 2 if start >= 0 else 0)
                if start < 0:
                    del buffer[:-2]
                    break
                if end < 0:
                    if start > 0:
                        del buffer[:start]
                    break
                frame = bytes(buffer[start : end + 2])
                del buffer[: end + 2]
                targets = self._stabilize_targets(self._detect_targets(frame))
                target = choose_primary_target(targets)
                body_target = self._latest_body_target
                with self._lock:
                    self._latest_jpeg = frame
                    self._latest_target = target
                    self._latest_targets = targets
                    self._latest_body_target = body_target
                    self._latest_frame_time = time.time()
                    self._recent_frames.append(
                        (self._latest_frame_time, frame, self._frame_width, self._frame_height)
                    )
                    self._latest_error = "streaming"
        stderr = self._stderr_tail(process)
        with self._lock:
            self._latest_error = stderr.splitlines()[-1] if stderr else f"ffmpeg exited with {process.returncode}"

    def stop(self) -> None:
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None
        self._close_mediapipe_detectors()

    def __del__(self) -> None:
        try:
            self.stop()
        except Exception:
            pass

    def read(self) -> VisionFrameResult:
        try:
            self._start()
        except Exception as exc:
            return VisionFrameResult(
                None,
                frame_width=640,
                frame_height=400,
                jpeg_base64="",
                status=VisionStatus(False, self.device_name, f"camera error: {exc}", detector="error"),
            )
        with self._lock:
            jpeg = self._latest_jpeg
            target = self._latest_target
            targets = list(self._latest_targets)
            body_target = self._latest_body_target
            detector = self._latest_detector
            detector_detail = self._latest_detector_detail
            age = time.time() - self._latest_frame_time if self._latest_frame_time else 999.0
            status = self._latest_error
        if detector_detail and status == "streaming":
            status = f"{status}; {detector_detail}"
        if not jpeg and self._process is not None and self._process.poll() is None:
            if time.time() - self._process_started_at > 5.0:
                self.stop()
                status = "no camera frames after 5s"
        if not jpeg:
            return VisionFrameResult(
                None,
                self._frame_width,
                self._frame_height,
                "",
                VisionStatus(False, self.device_name, status, fps=0.0, detector=detector),
                [],
                None,
            )
        return VisionFrameResult(
            target=target,
            frame_width=self._frame_width,
            frame_height=self._frame_height,
            jpeg_base64=base64.b64encode(jpeg).decode("ascii"),
            status=VisionStatus(age < 2.0, self.device_name, status, fps=float(self._output_fps), detector=detector),
            targets=targets,
            body_target=body_target,
        )

    def recent_frames(self, *, seconds: float = 2.0) -> list[tuple[float, bytes, int, int]]:
        cutoff = time.time() - max(0.1, float(seconds))
        with self._lock:
            return [entry for entry in self._recent_frames if entry[0] >= cutoff]
