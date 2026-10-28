"""Progressive, feedback-monitored tests of the installed head, with camera evidence.

This tool moves real hardware. It never scans beyond the configured soft limits,
never seeks a mechanical end stop, and never returns blindly after a fault.
Create OUTPUT/STOP to interrupt. Automatic audio/visual tracking is not started.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sys
import threading
import time

import cv2
import numpy as np

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from locator_demo.capture import StereoCameraCapture
from locator_demo.head import AxisConfig, HeadPose
from locator_demo.motion import SmoothHeadHardware
from locator_demo.servo_bus import ZPServoBus, TwoAxisHeadHardware, list_serial_ports
from locator_demo.settings import ControlConfig
from probe_head_direction import flow


class ObservedBus(ZPServoBus):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent = []

    def _write(self, command):
        with self._io_lock:
            before = time.perf_counter()
            super()._write(command)
            if "T" in command:
                self.sent.append({"t": before, "write_ms": (time.perf_counter() - before) * 1000,
                                  "command": command})


class CameraRecord:
    def __init__(self, output):
        self.camera = StereoCameraCapture()
        self.output = output
        self.stop_event = threading.Event()
        self.times = []
        self.error = None

    def start(self):
        self.camera.start()
        deadline = time.perf_counter() + 12
        while self.camera.latest() is None and time.perf_counter() < deadline:
            time.sleep(.05)
        self.image()
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def image(self):
        sample = self.camera.latest()
        if sample is None or time.perf_counter() - sample.captured_monotonic > .3:
            raise RuntimeError("Camera has no fresh image")
        raw = cv2.imdecode(np.frombuffer(sample.jpeg, np.uint8), cv2.IMREAD_COLOR)
        eyes = [cv2.rotate(eye, cv2.ROTATE_180) for eye in np.hsplit(raw, 2)]
        return np.concatenate(eyes, axis=1)

    def _run(self):
        writer = cv2.VideoWriter(str(self.output / "motion-preview.avi"),
                                 cv2.VideoWriter_fourcc(*"MJPG"), 25, (960, 270))
        previous = -1
        try:
            if not writer.isOpened():
                raise RuntimeError("Could not open motion video writer")
            while not self.stop_event.is_set():
                s = self.camera.latest()
                if s is not None and s.sequence != previous:
                    image = cv2.resize(self.image(), (960, 270))
                    writer.write(image)
                    self.times.append({"t": s.captured_monotonic, "sequence": s.sequence})
                    previous = s.sequence
                self.stop_event.wait(.038)
        except Exception as exc:
            self.error = repr(exc)
        finally:
            writer.release()

    def stop(self):
        self.stop_event.set()
        if hasattr(self, "worker"):
            self.worker.join(timeout=3)
        metrics = self.camera.metrics()
        self.camera.stop()
        return {"capture": metrics, "video_frames": len(self.times), "error": self.error,
                "video_timing": "25 fps playback preview; acquisition timestamps saved separately"}


def waypoints(mode, axis, extent, initial):
    if mode == "directions":
        values = []
        for name in ("yaw", "pitch"):
            for delta in (40, 0, -40, 0):
                pose = asdict(initial)
                pose[name] += delta
                values.append((f"{name}{delta:+d}", HeadPose(**pose), None))
        return values
    if mode == "range":
        values = []
        for target in (1500 + extent, 1500, 1500 - extent, 1500):
            pose = asdict(initial)
            pose[axis] = target
            values.append((f"{axis}-{target}", HeadPose(**pose), None))
        values.append(("return-initial", initial, None))
        return values
    # Includes a reversal before arrival, exercising continuous replanning.
    return [("diagonal-a", HeadPose(1700, 1400), None),
            ("diagonal-b", HeadPose(1300, 1600), None),
            ("reverse-start", HeadPose(1700, 1400), .9),
            ("reverse-mid-flight", HeadPose(1350, 1575), None),
            ("return-initial", initial, None)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("directions", "range", "smooth"), required=True)
    parser.add_argument("--axis", choices=("yaw", "pitch"), default="yaw")
    parser.add_argument("--extent", type=int, choices=(100, 200, 300), default=100)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    settings = json.loads((PROJECT / "locator_demo_settings.json").read_text(encoding="utf-8"))
    limits = settings["axis_limits"]
    axes = {name: AxisConfig(servo_id=settings[f"{name}_id"], center=limits[f"{name}_center"],
                            minimum=limits[f"{name}_min"], maximum=limits[f"{name}_max"])
            for name in ("yaw", "pitch")}
    bus = ObservedBus(settings["port"])
    head = TwoAxisHeadHardware(bus, **axes)
    config = ControlConfig(**settings["control"])
    recording = CameraRecord(args.output)
    report = {"started": datetime.now().isoformat(), "arguments": vars(args) | {"output": str(args.output)},
              "automatic_tracking": False, "units": "servo position units, not calibrated degrees",
              "mechanical_end_stops_sought": False, "ports": [asdict(p) for p in list_serial_ports()],
              "axes": {k: asdict(v) for k, v in axes.items()}, "control": asdict(config),
              "feedback": [], "segments": [], "initial_reads": []}
    motion = None
    latest_feedback = None
    latest_time = -1e10

    def read_pose():
        nonlocal latest_feedback, latest_time
        start = time.perf_counter()
        status = head.read_status()
        values = (status.yaw_position, status.pitch_position)
        if any(v is None for v in values):
            raise RuntimeError(f"Position feedback missing: {values}")
        for (name, axis), value in zip(axes.items(), values):
            if not axis.minimum - 10 <= value <= axis.maximum + 10:
                raise RuntimeError(f"{name} feedback {value} outside configured limits")
        latest_feedback, latest_time = HeadPose(*values), time.perf_counter()
        report["feedback"].append({"t": latest_time, **asdict(latest_feedback),
                                   "read_ms": (latest_time - start) * 1000,
                                   "planned": asdict(motion.estimated_pose) if motion else None})
        return latest_feedback

    try:
        for _ in range(3):
            report["initial_reads"].append(asdict(read_pose()))
            time.sleep(.1)
        initial = latest_feedback
        if max(abs(r[k] - report["initial_reads"][0][k]) for r in report["initial_reads"]
               for k in ("yaw", "pitch")) > 8:
            raise RuntimeError("Initial pose is unstable")
        report["initial_pose"] = asdict(initial)
        targets = waypoints(args.mode, args.axis, args.extent, initial)
        for _, pose, _ in targets:
            for (name, axis), value in zip(axes.items(), (pose.yaw, pose.pitch)):
                if not axis.minimum <= value <= axis.maximum:
                    raise ValueError(f"Requested {name} {value} outside configured limits")
        recording.start()
        cv2.imwrite(str(args.output / "initial.jpg"), recording.image())
        head.prepare()
        armed = read_pose()
        if max(abs(armed.yaw - initial.yaw), abs(armed.pitch - initial.pitch)) > 12:
            raise RuntimeError("Pose changed unexpectedly during torque/mode preparation")
        motion = SmoothHeadHardware(head, initial=armed, config=config)
        for label, target, interrupt_after in targets:
            before = recording.image()[:, :1920]
            row = {"label": label, "target": asdict(target), "started": time.perf_counter(),
                   "feedback_before": asdict(read_pose()), "interrupted_by_next_target": interrupt_after is not None}
            report["segments"].append(row)
            motion.move_to(target, time_ms=300)
            deadline = time.perf_counter() + 12
            lag_since = None
            while True:
                if (args.output / "STOP").exists():
                    raise RuntimeError("STOP requested")
                actual = read_pose()
                estimated = motion.estimated_pose
                lag = max(abs(actual.yaw - estimated.yaw), abs(actual.pitch - estimated.pitch))
                if lag > 80:
                    lag_since = lag_since or time.perf_counter()
                    if time.perf_counter() - lag_since > .5:
                        raise RuntimeError(f"Persistent tracking error exceeds 80 units: {lag}")
                else:
                    lag_since = None
                recording.image()  # Also stop for stale/lost camera evidence.
                elapsed = time.perf_counter() - row["started"]
                if interrupt_after is not None and elapsed >= interrupt_after:
                    break
                if not motion.is_moving and motion.metrics()["prepared"]:
                    time.sleep(.3)
                    actual = read_pose()
                    if max(abs(actual.yaw - target.yaw), abs(actual.pitch - target.pitch)) > 12:
                        raise RuntimeError(f"Settled position did not reach target {target}: {actual}")
                    break
                if time.perf_counter() > deadline or motion.metrics()["error"]:
                    raise RuntimeError(f"Motion timeout or worker error: {motion.metrics()}")
                time.sleep(.085)
            after = recording.image()[:, :1920]
            row.update(ended=time.perf_counter(), feedback_after=asdict(actual),
                       flow=flow(before, after), motion=motion.metrics())
            cv2.imwrite(str(args.output / f"{label}-before.jpg"), cv2.resize(before, (960, 540)))
            cv2.imwrite(str(args.output / f"{label}-after.jpg"), cv2.resize(after, (960, 540)))
            print(json.dumps({k: v for k, v in row.items() if k != "motion"}), flush=True)
        report["final_pose"] = asdict(read_pose())
        report["returned_initial_verified"] = max(abs(latest_feedback.yaw - initial.yaw),
                                                   abs(latest_feedback.pitch - initial.pitch)) <= 12
        report["passed"] = report["returned_initial_verified"]
    except BaseException as exc:
        report["error"] = repr(exc)
        report["passed"] = False
        if motion:
            motion.stop()
        # Only hold a fresh measured pose; no blind return sweep on failure.
        if latest_feedback is not None and time.perf_counter() - latest_time < .4:
            try:
                bus.move_many([(axes["yaw"].servo_id, latest_feedback.yaw),
                               (axes["pitch"].servo_id, latest_feedback.pitch)], 40)
                report["fault_hold_command"] = asdict(latest_feedback)
            except Exception as hold_error:
                report["hold_error"] = repr(hold_error)
        print(json.dumps({"error": report["error"]}), flush=True)
    finally:
        if motion:
            motion.stop()
            report["motion_final"] = motion.metrics()
        report["camera"] = recording.stop()
        report["position_commands"] = bus.sent
        bus.close()
        report["closed"] = {"serial": not bus.connected, "camera": recording.camera._process is None,
                             "worker": motion is None or not motion.metrics()["worker_active"]}
        (args.output / "video-timestamps.json").write_text(json.dumps(recording.times), encoding="utf-8")
        (args.output / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
