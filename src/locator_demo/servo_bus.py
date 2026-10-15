from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable

from .head import AxisConfig, HeadPose


SerialFactory = Callable[..., object]


@dataclass(frozen=True)
class SerialPortCandidate:
    device: str
    description: str
    hardware_id: str


def list_serial_ports() -> list[SerialPortCandidate]:
    try:
        from serial.tools import list_ports
    except Exception:
        return []
    return [
        SerialPortCandidate(
            device=str(port.device),
            description=str(port.description or ""),
            hardware_id=str(port.hwid or ""),
        )
        for port in list_ports.comports()
    ]


def choose_servo_port(preferred: str | None = None) -> str:
    ports = list_serial_ports()
    if preferred and any(port.device.upper() == preferred.upper() for port in ports):
        return preferred
    for port in ports:
        text = f"{port.description} {port.hardware_id}".casefold()
        if "ch340" in text or "1a86:7523" in text or "usb-serial" in text:
            return port.device
    if ports:
        return ports[0].device
    return preferred or "COM14"


def _default_serial_factory(*args, **kwargs):
    import serial

    return serial.Serial(*args, **kwargs)


class ZPServoBus:
    def __init__(
        self,
        port: str = "COM14",
        baudrate: int = 115200,
        *,
        timeout_s: float = 0.05,
        serial_factory: SerialFactory | None = None,
    ):
        self.port = port
        self.baudrate = int(baudrate)
        self.timeout_s = float(timeout_s)
        self.serial_factory = serial_factory or _default_serial_factory
        self._serial = None
        self._armed_ids: set[int] = set()

    @property
    def connected(self) -> bool:
        return bool(self._serial is not None and getattr(self._serial, "is_open", False))

    def connect(self):
        if self.connected:
            return self._serial
        self._serial = self.serial_factory(self.port, baudrate=self.baudrate, timeout=self.timeout_s, write_timeout=0.5)
        try:
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
        except Exception:
            pass
        return self._serial

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
        self._serial = None
        self._armed_ids.clear()

    def _write(self, command: str) -> None:
        serial_obj = self.connect()
        try:
            serial_obj.reset_input_buffer()
        except Exception:
            pass
        serial_obj.write(command.encode("ascii"))
        serial_obj.flush()

    def request(self, command: str, *, timeout_s: float = 0.8) -> str:
        self._write(command)
        serial_obj = self.connect()
        deadline = time.time() + timeout_s
        chunks: list[bytes] = []
        while time.time() < deadline:
            data = serial_obj.read(1)
            if data:
                chunks.append(data)
                if data == b"!":
                    break
        return b"".join(chunks).decode("ascii", errors="replace")

    def set_mode_1(self, servo_id: int) -> str:
        self._write(f"#{servo_id:03d}PMOD1!")
        return ""

    def restore_torque(self, servo_id: int) -> None:
        self._write(f"#{servo_id:03d}PULR!")
        time.sleep(0.25)

    def move(self, servo_id: int, position: int, time_ms: int, *, force_arm: bool = False) -> None:
        servo_id = int(servo_id)
        if force_arm or servo_id not in self._armed_ids:
            self.restore_torque(servo_id)
            self.set_mode_1(servo_id)
            time.sleep(0.25)
            self._armed_ids.add(servo_id)
        self._write(f"#{servo_id:03d}P{int(position):04d}T{int(time_ms):04d}!")

    def move_position_only(self, servo_id: int, position: int, time_ms: int) -> None:
        self._write(f"#{int(servo_id):03d}P{int(position):04d}T{int(time_ms):04d}!")

    def read_position(self, servo_id: int) -> int | None:
        response = self.read_position_response(servo_id)
        prefix = f"#{servo_id:03d}P"
        if response.startswith(prefix) and response.endswith("!") and len(response) >= 10:
            value = response[len(prefix) : -1]
            if value.isdigit():
                return int(value)
        return None

    def read_position_response(self, servo_id: int) -> str:
        return self.request(f"#{int(servo_id):03d}PRAD!", timeout_s=0.8)

    def probe(self, servo_id: int) -> bool:
        return self.request(f"#{servo_id:03d}PID!", timeout_s=0.5) == f"#{servo_id:03d}P!"

    def scan_ids(self, ids: Iterable[int] = range(0, 254)) -> list[int]:
        found: list[int] = []
        for servo_id in ids:
            if self.probe(int(servo_id)):
                found.append(int(servo_id))
        return found


@dataclass
class HeadHardwareStatus:
    yaw_id: int | None
    pitch_id: int | None
    yaw_position: int | None
    pitch_position: int | None
    connected: bool


class TwoAxisHeadHardware:
    def __init__(
        self,
        bus: ZPServoBus,
        *,
        yaw: AxisConfig | None,
        pitch: AxisConfig | None,
        force_arm_pitch: bool = False,
    ):
        self.bus = bus
        self.yaw = yaw
        self.pitch = pitch
        self.force_arm_pitch = bool(force_arm_pitch)

    def move_to(self, pose: HeadPose, *, time_ms: int = 1000, current: HeadPose | None = None) -> None:
        if self.yaw is not None:
            yaw = max(self.yaw.minimum, min(self.yaw.maximum, int(pose.yaw)))
            if current is None or int(current.yaw) != yaw:
                self.bus.move(self.yaw.servo_id, yaw, time_ms)
        if self.pitch is not None:
            pitch = max(self.pitch.minimum, min(self.pitch.maximum, int(pose.pitch)))
            if current is None or int(current.pitch) != pitch:
                self.bus.move(self.pitch.servo_id, pitch, time_ms, force_arm=self.force_arm_pitch)

    def read_status(self) -> HeadHardwareStatus:
        yaw_position = self.bus.read_position(self.yaw.servo_id) if self.yaw is not None else None
        pitch_position = self.bus.read_position(self.pitch.servo_id) if self.pitch is not None else None
        return HeadHardwareStatus(
            yaw_id=self.yaw.servo_id if self.yaw is not None else None,
            pitch_id=self.pitch.servo_id if self.pitch is not None else None,
            yaw_position=yaw_position,
            pitch_position=pitch_position,
            connected=self.bus.connected,
        )
