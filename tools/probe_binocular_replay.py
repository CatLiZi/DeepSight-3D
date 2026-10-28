"""Replay a saved real SBS frame with synthetic eye masks, without device I/O."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time

import cv2
import numpy as np

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from locator_demo.settings import load_settings
from locator_demo.stereo_vision import StereoVisionTracker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument('--guided',action='store_true')
    parser.add_argument('--visual-priority',action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    image = cv2.imdecode(np.frombuffer(args.source.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    h, w = image.shape[:2]
    config = load_settings(PROJECT / "locator_demo_settings.json").camera
    tracker = StereoVisionTracker(video_size=f"{w}x{h}", output_width=w//2,
        left_rotation=config.left_rotation, right_rotation=config.right_rotation,
        face_detector_backend=config.face_detector_backend, scrfd_model_path=config.scrfd_model_path,
        scrfd_threshold=config.scrfd_threshold, multi_scale_search=config.multi_scale_search,
        focus_assist=False,audio_visual_guided=args.guided or args.visual_priority,visual_priority=args.visual_priority)
    tracker.capture.start = lambda: None
    tracker.capabilities.start = lambda: None
    original_metrics = tracker.capture.metrics
    tracker.capture.metrics = lambda: {**original_metrics(), "ok": True}
    records = []
    try:
        for kind, mask in (("original",None),("left_masked","left"),("right_masked","right"),("both_masked","both"),("restored",None)):
            pixels = image.copy()
            if mask in ("left","both"):
                pixels[:,:w//2] = 146
            if mask in ("right","both"):
                pixels[:,w//2:] = 146
            data = cv2.imencode(".jpg", pixels, [cv2.IMWRITE_JPEG_QUALITY,95])[1].tobytes()
            # At least three separate input sequences, with a time separation,
            # exercise handoff confirmation instead of reusing one observation.
            for i in range(4):
                tracker.capture._accept(data)
                seq = tracker.capture.latest().sequence
                tracker._start()
                deadline = time.perf_counter()+30
                while tracker.capture_metrics()["detection_sequence"] != seq and time.perf_counter()<deadline:
                    time.sleep(.01)
                if tracker.capture_metrics()["detection_sequence"] != seq:
                    raise RuntimeError(tracker._latest_error)
                time.sleep(.06)
            frame = tracker.read()
            records.append({"case":kind,"sequence":frame.detection_sequence,"control_eye":frame.control_eye,
                            "control_ready":frame.control_ready,"target":asdict(frame.target) if frame.target else None,
                            "eye_targets":{eye:[asdict(t) for t in targets] for eye,targets in frame.eye_targets.items()},
                            "binocular":frame.binocular,"capture":tracker.capture_metrics()})
        observed = {r["case"]:r for r in records}
        checks = {
            "original_both_eyes_detected": all(observed["original"]["eye_targets"].get(eye) for eye in ("left","right")),
            "left_mask_uses_right_face": observed["left_masked"]["control_eye"]=="right" and bool(observed["left_masked"]["target"]),
            "right_mask_uses_left_face": observed["right_masked"]["control_eye"]=="left" and bool(observed["right_masked"]["target"]),
            "both_mask_clear_target": observed["both_masked"]["target"] is None and not observed["both_masked"]["control_ready"],
            "restored_both_eyes_detected": all(observed["restored"]["eye_targets"].get(eye) for eye in ("left","right")),
        }
        report = {"hardware_io":False,"servo_io":False,"source":str(args.source.resolve()),
                  'guided_strategy':args.guided,
                  'visual_priority_strategy':args.visual_priority,
                  "source_sha256":hashlib.sha256(args.source.read_bytes()).hexdigest(),
                  "scope":"One real captured scene replayed with artificial flat masks; not physical occlusion, distance accuracy or a general face benchmark",
                  "checks":checks,"cases":records}
        (args.output/"validation.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({"checks":checks,"scope":report["scope"]}))
        return not all(checks.values())
    finally:
        tracker.stop()


if __name__ == "__main__":
    raise SystemExit(main())
