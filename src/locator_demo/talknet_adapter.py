from __future__ import annotations

import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .vision import FaceTarget


_MODEL = None
_MODEL_LOCK = threading.Lock()
_MODEL_MESSAGE = "not loaded"
_SCORE_CACHE: dict[int, tuple[float, float]] = {}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _repo_path() -> Path:
    override = (os.environ.get("LOCATOR_TALKNET_REPO") or "").strip()
    if override:
        return Path(override).expanduser()
    return _project_root() / "third_party" / "TalkNet-ASD"


def _model_path(repo: Path) -> Path:
    override = (os.environ.get("LOCATOR_TALKNET_MODEL") or "").strip()
    if override:
        return Path(override).expanduser()
    for filename in ("pretrain_TalkSet.model", "pretrain_AVA.model"):
        candidate = repo / filename
        if candidate.exists():
            return candidate
    return repo / "pretrain_TalkSet.model"


def is_available() -> tuple[bool, str]:
    repo = _repo_path()
    model = _model_path(repo)
    if not repo.exists():
        return False, f"TalkNet repo missing: {repo}"
    if not (repo / "talkNet.py").exists():
        return False, f"TalkNet repo has no talkNet.py: {repo}"
    if not model.exists():
        return False, f"TalkNet pretrained model missing: {model}"
    try:
        import torch

        if not torch.cuda.is_available():
            return False, "TalkNet official model requires CUDA in this adapter"
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        import python_speech_features  # noqa: F401
    except Exception as exc:
        return False, f"TalkNet dependency missing: {type(exc).__name__}: {exc}"
    return True, f"TalkNet ready: {model}"


def _load_model():
    global _MODEL, _MODEL_MESSAGE
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        ok, message = is_available()
        if not ok:
            raise RuntimeError(message)
        repo = _repo_path()
        model_path = _model_path(repo)
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
        from talkNet import talkNet

        model = talkNet()
        model.loadParameters(str(model_path))
        model.eval()
        _MODEL = model
        _MODEL_MESSAGE = message
        return _MODEL


def _decode_jpeg(jpeg_bytes: bytes):
    import cv2
    import numpy as np

    data = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def _crop_face(image, target: FaceTarget, *, frame_width: int, frame_height: int, entry_width: int, entry_height: int):
    import cv2

    if image is None:
        return None
    scale_x = float(entry_width) / max(1.0, float(frame_width))
    scale_y = float(entry_height) / max(1.0, float(frame_height))
    x1 = float(target.x1) * scale_x
    y1 = float(target.y1) * scale_y
    x2 = float(target.x2) * scale_x
    y2 = float(target.y2) * scale_y
    cx = (x1 + x2) * 0.5
    cy = (y1 + y2) * 0.5
    size = max(x2 - x1, y2 - y1) * 1.45
    if size < 8:
        return None
    left = max(0, int(round(cx - size * 0.5)))
    right = min(entry_width, int(round(cx + size * 0.5)))
    top = max(0, int(round(cy - size * 0.55)))
    bottom = min(entry_height, int(round(cy + size * 0.65)))
    if right - left < 8 or bottom - top < 8:
        return None
    face = image[top:bottom, left:right]
    face = cv2.resize(face, (224, 224))
    face = cv2.cvtColor(face, cv2.COLOR_BGR2GRAY)
    return face[56:168, 56:168]


def _resample_faces(frames, target: FaceTarget, *, frame_width: int, frame_height: int, duration_s: float):
    import numpy as np

    if len(frames) < 3:
        return None
    frames = sorted(frames, key=lambda item: item[0])
    end_ts = frames[-1][0]
    start_ts = max(frames[0][0], end_ts - duration_s)
    usable = [entry for entry in frames if entry[0] >= start_ts]
    if len(usable) < 3:
        return None
    target_count = max(8, int(round(duration_s * 25.0)))
    sample_times = np.linspace(usable[0][0], usable[-1][0], num=target_count)
    crops = []
    cursor = 0
    for sample_time in sample_times:
        while cursor + 1 < len(usable) and abs(usable[cursor + 1][0] - sample_time) < abs(usable[cursor][0] - sample_time):
            cursor += 1
        _ts, jpeg, entry_width, entry_height = usable[cursor]
        image = _decode_jpeg(jpeg)
        crop = _crop_face(
            image,
            target,
            frame_width=frame_width,
            frame_height=frame_height,
            entry_width=entry_width,
            entry_height=entry_height,
        )
        if crop is not None:
            crops.append(crop)
    if len(crops) < 8:
        return None
    return np.asarray(crops, dtype=np.float32)


def _audio_mfcc(audio_samples: list[float] | None, sample_rate: int | None, *, duration_s: float):
    import numpy as np
    import python_speech_features

    if not audio_samples or not sample_rate:
        return None
    sample_count = int(round(duration_s * float(sample_rate)))
    if sample_count <= 0 or len(audio_samples) < min(sample_count, int(sample_rate * 0.6)):
        return None
    audio = np.asarray(audio_samples[-sample_count:], dtype=np.float32)
    if audio.size < int(sample_rate * 0.5):
        return None
    return python_speech_features.mfcc(audio, int(sample_rate), numcep=13, winlen=0.025, winstep=0.010)


def _durations() -> list[int]:
    raw = (os.environ.get("LOCATOR_TALKNET_DURATIONS") or "1").strip()
    durations = []
    for item in raw.split(","):
        try:
            value = int(item.strip())
        except Exception:
            continue
        if 1 <= value <= 6:
            durations.append(value)
    return durations or [1]


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, value))))


def score_face(
    *,
    target: FaceTarget,
    audio: Any = None,
    frame_width: int | None = None,
    frame_height: int | None = None,
    frames: list[tuple[float, bytes, int, int]] | None = None,
    audio_samples: list[float] | None = None,
    sample_rate: int | None = None,
    device: str = "cuda",
) -> float:
    now = time.time()
    cached = _SCORE_CACHE.get(int(target.face_id))
    if cached is not None and now - cached[0] < 0.35:
        return cached[1]
    if device != "cuda":
        raise RuntimeError("TalkNet adapter currently requires CUDA")
    model = _load_model()
    window_s = max(0.8, min(3.0, float(os.environ.get("LOCATOR_TALKNET_WINDOW_S", "2.0"))))
    if not frame_width or not frame_height:
        raise RuntimeError("TalkNet needs frame_width and frame_height")
    video_feature = _resample_faces(
        frames or [],
        target,
        frame_width=int(frame_width),
        frame_height=int(frame_height),
        duration_s=window_s,
    )
    audio_feature = _audio_mfcc(audio_samples, sample_rate, duration_s=window_s)
    if video_feature is None or audio_feature is None:
        raise RuntimeError("TalkNet needs recent face frames and audio samples")
    length = min((audio_feature.shape[0] - audio_feature.shape[0] % 4) / 100.0, video_feature.shape[0] / 25.0)
    if length < 0.5:
        raise RuntimeError("TalkNet window is too short")
    import numpy as np
    import torch

    audio_feature = audio_feature[: int(round(length * 100)), :]
    video_feature = video_feature[: int(round(length * 25)), :, :]
    logits = []
    with torch.no_grad():
        for duration in _durations():
            batch_size = int(math.ceil(length / duration))
            for index in range(batch_size):
                input_a = torch.FloatTensor(
                    audio_feature[index * duration * 100 : (index + 1) * duration * 100, :]
                ).unsqueeze(0).cuda()
                input_v = torch.FloatTensor(
                    video_feature[index * duration * 25 : (index + 1) * duration * 25, :, :]
                ).unsqueeze(0).cuda()
                if input_a.shape[1] < 40 or input_v.shape[1] < 8:
                    continue
                embed_a = model.model.forward_audio_frontend(input_a)
                embed_v = model.model.forward_visual_frontend(input_v)
                embed_a, embed_v = model.model.forward_cross_attention(embed_a, embed_v)
                out = model.model.forward_audio_visual_backend(embed_a, embed_v)
                score = model.lossAV.forward(out, labels=None)
                logits.extend([float(value) for value in score])
    if not logits:
        raise RuntimeError("TalkNet produced no score")
    probability = _sigmoid(float(np.mean(np.asarray(logits, dtype=np.float32))))
    _SCORE_CACHE[int(target.face_id)] = (now, probability)
    return probability
