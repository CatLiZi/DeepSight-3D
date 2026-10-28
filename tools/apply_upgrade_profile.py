"""Apply the user-selected acquisition profile, backing up local settings once."""
import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parent
settings_path = PROJECT / "locator_demo_settings.json"
backup = ROOT / "hardware_inventory/stage1/settings-before-upgrade.json"
if settings_path.exists() and not backup.exists():
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(settings_path.read_bytes())

for path in [settings_path, PROJECT / "locator_demo_settings.example.json"]:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data.setdefault("camera", {}).update(device_name="USB Video Device", video_size="3840x1080",
                                         crop_left_half=False, fps=None, output_width=1920,
                                         capture_backend="stereo_mjpeg", input_fps=60, usb_vid="1BCF", usb_pid="0B15")
    data.setdefault("audio_capture", {}).update(backend="wasapi", device_name="YDPI4MIC", channels=4,
                                  sample_rate=48000, sample_bits=16, block_ms=20, usb_vid="EF09", usb_pid="459D", wasapi_raw=True)
    data.setdefault("control", {}).update(smooth_motion=True, motion_hz=50, max_velocity=180.0,
                                          max_acceleration=600.0, max_jerk=3000.0)
    # Feature switches, directions, servo IDs and mechanical limits are retained.
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"backup": str(backup), "updated": [str(settings_path), "locator_demo_settings.example.json"]}))
