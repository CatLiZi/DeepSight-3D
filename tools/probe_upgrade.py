"""Validate selected native camera/audio streams without opening a servo port."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from locator_demo.capture import StereoCameraCapture, WasapiAudioCapture, jpeg_dimensions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--camera-only", action="store_true")
    parser.add_argument("--width", type=int, default=3840)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=float, default=60)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    camera = StereoCameraCapture(width=args.width, height=args.height, fps=args.fps)
    audio = None if args.camera_only else WasapiAudioCapture()
    report = {"started": datetime.now().isoformat(), "seconds": args.seconds, "servo_io": False, "snapshots": []}
    try:
        camera.start()
        if audio:
            audio.start()
        deadline = time.perf_counter() + 10
        while camera.latest() is None and time.perf_counter() < deadline:
            time.sleep(.02)
        if camera.latest() is None:
            raise RuntimeError(str(camera.metrics()))
        warm = time.perf_counter() + 1
        while time.perf_counter() < warm:
            time.sleep(.02)
        started = time.perf_counter()
        first_sequence = camera.latest().sequence
        first_audio_frame = audio.metrics()["sample_frames"] if audio else 0
        seen = {}
        next_snapshot = started
        while time.perf_counter() - started < args.seconds:
            sample = camera.latest()
            if sample and sample.sequence not in seen:
                seen[sample.sequence] = hashlib.sha256(sample.jpeg).hexdigest()
            if time.perf_counter() >= next_snapshot:
                report["snapshots"].append({"elapsed_s": round(time.perf_counter() - started, 3),
                                             "camera": camera.metrics(), "audio": audio.metrics() if audio else None})
                next_snapshot += 2
            time.sleep(.002)
        elapsed = time.perf_counter() - started
        sample = camera.latest()
        report["camera"] = camera.metrics()
        report["measurement"] = {"wall_s": elapsed, "frames_after_warmup": sample.sequence - first_sequence,
                                   "fps_after_warmup": (sample.sequence - first_sequence) / elapsed,
                                   "sampled_packet_count": len(seen), "sampled_distinct_sha256": len(set(seen.values()))}
        assert jpeg_dimensions(sample.jpeg) == (args.width, args.height)
        (args.output / "stereo-native.jpg").write_bytes(sample.jpeg)
        if audio:
            report["audio"] = audio.metrics()
            report["measurement"]["audio_frames_after_warmup"] = report["audio"]["sample_frames"] - first_audio_frame
            audio.save_recent(args.output / "microphone-4ch-48000.wav", seconds=3)
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        camera.stop()
        if audio:
            audio.stop()
        report["stopped"] = datetime.now().isoformat()
        (args.output / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(args.output / "validation.json"), "error": report.get("error"),
                      "measurement": report.get("measurement"), "camera": report.get("camera"),
                      "audio": report.get("audio")}, ensure_ascii=True), flush=True)
    if "error" in report:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
