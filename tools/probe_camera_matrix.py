"""Camera-only MJPEG comparisons with IAMCameraControl settings restored in finally."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent


def controls(value=None, flags=None):
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(HERE / "camera_controls.ps1")]
    if value is not None:
        command += ["-Exposure", str(value), "-Flags", str(flags)]
    output = subprocess.run(command, capture_output=True, text=True, check=True, timeout=15)
    return json.loads(output.stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    original = controls()
    report = {"original": original, "tests": []}
    cases = [("manual-short-8", -8, 2, 3840, 1080, 60),
             ("manual-short-10", -10, 2, 3840, 1080, 60),
             ("auto-exposure", -6, 1, 3840, 1080, 60),
             ("720p-120", -8, 2, 2560, 720, 120)]
    try:
        for name, exposure, flags, width, height, fps in cases:
            current = controls(exposure, flags)
            destination = args.output / name
            command = [sys.executable, str(HERE / "probe_upgrade.py"), "--seconds", "6", "--camera-only",
                       "--width", str(width), "--height", str(height), "--fps", str(fps), "--output", str(destination)]
            process = subprocess.run(command, capture_output=True, text=True, timeout=40)
            validation = json.loads((destination / "validation.json").read_text(encoding="utf-8"))
            report["tests"].append({"name": name, "controls": current, "validation": validation})
            print(json.dumps({"case": name, "measurement": validation.get("measurement"), "error": validation.get("error")}), flush=True)
    finally:
        report["restored"] = controls(original["BeforeValue"], original["BeforeFlags"])
        (args.output / "matrix.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
