from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class DshowDevice:
    name: str
    kind: str
    alternative_name: str | None = None


_DEVICE_RE = re.compile(r'\]\s+"(?P<name>.+)"\s+\((?P<kind>video|audio)\)')
_ALT_RE = re.compile(r'\]\s+Alternative name "(?P<alt>.+)"')


def parse_dshow_devices(text: str) -> list[DshowDevice]:
    devices: list[DshowDevice] = []
    for line in text.splitlines():
        match = _DEVICE_RE.search(line)
        if match:
            devices.append(DshowDevice(name=match.group("name"), kind=match.group("kind")))
            continue
        alt_match = _ALT_RE.search(line)
        if alt_match and devices:
            previous = devices[-1]
            devices[-1] = DshowDevice(
                name=previous.name,
                kind=previous.kind,
                alternative_name=alt_match.group("alt"),
            )
    return devices


def list_dshow_devices(*, ffmpeg: str = "ffmpeg", timeout_s: float = 4.0) -> list[DshowDevice]:
    command = [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except Exception:
        return []
    return parse_dshow_devices((result.stderr or "") + "\n" + (result.stdout or ""))


def choose_dshow_device(
    kind: str,
    preferred_names: list[str | None],
    *,
    ffmpeg: str = "ffmpeg",
) -> DshowDevice | None:
    devices = [device for device in list_dshow_devices(ffmpeg=ffmpeg) if device.kind == kind]
    if not devices:
        return None

    normalized = [(name or "").casefold() for name in preferred_names if name]
    for wanted in normalized:
        for device in devices:
            if device.name.casefold() == wanted:
                return device
    for wanted in normalized:
        for device in devices:
            if wanted in device.name.casefold():
                return device
    return devices[0]
