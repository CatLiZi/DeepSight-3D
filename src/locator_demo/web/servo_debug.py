"""Local, single-axis hardware bring-up UI; never enables autonomous tracking."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import threading
import time
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from ..head import AxisConfig, HeadPose
from ..motion import SmoothHeadHardware
from ..servo_bus import TwoAxisHeadHardware, ZPServoBus
from ..settings import ControlConfig


class DebugMoveRequest(BaseModel):
    axis: Literal["yaw", "pitch"]
    delta: int | None = Field(default=None, ge=-300, le=300)
    target: int | None = Field(default=None, ge=500, le=2500)
    return_origin: bool = False

    @model_validator(mode="after")
    def one_target(self):
        if sum((self.delta is not None, self.target is not None, self.return_origin)) != 1:
            raise ValueError("Choose one relative move, target or return")
        return self


class ObservationRequest(BaseModel):
    move_id: int
    direction: Literal["left", "right", "up", "down", "none", "unknown"]
    smoothness: Literal["smooth", "sticky", "shaking", "unknown"]
    note: str = Field(default="", max_length=500)


class ServoDebugSession:
    def __init__(self, settings_path, output, *, bus=None, clock=time.perf_counter):
        self.clock = clock
        self.settings = json.loads(Path(settings_path).read_text(encoding="utf-8"))
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=False)
        self.bus = bus or ZPServoBus(self.settings["port"])
        self.control = ControlConfig(**self.settings["control"])
        limits = self.settings["axis_limits"]
        self.axes = {name: AxisConfig(servo_id=self.settings[f"{name}_id"],
                    center=limits[f"{name}_center"], minimum=limits[f"{name}_min"], maximum=limits[f"{name}_max"])
                    for name in ("yaw", "pitch")}
        self.feedback = {name: {"position": None, "last_position": None, "received": None,
                               "raw": "", "valid": False, "fault": "", "origin": None}
                         for name in self.axes}
        self.moves = []
        self.trace = deque(maxlen=2400)
        self.events = deque(maxlen=100)
        self.motion = None
        self.active_axis = None
        self.active_move = None
        self.enabled = False
        self.last_heartbeat = -1e9
        self._operation = threading.RLock()
        self._data = threading.RLock()
        self._done = threading.Event()
        self._halt = threading.Event()
        self._worker = None

    def _event(self, kind, **values):
        event = {"at": datetime.now().isoformat(), "t": self.clock(), "kind": kind, **values}
        with self._data:
            self.events.append(event)
            with (self.output / "events.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _save(self):
        with self._data:
            result = {"scope": "User-operated single-axis tests; no autonomous tracking or mechanical end-stop search",
                      "settings": self.settings, "axes": {k: asdict(v) for k, v in self.axes.items()},
                      "moves": self.moves, "feedback": self.feedback, "trace": list(self.trace),
                      "events": list(self.events), "direction_saved_to_runtime": False}
            temp = self.output / "session.tmp"
            temp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.output / "session.json")

    def _read(self, name):
        axis = self.axes[name]
        raw = self.bus.request(f"#{axis.servo_id:03d}PRAD!", timeout_s=.22)
        prefix = f"#{axis.servo_id:03d}P"
        digits = raw[len(prefix):-1] if raw.startswith(prefix) and raw.endswith("!") else ""
        value = int(digits) if digits.isdigit() else None
        valid = value is not None and axis.minimum <= value <= axis.maximum
        with self._data:
            item = self.feedback[name]
            item.update(position=value if valid else None, raw=raw, valid=valid)
            if valid:
                item.update(last_position=value, received=self.clock())
        return value if valid else None

    def connect(self):
        with self._operation:
            self.stop("reconnect")
            self.bus.close()
            self.enabled = True
            for name in self.axes:
                try:
                    values = [self._read(name) for _ in range(3)]
                    valid = all(v is not None for v in values) and max(values) - min(values) <= 8
                    fault = "" if valid else "位置反馈缺失、超界或不稳定；请检查后重新读取"
                except Exception as exc:
                    valid, fault = False, str(exc)
                    with self._data:
                        self.feedback[name].update(valid=False, position=None)
                with self._data:
                    self.feedback[name]["fault"] = fault
                    if valid and self.feedback[name]["origin"] is None:
                        self.feedback[name]["origin"] = values[-1]
            self.last_heartbeat = self.clock()
            self._halt.clear()
            self._event("connect", feedback=self.state()["axes"])
            self._save()
        return self.state()

    def state(self):
        with self._data:
            now = self.clock()
            rows = {}
            for name, axis in self.axes.items():
                f = self.feedback[name]
                age = now - f["received"] if f["received"] is not None else None
                fresh = bool(f["valid"] and age is not None and age < 1.2)
                rows[name] = {**asdict(axis), **f, "age_s": age, "fresh": fresh,
                              "can_move": self.enabled and fresh and not f["fault"] and self.active_move is None}
            return {"port": self.settings["port"], "enabled": self.enabled, "axes": rows,
                    "active_axis": self.active_axis, "active_move": self.active_move,
                    "motion": self.motion.metrics() if self.motion else None,
                    "moves": list(self.moves), "trace": list(self.trace)[-240:],
                    "events": list(self.events)[-12:], "automatic_tracking": False,
                    "units": "舵机位置刻度，尚未换算为角度", "record_path": str(self.output / "session.json")}

    def move(self, request):
        with self._operation:
            if not self.enabled or self.active_move is not None:
                raise RuntimeError("请先读取位置，等待当前动作完成")
            name, axis = request.axis, self.axes[request.axis]
            if self.feedback[name]["fault"]:
                raise RuntimeError("该轴反馈异常，请先重新读取位置")
            initial = self._read(name)
            if initial is None:
                self.feedback[name]["fault"] = "本次位置读取失败，未发送运动指令"
                raise RuntimeError(self.feedback[name]["fault"])
            target = (self.feedback[name]["origin"] if request.return_origin else request.target)
            if request.delta is not None:
                target = initial + request.delta
            if target is None or not axis.minimum <= target <= axis.maximum:
                raise ValueError(f"目标需在 {axis.minimum}–{axis.maximum} 刻度内")
            if target == initial:
                raise ValueError("已经在该位置，无需运动")
            # Isolate the requested axis; a missing second servo cannot move it.
            head = TwoAxisHeadHardware(self.bus, yaw=axis if name == "yaw" else None,
                                       pitch=axis if name == "pitch" else None)
            head.prepare()
            armed = self._read(name)
            if armed is None or abs(armed - initial) > 12:
                self.feedback[name]["fault"] = "启用后反馈异常，未启动位置轨迹"
                raise RuntimeError(self.feedback[name]["fault"])
            if self._halt.is_set() or not self.enabled:
                raise RuntimeError("动作已被停止，未启动位置轨迹")
            if self.motion:
                self.motion.stop()
            start = HeadPose(armed if name == "yaw" else 1500, armed if name == "pitch" else 1500)
            end = HeadPose(target if name == "yaw" else 1500, target if name == "pitch" else 1500)
            self.motion = SmoothHeadHardware(head, initial=start, config=self.control)
            row = {"id": len(self.moves) + 1, "axis": name, "servo_id": axis.servo_id,
                   "from": armed, "target": target, "started": self.clock(), "status": "moving"}
            with self._data:
                if self._halt.is_set():
                    raise RuntimeError("动作已被停止")
                self.moves.append(row)
                self.active_axis, self.active_move = name, row
                self.last_heartbeat = self.clock()
                self.motion.move_to(end, time_ms=600)
            self._event("move", **row)
            self._save()
        return self.state()

    def stop(self, reason="user_stop"):
        self._halt.set()
        self.enabled = False
        with self._data:
            motion = self.motion
            if motion:
                motion._stop.set()
        if motion:
            # Signal before waiting for other serial I/O; no further samples.
            motion.stop()
        with self._data:
            if self.active_move is not None:
                self.active_move.update(status="stopped", reason=reason, ended=self.clock())
            self.active_move, self.active_axis = None, None
        self._event("stop", reason=reason)
        self._save()

    def observe(self, request):
        with self._operation, self._data:
            if not 1 <= request.move_id <= len(self.moves):
                raise ValueError("没有对应的动作记录")
            row = self.moves[request.move_id - 1]
            if row["status"] == "moving":
                raise ValueError("请等动作结束或先停止，再记录观察")
            row["observation"] = request.model_dump(exclude={"move_id"})
            self._event("user_observation", move_id=request.move_id, **row["observation"])
            self._save()
        return self.state()

    def start_monitor(self):
        self._worker = threading.Thread(target=self._monitor, daemon=True)
        self._worker.start()

    def _monitor(self):
        while not self._done.wait(.16):
            with self._operation:
                if not self.enabled:
                    continue
                if self.clock() - self.last_heartbeat > 2:
                    if self.active_move:
                        self.stop("网页失去连接，停止发令")
                    continue
                names = [self.active_axis] if self.active_axis else list(self.axes)
                for name in names:
                    try:
                        value = self._read(name)
                        if value is None:
                            self.feedback[name]["fault"] = "位置反馈中断，已停止；检查后重新读取"
                            if self.active_move:
                                self.stop(self.feedback[name]["fault"])
                            continue
                        planned = getattr(self.motion.estimated_pose, name) if self.active_axis == name else None
                        with self._data:
                            self.trace.append({"t": self.clock(), "axis": name, "position": value, "planned": planned})
                        row = self.active_move
                        if row and self.active_axis == name:
                            metrics = self.motion.metrics()
                            if metrics["error"]:
                                raise RuntimeError(metrics["error"])
                            if self.clock() - row["started"] > 15:
                                raise RuntimeError("运动超时")
                            if not self.motion.is_moving and self.clock() - row["started"] > .9:
                                if abs(value - row["target"]) > 12:
                                    raise RuntimeError(f"实际位置 {value} 未到目标 {row['target']}")
                                self.motion.stop()
                                row.update(status="completed", actual=value, ended=self.clock(), metrics=metrics)
                                self.active_axis, self.active_move = None, None
                                self._event("move_completed", **row)
                                self._save()
                    except Exception as exc:
                        self.feedback[name]["fault"] = str(exc)
                        if self.active_move:
                            self.stop(str(exc))

    def shutdown(self):
        self._done.set()
        self.stop("server_shutdown")
        if self._worker:
            self._worker.join(timeout=3)
        self.bus.close()


def create_servo_debug_app(settings_path: Path, output: Path):
    session = ServoDebugSession(settings_path, output)
    app = FastAPI(title="人头舵机实物调试")
    app.state.session = session
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.on_event("startup")
    def start():
        session.start_monitor()

    @app.on_event("shutdown")
    def shutdown():
        session.shutdown()

    @app.get("/", response_class=HTMLResponse)
    def root():
        return (static / "servo-debug.html").read_text(encoding="utf-8")

    @app.get("/api/debug/state")
    def state():
        session.last_heartbeat = session.clock()
        return session.state()

    async def action(fn, *args):
        try:
            return await asyncio.to_thread(fn, *args)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/debug/connect")
    async def connect():
        return await action(session.connect)

    @app.post("/api/debug/move")
    async def move(payload: DebugMoveRequest):
        return await action(session.move, payload)

    @app.post("/api/debug/stop")
    async def stop():
        # Set the stop flag immediately, even during a read or preparation.
        if session.motion:
            session.motion._stop.set()
        await action(session.stop)
        return session.state()

    @app.post("/api/debug/observation")
    async def observation(payload: ObservationRequest):
        return await action(session.observe, payload)

    @app.get("/api/debug/download")
    def download():
        session._save()
        return FileResponse(session.output / "session.json", filename="servo-test-record.json")

    return app
