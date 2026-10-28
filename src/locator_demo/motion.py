"""Bounded C2-continuous trajectories; time-based sampling independent of the UI."""
from __future__ import annotations

from dataclasses import asdict
from contextlib import nullcontext
import math
import threading
import time

import numpy as np
from numpy.polynomial import polynomial as poly

from .head import HeadPose, DEFAULT_YAW_MIN, DEFAULT_YAW_MAX


def quintic(start, velocity, acceleration, target, duration):
    delta = target - start
    t = duration
    return np.array([start, velocity, acceleration / 2,
                     (20 * delta - 12 * velocity * t - 3 * acceleration * t * t) / (2 * t ** 3),
                     (-30 * delta + 16 * velocity * t + 3 * acceleration * t * t) / (2 * t ** 4),
                     (12 * delta - 6 * velocity * t - acceleration * t * t) / (2 * t ** 5)])


def extrema(coefficients, duration):
    derivative = poly.polyder(coefficients)
    roots = poly.polyroots(derivative) if np.any(derivative) else []
    times = [0.0, duration] + [float(root.real) for root in roots
                             if abs(root.imag) < 1e-7 and 0 < root.real < duration]
    return np.array(poly.polyval(times, coefficients))


class SmoothMotionPlanner:
    def __init__(self, initial: HeadPose, *, limits=((DEFAULT_YAW_MIN, DEFAULT_YAW_MAX), (1200, 1800)),
                 max_velocity=180.0, max_acceleration=600.0, max_jerk=3000.0):
        self.limits = limits
        self.max_velocity, self.max_acceleration, self.max_jerk = max_velocity, max_acceleration, max_jerk
        self.reset(initial)

    def reset(self, pose):
        self._coefficients = [np.array([float(pose.yaw)]), np.array([float(pose.pitch)])]
        self.started = 0.0
        self.duration = 0.0
        self.target = pose

    def sample(self, now):
        elapsed = max(0.0, min(self.duration, now - self.started))
        rows = []
        for coefficients in self._coefficients:
            rows.append(tuple(float(poly.polyval(elapsed, poly.polyder(coefficients, order))) for order in (0, 1, 2)))
        return rows

    def retarget(self, target: HeadPose, now: float, minimum_duration=0.0):
        target = HeadPose(*(max(lo, min(hi, value)) for value, (lo, hi) in
                           zip((target.yaw, target.pitch), self.limits)))
        if target == self.target:
            return True
        states = self.sample(now)
        distance = max(abs(value - state[0]) for value, state in zip((target.yaw, target.pitch), states))
        duration = max(0.08, minimum_duration, 1.875 * distance / self.max_velocity,
                       math.sqrt(5.78 * distance / self.max_acceleration), (60 * distance / self.max_jerk) ** (1 / 3))
        for _attempt in range(32):
            candidates = [quintic(*state, value, duration) for state, value in zip(states, (target.yaw, target.pitch))]
            feasible = True
            for coefficients, (lo, hi) in zip(candidates, self.limits):
                positions = extrema(coefficients, duration)
                if positions.min() < lo - 1e-6 or positions.max() > hi + 1e-6:
                    feasible = False
                    break
                for order, limit in ((1, self.max_velocity), (2, self.max_acceleration), (3, self.max_jerk)):
                    if np.max(np.abs(extrema(poly.polyder(coefficients, order), duration))) > limit + 1e-6:
                        feasible = False
                        break
            if feasible:
                self._coefficients = candidates
                self.started, self.duration, self.target = now, duration, target
                return True
            duration *= 1.15
        # Preserve the already bounded segment if a reversal cannot be fitted
        # within the physical limits. The hardware worker retries as it slows.
        return False


class SmoothHeadHardware:
    def __init__(self, head, *, initial: HeadPose, config, clock=time.perf_counter, autostart=True):
        self.head, self.yaw, self.pitch, self.config = head, head.yaw, head.pitch, config
        self.clock, self.autostart = clock, autostart
        limits = tuple((axis.minimum, axis.maximum) if axis else (value, value)
                       for axis, value in zip((head.yaw, head.pitch), (initial.yaw, initial.pitch)))
        self.planner = SmoothMotionPlanner(initial, limits=limits, max_velocity=config.max_velocity,
                                            max_acceleration=config.max_acceleration, max_jerk=config.max_jerk)
        self._last_sent = initial
        self._goal = initial
        self._minimum_duration = 0.0
        self._pending = False
        self._commands = 0
        self._error = ""
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._worker = None
        self._prepared = not autostart
        bus=getattr(head,'bus',None)
        self._session_bus=bus if callable(getattr(bus,'motion_session',None)) and callable(getattr(bus,'guard_motion_session',None)) else None
        self._serial_session=self._session_bus.motion_session() if self._session_bus is not None else None

    def _session_guard(self):
        return self._session_bus.guard_motion_session(self._serial_session) if self._session_bus is not None else nullcontext()

    def _latch_error(self, exc):
        # Called under the motion lock; never reconnect, arm or replay a goal.
        self._error=str(exc)
        self._prepared=False
        self._goal=self._last_sent
        self.planner.reset(self._last_sent)
        self._pending=False
        self._stop.set()

    @property
    def estimated_pose(self):
        with self._lock:
            if not self._prepared:
                return self._last_sent
            values = self.planner.sample(self.clock())
            return HeadPose(round(values[0][0]), round(values[1][0]))

    @property
    def is_moving(self):
        with self._lock:
            return not self._error and (self._pending or self.clock() < self.planner.started + self.planner.duration)

    def move_to(self, pose, *, time_ms=1000, current=None):
        with self._lock:
            if self._error:
                raise RuntimeError('Motion is stopped after an IO failure; fresh position feedback is required: ' + self._error)
            self._goal = pose
            self._minimum_duration = max(0.0, time_ms / 1000)
            self._pending = not self._prepared or not self.planner.retarget(pose, self.clock(), self._minimum_duration)
            if self.autostart and (self._worker is None or not self._worker.is_alive()):
                self._stop.clear()
                self._worker = threading.Thread(target=self._run, daemon=True)
                self._worker.start()

    def step(self, now=None):
        now = self.clock() if now is None else now
        with self._lock:
            if self._error:
                raise RuntimeError('Motion is stopped after an IO failure; fresh position feedback is required: ' + self._error)
            try:
                if self._pending:
                    self._pending = not self.planner.retarget(self._goal, now, self._minimum_duration)
                values = self.planner.sample(now)
                pose = HeadPose(round(values[0][0]), round(values[1][0]))
                previous = self._last_sent
                if pose == previous:
                    return
                duration_ms = max(20, round(2000 / self.config.motion_hz))
                sender = getattr(self.head, "move_stream", self.head.move_to)
                with self._session_guard():
                    sender(pose, time_ms=duration_ms, current=previous)
                self._last_sent = pose
                self._commands += 1
            except Exception as exc:
                self._latch_error(exc)
                raise

    def _run(self):
        try:
            if self._stop.is_set():
                return
            # Arming is completed before the trajectory clock starts; its
            # protocol settling sleeps must not advance the planned movement.
            with self._session_guard():
                prepare_motion=getattr(self.head,'prepare_motion',None)
                measured=prepare_motion(self._last_sent,self._goal) if callable(prepare_motion) else None
                if not callable(prepare_motion):self.head.prepare()
            with self._lock:
                if self._stop.is_set():
                    return
                self._prepared = True
                if isinstance(measured,HeadPose):self._last_sent=measured
                self.planner.reset(self._last_sent)
                self._pending = not self.planner.retarget(self._goal, self.clock(), self._minimum_duration)
            interval = 1 / self.config.motion_hz
            deadline = self.clock()
            while not self._stop.is_set():
                self.step()
                deadline += interval
                now = self.clock()
                if deadline < now:
                    deadline = now + interval
                self._stop.wait(max(0.0, deadline - now))
        except Exception as exc:
            with self._lock:
                self._latch_error(exc)

    def cancel(self):
        with self._lock:
            # A request may be cancelled while prepare() is sleeping. The
            # worker may finish arming, but must not replay the old target.
            self._goal = self._last_sent
            self.planner.reset(self._last_sent)
            self._pending = False

    def stop(self):
        self._stop.set()
        worker = self._worker
        if worker and worker is not threading.current_thread():
            worker.join(timeout=3)
        self.cancel()

    def read_status(self):
        return self.head.read_status()

    def metrics(self):
        with self._lock:
            return {"enabled": True, "sample_hz": self.config.motion_hz,
                    "max_velocity": self.config.max_velocity, "max_acceleration": self.config.max_acceleration,
                    "max_jerk": self.config.max_jerk, "units": "servo position units, not degrees",
                    "trajectory": "quintic C2 continuous; bounded velocity, acceleration, jerk",
                    "estimated_pose": asdict(self.estimated_pose), "goal_pose": asdict(self._goal),
                    "segment_duration_s": round(self.planner.duration, 4), "position_commands": self._commands,
                    "moving": self.is_moving, "prepared": self._prepared,
                    "serial_session_bound": self._session_bus is not None,
                    "expected_serial_session": self._serial_session,
                    "worker_active": self._worker is not None and self._worker.is_alive(), "error": self._error}
