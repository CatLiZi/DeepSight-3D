"""Native-pixel focus assistance and read-only camera capability discovery.

Sharpness measurements do not actuate an MF lens and are not autofocus.
Scores compare the same textured ROI; they are not calibrated optical quality.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

import cv2
import numpy as np


class CameraCapabilityProbe:
    def __init__(self):
        self._worker = None
        self._lock = threading.Lock()
        self._result = {"status": "not_checked", "hardware_writes": False}

    def start(self):
        with self._lock:
            if self._worker is not None:
                return
            self._result["status"] = "checking"
            self._worker = threading.Thread(target=self._run, daemon=True)
            self._worker.start()

    def _run(self):
        try:
            if os.name != "nt":
                raise RuntimeError("Windows camera control probe is unavailable on this platform")
            shell = shutil.which("pwsh") or shutil.which("powershell")
            script = Path(__file__).resolve().parents[2] / "tools/probe_focus_controls.ps1"
            if not shell or not script.is_file():
                raise RuntimeError("Read-only camera control probe is unavailable")
            run = subprocess.run([shell, "-NoProfile", "-File", str(script)],
                                 capture_output=True, timeout=15, encoding="utf-8-sig", errors="replace",
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if run.returncode:
                raise RuntimeError(run.stderr[-500:])
            result = json.loads(run.stdout)
            focus = next(p for p in result["controls"] if p["property"] == "Focus")
            result.update(status="available" if focus["supported"] else "unsupported",
                          optical_autofocus_available=bool(focus["supported"] and focus["auto_capable"]),
                          focus=focus)
        except Exception as exc:
            result = {"status": "unavailable", "error": str(exc), "hardware_writes": False}
        with self._lock:
            self._result = result

    def snapshot(self):
        with self._lock:
            return deepcopy(self._result)


def focus_sample(image, center=None):
    """Return a 640x360-or-smaller unscaled ROI and content-dependent metrics."""
    h, w = image.shape[:2]
    rw, rh = min(640, w), min(360, h)
    cx, cy = center or (w / 2, h / 2)
    x = max(0, min(w - rw, round(cx - rw / 2)))
    y = max(0, min(h - rh, round(cy - rh / 2)))
    roi = image[y:y + rh, x:x + rw].copy()
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    smooth = cv2.GaussianBlur(gray, (3, 3), .6)
    score = float(cv2.Laplacian(smooth, cv2.CV_32F).var())
    contrast = float(np.std(smooth))
    mean = float(gray.mean())
    reason = "low_texture" if contrast < 8 else "dark" if mean < 30 else "bright" if mean > 235 else "relative_measurement"
    return roi, {"roi": [x, y, rw, rh], "native_pixels": True, "score": round(score, 2),
                 "contrast": round(contrast, 2), "mean_luma": round(mean, 2), "reason": reason,
                 "black_fraction": round(float(np.mean(gray < 5)), 4),
                 "white_fraction": round(float(np.mean(gray > 250)), 4)}


class FocusAssist:
    def __init__(self, interval=.5):
        self.interval = interval
        self.last = -1e9
        self.metrics, self.previews, self.reference = {}, {}, {}

    def update(self, eyes, *, target=None, active_eye="left", targets=None, now=None):
        now = time.perf_counter() if now is None else now
        if now - self.last < self.interval:
            return
        self.last = now
        for name, image in eyes.items():
            eye_target = targets.get(name) if targets is not None else (target if name == active_eye else None)
            center = eye_target.center if eye_target is not None else None
            roi, metrics = focus_sample(image, center)
            old = self.reference.get(name)
            changed = (old is None or max(abs(a-b) for a,b in zip(old["roi"], metrics["roi"])) > 24
                       or abs(old["mean_luma"] - metrics["mean_luma"]) > 18)
            peak = metrics["score"] if changed else max(old["peak"], metrics["score"])
            self.reference[name] = {**metrics, "peak": peak}
            metrics.update(peak_same_roi=peak, roi_source="detected_face" if center else "image_center",
                           note="Relative sharpness aid; not optical autofocus or an absolute pass threshold")
            ok, jpeg = cv2.imencode(".jpg", roi, [cv2.IMWRITE_JPEG_QUALITY, 95])
            self.metrics[name] = metrics
            if ok:
                self.previews[name] = base64.b64encode(jpeg).decode("ascii")

