from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class FaceTarget:
    x1: float
    y1: float
    x2: float
    y2: float
    label: str = "person"
    score: float = 0.0
    face_id: int = 0
    face_yaw_deg: float = 0.0
    frontal_score: float = 1.0
    mouth_motion_score: float = 0.0
    mouth_audio_sync_score: float = 0.0
    active_speaker_score: float = 0.0
    face_height_ratio: float = 0.0
    near_candidate: bool = False
    asd_score: float = 0.0
    active_candidate: bool = False
    locked: bool = False
    specific_speaker: bool = False
    too_far: bool = False
    backend: str = "rules"
    tracking_x: float | None = None
    tracking_y: float | None = None
    tracking_source: str = ""

    @property
    def area(self) -> float:
        return max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    @property
    def tracking_center(self) -> tuple[float, float]:
        if self.tracking_x is not None and self.tracking_y is not None:
            return (float(self.tracking_x), float(self.tracking_y))
        return self.center

    @property
    def speaker_score(self) -> float:
        if self.active_speaker_score > 0:
            return self.active_speaker_score
        return max(0.0, min(1.0, self.score * max(0.0, self.frontal_score)))


@dataclass(frozen=True)
class BodyTarget:
    x1: float
    y1: float
    x2: float
    y2: float
    label: str = "body"
    score: float = 0.0
    visibility: float = 0.0

    @property
    def area(self) -> float:
        return max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)


def choose_primary_target(targets: Iterable[FaceTarget]) -> FaceTarget | None:
    target_list = list(targets)
    if not target_list:
        return None
    if not any(target.active_speaker_score > 0 for target in target_list):
        return max(target_list, key=lambda target: target.area)
    return max(target_list, key=lambda target: (target.speaker_score, target.area))


def choose_active_speaker_target(
    targets: Iterable[FaceTarget],
    *,
    audio_direction: str | None = None,
    frame_width: int | None = None,
    min_score: float = 0.35,
) -> FaceTarget | None:
    target_list = list(targets)
    if not target_list:
        return None
    scored: list[tuple[float, FaceTarget]] = []
    for target in target_list:
        score = target.speaker_score
        if audio_direction and frame_width and audio_direction in ("left", "right"):
            center_x, _ = target.tracking_center
            is_left_half = center_x < frame_width / 2.0
            direction_matches = (audio_direction == "left" and is_left_half) or (
                audio_direction == "right" and not is_left_half
            )
            score += 0.18 if direction_matches else -0.18
        scored.append((score, target))
    best_score, best_target = max(scored, key=lambda item: (item[0], item[1].area))
    if best_score >= min_score:
        return best_target
    fallback = max(
        target_list,
        key=lambda target: (float(getattr(target, "face_height_ratio", 0.0)), float(target.area), float(target.score)),
    )
    return fallback if fallback is not None else None


def normalized_center_error(
    target: FaceTarget | BodyTarget,
    *,
    frame_width: int,
    frame_height: int,
    target_offset_x_norm: float = 0.0,
    target_offset_y_norm: float = 0.0,
) -> tuple[float, float]:
    if frame_width <= 0 or frame_height <= 0:
        raise ValueError("frame dimensions must be positive")
    center_x, center_y = target.tracking_center if isinstance(target, FaceTarget) else target.center
    offset_x = max(-0.45, min(0.45, float(target_offset_x_norm)))
    offset_y = max(-0.45, min(0.45, float(target_offset_y_norm)))
    aim_x = frame_width * (0.5 + offset_x)
    aim_y = frame_height * (0.5 + offset_y)
    error_x = (center_x - aim_x) / (frame_width / 2.0)
    error_y = (center_y - aim_y) / (frame_height / 2.0)
    return (max(-1.0, min(1.0, error_x)), max(-1.0, min(1.0, error_y)))

