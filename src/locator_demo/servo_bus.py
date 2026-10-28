from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
import threading
import re
from collections import deque
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


def is_microphone_port(port: SerialPortCandidate) -> bool:
    text = f"{port.description} {port.hardware_id}".casefold()
    return "ydpi4mic" in text or "ef09:459d" in text or "vid_ef09&pid_459d" in text


def choose_servo_port(preferred: str | None = None) -> str | None:
    ports = [port for port in list_serial_ports() if not is_microphone_port(port)]
    if preferred and any(port.device.upper() == preferred.upper() for port in ports):
        return preferred
    for port in ports:
        text = f"{port.description} {port.hardware_id}".casefold()
        if any(marker in text for marker in ("ch340", "1a86:7523", "cp210", "10c4:ea60", "ftdi", "0403:6001")):
            return port.device
    return None


def servo_port_candidates(ports=None):
    """USB serial adapters only; audio composite ports never enter probing."""
    return [p for p in (list_serial_ports() if ports is None else ports)
            if not is_microphone_port(p) and any(marker in
                f'{p.description} {p.hardware_id}'.casefold() for marker in
                ('ch340','1a86:7523','vid_1a86&pid_7523','cp210','10c4:ea60','ftdi','0403:6001'))]


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
        self._io_lock = threading.RLock()
        self.diagnostic_sink=None
        self._transactions=deque(maxlen=30)
        self._position_reads={}
        self._request_sequence=0
        self._session_generation=0
        self._guarded_session=None

    def _record(self,event,detail,*,error=False):
        if self.diagnostic_sink:
            try:self.diagnostic_sink(event,detail,error=error)
            except Exception:pass

    def diagnostics(self):
        with self._io_lock:
            return dict(port=self.port,baudrate=self.baudrate,connected=self.connected,
                        armed_ids=sorted(self._armed_ids),
                        session_generation=self._session_generation,
                        positions={str(k):dict(v) for k,v in self._position_reads.items()},
                        transactions=list(self._transactions))

    @property
    def connected(self) -> bool:
        return bool(self._serial is not None and getattr(self._serial, "is_open", False))

    def motion_session(self):
        """Nonblocking binding hint; the guarded write validates it atomically.

        Head construction may run while the runtime holds its intent lock.
        Never wait for serial IO here. A racing snapshot can only cause a
        conservative rejection when guard_motion_session checks it later.
        """
        return self._session_generation if self.connected else None

    def _require_motion_session(self, expected):
        if expected is None or expected!=self._session_generation or not self.connected:
            raise RuntimeError('Serial session changed or disconnected; fresh position feedback is required before motion')

    @contextmanager
    def guard_motion_session(self, expected):
        """Pin check and one motion transaction to the same serial session."""
        with self._io_lock:
            self._require_motion_session(expected)
            previous=self._guarded_session
            self._guarded_session=expected
            try:
                yield
                self._require_motion_session(expected)
            finally:
                self._guarded_session=previous

    def connect(self):
        with self._io_lock:
            # A trajectory cannot turn an unavailable handle into a new live
            # session behind the caller's fresh-position synchronization.
            if self._guarded_session is not None:
                self._require_motion_session(self._guarded_session)
            if self.connected:
                return self._serial
            # Activation is evidence about one open serial session. A closed
            # handle (including an implicit reopen) cannot carry that evidence
            # into the new session, even if opening the new handle fails.
            previous_armed=sorted(self._armed_ids)
            self._armed_ids.clear()
            if previous_armed:
                self._record('serial_arm_cache_cleared',dict(port=self.port,ids=previous_armed,
                    reason='serial connection unavailable; old session state discarded'))
            if not self.port:
                raise RuntimeError("No identified servo port; select or connect the servo controller")
            if any(port.device.upper() == self.port.upper() and is_microphone_port(port) for port in list_serial_ports()):
                raise RuntimeError(f"{self.port} belongs to the microphone board, not a servo controller")
            self._serial = self.serial_factory(self.port, baudrate=self.baudrate, timeout=self.timeout_s, write_timeout=0.5)
            self._session_generation+=1
            self._record('serial_connected',dict(port=self.port,baudrate=self.baudrate,session_generation=self._session_generation))
            try:
                self._serial.reset_input_buffer()
                self._serial.reset_output_buffer()
            except Exception:
                pass
            return self._serial

    def close(self) -> None:
        with self._io_lock:
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
            self._serial = None
            self._armed_ids.clear()

    def _write(self, command: str) -> None:
        with self._io_lock:
            serial_obj = self.connect()
            try:
                data=command.encode('ascii');count=serial_obj.write(data)
                if count is not None and count!=len(data):raise IOError(f'Partial serial write: {count}/{len(data)} bytes')
                serial_obj.flush()
                self._record('serial_tx',dict(port=self.port,command=command))
            except Exception as exc:
                self._record('serial_write_error',dict(port=self.port,command=command,error=str(exc)),error=True)
                raise

    def request(self, command: str, *, timeout_s: float = 0.8, expected=None) -> str:
        with self._io_lock:
            start=time.monotonic();self._request_sequence+=1;raw=bytearray();packet=bytearray();selected='';failure=''
            try:
                serial_obj=self.connect();serial_obj.reset_input_buffer();self._write(command)
                deadline=time.monotonic()+timeout_s
                while time.monotonic()<deadline:
                    data=serial_obj.read(1)
                    if not data:continue
                    raw.extend(data);packet.extend(data)
                    if data==b'!':
                        text=packet.decode('ascii',errors='replace');packet.clear()
                        # Ignore command echoes/unrelated frames until the correct
                        # fresh reply arrives, rather than flushing it next time.
                        if expected is None or expected(text):selected=text;break
                    if len(raw)>=4096:break
            except Exception as exc:
                failure=str(exc);raise
            finally:
                row=dict(sequence=self._request_sequence,ts=time.time(),port=self.port,command=command,
                         raw=raw.decode('ascii',errors='replace')[:2048],response=selected,
                         received_bytes=len(raw),elapsed_ms=round((time.monotonic()-start)*1000,2),
                         outcome='io_error' if failure else 'reply' if selected else 'unexpected_reply' if raw else 'no_reply',error=failure)
                self._transactions.append(row);self._record('serial_request',row,error=bool(failure))
            return selected or raw.decode('ascii',errors='replace')

    @staticmethod
    def parse_position(response,servo_id):
        for match in re.finditer(r'#(\d{1,3})P(\d{1,5})!',response):
            if int(match.group(1))==int(servo_id):return int(match.group(2))
        return None

    def reconnect_readonly(self):
        """Only the stopped-motion caller may recover a session; never arm."""
        with self._io_lock:
            self._record('serial_reconnect',dict(port=self.port,reason='stopped motion; fresh feedback required'))
            self.close();self.connect()

    def set_mode_1(self, servo_id: int) -> str:
        self._write(f"#{servo_id:03d}PMOD1!")
        return ""

    def restore_torque(self, servo_id: int) -> None:
        self._write(f"#{servo_id:03d}PULR!")
        time.sleep(0.25)

    def move(self, servo_id: int, position: int, time_ms: int, *, force_arm: bool = False) -> None:
        with self._io_lock:
            self.arm(servo_id, force=force_arm)
            self._write(f"#{int(servo_id):03d}P{int(position):04d}T{int(time_ms):04d}!")

    def arm(self, servo_id: int, *, force: bool = False) -> None:
        with self._io_lock:
            servo_id = int(servo_id)
            # Check the handle before the cache: otherwise a cached axis can
            # skip arming and the following position write opens a new session.
            self.connect()
            if force or servo_id not in self._armed_ids:
                self.restore_torque(servo_id)
                # Mode is part of the assembled rig's coordinate calibration.
                # Never rewrite it implicitly while enabling an axis.
                self._armed_ids.add(servo_id)

    def read_mode(self, servo_id: int) -> int | None:
        def parse(text):
            # Actual assembled hardware replies #000MOD1!; some firmware
            # variants include P. Both still require a complete addressed frame.
            for match in re.finditer(r'#(\d{1,3})P?MOD(\d+)!', text):
                if int(match.group(1)) == int(servo_id):return int(match.group(2))
            return None
        response=self.request(f'#{int(servo_id):03d}PMOD!',timeout_s=.3,
                              expected=lambda text:parse(text) is not None)
        return parse(response)

    def activate_axis(self, axis, *, expected_position=None):
        """Verify coordinates, enable torque, and verify again before any goal."""
        with self._io_lock:
            sid=int(axis.servo_id);before=after=mode=None
            try:
                before=self.read_position(sid)
                if before is None:raise RuntimeError(f'Servo {sid} has no position feedback; motion remains stopped')
                if not axis.minimum <= before <= axis.maximum:
                    raise RuntimeError(f'Servo {sid} is outside configured limits; calibrate before motion')
                if expected_position is not None and abs(before-expected_position)>12:
                    raise RuntimeError(f'Servo {sid} position changed before activation; fresh position synchronization required')
                mode=self.read_mode(sid)
                if mode != 1:
                    raise RuntimeError(f'Servo {sid} mode is {mode if mode is not None else "unknown"}; expected 1; automatic mode rewriting is disabled')
                self.arm(sid)
                after=self.read_position(sid)
                if after is None:raise RuntimeError(f'Servo {sid} has no position feedback; motion remains stopped')
                if not axis.minimum <= after <= axis.maximum or abs(after-before)>12:
                    raise RuntimeError(f'Servo {sid} moved unexpectedly during activation ({before} to {after}); motion remains stopped')
                self._record('servo_activation_verified',dict(servo_id=sid,mode=mode,before=before,after=after,
                    mode_changed=False,position_command_sent=False))
                return after
            except Exception as exc:
                self._armed_ids.discard(sid)
                self._record('servo_activation_error',dict(servo_id=sid,mode=mode,before=before,after=after,
                    error=str(exc),mode_changed=False,position_command_sent=False),error=True)
                raise

    def move_many(self, positions: list[tuple[int, int]], time_ms: int) -> None:
        with self._io_lock:
            for servo_id, _position in positions:
                self.arm(servo_id)
            self._write("".join(f"#{servo_id:03d}P{position:04d}T{int(time_ms):04d}!" for servo_id, position in positions))

    def move_position_only(self, servo_id: int, position: int, time_ms: int) -> None:
        self._write(f"#{int(servo_id):03d}P{int(position):04d}T{int(time_ms):04d}!")

    def read_position(self, servo_id: int) -> int | None:
        with self._io_lock:
            responses=[];value=None;error=''
            for attempt in range(1,4):
                try:response=self.read_position_response(servo_id,timeout_s=.25)
                except Exception as exc:response='';error=str(exc)
                responses.append(response);value=self.parse_position(response,servo_id)
                if value is not None:break
                if attempt<3:time.sleep(.025)
            row=dict(servo_id=int(servo_id),position=value,ts=time.time(),attempts=attempt,
                     raw=responses[-1],responses=responses,error=error,
                     outcome='valid' if value is not None else 'io_error' if error else 'no_reply' if not any(responses) else 'invalid_reply')
            self._position_reads[int(servo_id)]=row
            self._record('servo_position_read',dict(port=self.port,**row),error=value is None)
            return value

    def read_position_response(self, servo_id: int, *,timeout_s=.8) -> str:
        return self.request(f"#{int(servo_id):03d}PRAD!",timeout_s=timeout_s,
                            expected=lambda text:self.parse_position(text,servo_id) is not None)

    def probe(self, servo_id: int) -> bool:
        reply = f"#{servo_id:03d}P!"
        return self.request(f"#{servo_id:03d}PID!", timeout_s=0.5, expected=lambda text: text == reply) == reply

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
        self._verified_axes = {}

    def _activate(self, axis, expected=None):
        if isinstance(self.bus,ZPServoBus):
            if self.bus._guarded_session is not None:
                self.bus._require_motion_session(self.bus._guarded_session)
            session=self.bus.motion_session()
            if session is not None and self._verified_axes.get(axis.servo_id)==session:
                return expected
            position=self.bus.activate_axis(axis,expected_position=expected)
            self._verified_axes[axis.servo_id]=self.bus.motion_session()
            return position
        if callable(getattr(self.bus,'arm',None)):self.bus.arm(axis.servo_id)
        return expected

    def prepare(self):
        with self.bus._io_lock:
            for axis in (self.yaw, self.pitch):
                if axis is not None:
                    self._activate(axis)

    def prepare_motion(self, current, target):
        values={'yaw':current.yaw,'pitch':current.pitch}
        with self.bus._io_lock:
            for name,axis in (('yaw',self.yaw),('pitch',self.pitch)):
                if axis is not None and getattr(target,name)!=getattr(current,name):
                    actual=self._activate(axis,getattr(current,name))
                    if actual is not None:values[name]=actual
        return HeadPose(**values)

    def move_to(self, pose: HeadPose, *, time_ms: int = 1000, current: HeadPose | None = None) -> None:
        if self.yaw is not None:
            yaw = max(self.yaw.minimum, min(self.yaw.maximum, int(pose.yaw)))
            if current is None or int(current.yaw) != yaw:
                self._activate(self.yaw, current.yaw if current is not None else None)
                self.bus.move(self.yaw.servo_id, yaw, time_ms)
        if self.pitch is not None:
            pitch = max(self.pitch.minimum, min(self.pitch.maximum, int(pose.pitch)))
            if current is None or int(current.pitch) != pitch:
                self._activate(self.pitch, current.pitch if current is not None else None)
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

    def move_stream(self, pose: HeadPose, *, time_ms: int, current: HeadPose):
        positions = []
        for axis, value, previous in ((self.yaw, pose.yaw, current.yaw), (self.pitch, pose.pitch, current.pitch)):
            if axis is not None:
                target = max(axis.minimum, min(axis.maximum, int(value)))
                if target != previous:
                    self._activate(axis,previous)
                    positions.append((axis.servo_id, target))
        if positions:
            self.bus.move_many(positions, time_ms)
