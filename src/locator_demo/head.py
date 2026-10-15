from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AxisConfig:
    servo_id: int = 2
    center: int = 1500
    minimum: int = 1200
    maximum: int = 1800
    step_per_unit_error: int = 200


@dataclass(frozen=True)
class HeadPose:
    yaw: int
    pitch: int


@dataclass(frozen=True)
class DirectionConfig:
    yaw_left_sign: int = -1
    pitch_up_sign: int = -1
    manual_step: int = 60

    def __post_init__(self) -> None:
        object.__setattr__(self, "yaw_left_sign", 1 if int(self.yaw_left_sign) >= 0 else -1)
        object.__setattr__(self, "pitch_up_sign", 1 if int(self.pitch_up_sign) >= 0 else -1)
        object.__setattr__(self, "manual_step", max(1, min(300, int(self.manual_step))))


class HeadControllerLogic:
    def __init__(
        self,
        *,
        yaw: AxisConfig | None = None,
        pitch: AxisConfig | None = None,
        direction: DirectionConfig | None = None,
        visual_deadband: float = 0.06,
    ):
        self.yaw = yaw or AxisConfig(servo_id=3)
        self.pitch = pitch or AxisConfig(servo_id=2)
        self.direction = direction or DirectionConfig()
        self.visual_deadband = float(visual_deadband)

    @property
    def center_pose(self) -> HeadPose:
        return HeadPose(self.yaw.center, self.pitch.center)

    def clamp_pose(self, pose: HeadPose) -> HeadPose:
        return HeadPose(
            yaw=max(self.yaw.minimum, min(self.yaw.maximum, int(pose.yaw))),
            pitch=max(self.pitch.minimum, min(self.pitch.maximum, int(pose.pitch))),
        )

    def pose_for_audio_direction(self, direction: str) -> HeadPose:
        left = self.direction.yaw_left_sign
        targets = {
            "left": HeadPose(self.yaw.center + left * 200, self.pitch.center),
            "right": HeadPose(self.yaw.center - left * 200, self.pitch.center),
            "left_front": HeadPose(self.yaw.center + left * 150, self.pitch.center),
            "right_front": HeadPose(self.yaw.center - left * 150, self.pitch.center),
            "left_back": HeadPose(self.yaw.center + left * 300, self.pitch.center),
            "right_back": HeadPose(self.yaw.center - left * 300, self.pitch.center),
            "center": self.center_pose,
        }
        return self.clamp_pose(targets.get(str(direction), self.center_pose))

    def apply_audio_azimuth(
        self,
        current: HeadPose,
        azimuth_deg: float,
        *,
        deadband_deg: float = 10.0,
        max_yaw_delta: int = 100,
        min_yaw_delta: int = 30,
        full_scale_deg: float = 60.0,
        seek_min_yaw: int | None = None,
        seek_max_yaw: int | None = None,
    ) -> HeadPose:
        if abs(azimuth_deg) <= float(deadband_deg):
            return HeadPose(current.yaw, current.pitch)
        scale = min(1.0, abs(float(azimuth_deg)) / max(1.0, float(full_scale_deg)))
        step = max(int(min_yaw_delta), round(scale * int(max_yaw_delta)))
        direction = 1 if azimuth_deg > 0 else -1
        yaw = current.yaw + self.direction.yaw_left_sign * direction * step
        if seek_min_yaw is not None:
            yaw = max(int(seek_min_yaw), yaw)
        if seek_max_yaw is not None:
            yaw = min(int(seek_max_yaw), yaw)
        return self.clamp_pose(HeadPose(yaw, current.pitch))

    def apply_visual_error(
        self,
        current: HeadPose,
        error_x: float,
        error_y: float,
        *,
        yaw_deadband: float | None = None,
        pitch_deadband: float | None = None,
        max_yaw_delta: int | None = None,
        max_pitch_delta: int | None = None,
        min_yaw_delta: int | None = None,
        min_pitch_delta: int | None = None,
    ) -> HeadPose:
        yaw_band = self.visual_deadband if yaw_deadband is None else float(yaw_deadband)
        pitch_band = self.visual_deadband if pitch_deadband is None else float(pitch_deadband)
        yaw_delta = (
            0
            if abs(error_x) <= yaw_band
            else round(self.direction.yaw_left_sign * error_x * self.yaw.step_per_unit_error)
        )
        pitch_delta = (
            0
            if abs(error_y) <= pitch_band
            else round(-self.direction.pitch_up_sign * error_y * self.pitch.step_per_unit_error)
        )
        if yaw_delta and min_yaw_delta is not None:
            minimum = max(0, int(min_yaw_delta))
            if 0 < abs(yaw_delta) < minimum:
                yaw_delta = minimum if yaw_delta > 0 else -minimum
        if pitch_delta and min_pitch_delta is not None:
            minimum = max(0, int(min_pitch_delta))
            if 0 < abs(pitch_delta) < minimum:
                pitch_delta = minimum if pitch_delta > 0 else -minimum
        if max_yaw_delta is not None:
            limit = max(1, int(max_yaw_delta))
            yaw_delta = max(-limit, min(limit, yaw_delta))
        if max_pitch_delta is not None:
            limit = max(1, int(max_pitch_delta))
            pitch_delta = max(-limit, min(limit, pitch_delta))
        return self.clamp_pose(HeadPose(current.yaw + yaw_delta, current.pitch + pitch_delta))

    def jog(self, current: HeadPose, direction: str, amount: int | None = None) -> HeadPose:
        step = self.direction.manual_step if amount is None else max(1, min(300, int(amount)))
        name = str(direction).strip().lower()
        yaw_delta = 0
        pitch_delta = 0
        if name == "left":
            yaw_delta = self.direction.yaw_left_sign * step
        elif name == "right":
            yaw_delta = -self.direction.yaw_left_sign * step
        elif name == "up":
            pitch_delta = self.direction.pitch_up_sign * step
        elif name == "down":
            pitch_delta = -self.direction.pitch_up_sign * step
        else:
            raise ValueError("direction must be left, right, up, or down")
        return self.clamp_pose(HeadPose(current.yaw + yaw_delta, current.pitch + pitch_delta))
