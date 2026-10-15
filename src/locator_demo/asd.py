from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from typing import Any

from .audio import AudioDirection, AudioEstimate
from .vision import FaceTarget


@dataclass(frozen=True)
class AsdBackendStatus:
    requested: str
    actual: str
    available: bool
    fallback: bool = False
    message: str = ""
    device: str = "cpu"


class RuleBasedAsdBackend:
    name = "rules"

    def __init__(self, *, requested: str = "rules", fallback_message: str = ""):
        self._status = AsdBackendStatus(
            requested=requested,
            actual="rules",
            available=True,
            fallback=requested != "rules",
            message=fallback_message or "rule-based mouth motion backend",
            device="cpu",
        )

    @property
    def status(self) -> AsdBackendStatus:
        return self._status

    def score(
        self,
        target: FaceTarget,
        *,
        audio: AudioEstimate | None = None,
        frame_width: int | None = None,
        frame_height: int | None = None,
        visual_mirror_x: bool = False,
        frames: list[tuple[float, bytes, int, int]] | None = None,
        audio_samples: list[float] | None = None,
        sample_rate: int | None = None,
    ) -> float:
        mouth = max(float(target.mouth_motion_score), float(target.mouth_audio_sync_score))
        base_score = max(
            float(target.active_speaker_score),
            0.65 * mouth + 0.22 * float(target.frontal_score) + 0.08 * float(target.score),
        )
        if audio is not None and frame_width and audio.direction in (AudioDirection.LEFT, AudioDirection.RIGHT):
            center_x, _center_y = target.tracking_center
            is_left_half = center_x < frame_width / 2.0
            if visual_mirror_x:
                is_left_half = not is_left_half
            matches = (audio.direction == AudioDirection.LEFT and is_left_half) or (
                audio.direction == AudioDirection.RIGHT and not is_left_half
            )
            base_score += 0.06 if matches else -0.04
        return max(0.0, min(1.0, base_score))


class TalkNetAsdBackend:
    name = "talknet"

    def __init__(self):
        self._module: Any | None = None
        self._device = "cpu"
        self._available = False
        self._message = "TalkNet backend unavailable"
        try:
            import torch

            if torch.cuda.is_available():
                self._device = "cuda"
            module_name = (os.environ.get("LOCATOR_TALKNET_MODULE") or "locator_demo.talknet_adapter").strip()
            self._module = importlib.import_module(module_name)
            if not hasattr(self._module, "score_face"):
                self._message = f"{module_name} has no score_face(target, audio) function"
                self._module = None
                return
            is_available = getattr(self._module, "is_available", None)
            if callable(is_available):
                available = is_available()
                if isinstance(available, tuple):
                    ok, message = bool(available[0]), str(available[1])
                else:
                    ok, message = bool(available), ""
                if not ok:
                    self._message = message or f"{module_name} is not available"
                    self._module = None
                    return
            self._available = True
            self._message = f"TalkNet plugin loaded on {self._device}"
        except Exception as exc:
            self._message = f"TalkNet import failed: {type(exc).__name__}: {exc}"

    @property
    def status(self) -> AsdBackendStatus:
        return AsdBackendStatus(
            requested="talknet",
            actual="talknet" if self._available else "rules",
            available=self._available,
            fallback=not self._available,
            message=self._message,
            device=self._device,
        )

    def score(
        self,
        target: FaceTarget,
        *,
        audio: AudioEstimate | None = None,
        frame_width: int | None = None,
        frame_height: int | None = None,
        visual_mirror_x: bool = False,
        frames: list[tuple[float, bytes, int, int]] | None = None,
        audio_samples: list[float] | None = None,
        sample_rate: int | None = None,
    ) -> float:
        if self._module is None:
            raise RuntimeError(self._message)
        score_face = getattr(self._module, "score_face")
        value = score_face(
            target=target,
            audio=audio,
            frame_width=frame_width,
            frame_height=frame_height,
            frames=frames,
            audio_samples=audio_samples,
            sample_rate=sample_rate,
            device=self._device,
        )
        return max(0.0, min(1.0, float(value)))


def make_asd_backend(requested: str):
    requested_backend = str(requested or "rules").strip().lower()
    if requested_backend != "talknet":
        return RuleBasedAsdBackend()
    talknet = TalkNetAsdBackend()
    if talknet.status.available:
        return talknet
    return RuleBasedAsdBackend(requested="talknet", fallback_message=talknet.status.message)
