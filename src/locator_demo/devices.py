from __future__ import annotations

import re
import subprocess
import json
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class DshowDevice:
    name: str
    kind: str
    alternative_name: str | None = None

    @property
    def usb_identity(self) -> tuple[str, str] | None:
        return usb_identity(self.alternative_name or "")


def usb_identity(text: str) -> tuple[str, str] | None:
    match = re.search(r"(?:vid[_:=]?)([0-9a-f]{4}).*?(?:pid[_:=]?)([0-9a-f]{4})", text, re.I)
    if not match:
        match = re.search(r"(?:vid:pid=)([0-9a-f]{4}):([0-9a-f]{4})", text, re.I)
    return tuple(part.upper() for part in match.groups()) if match else None


def list_windows_usb_interfaces() -> list[dict]:
    """Read PnP identities; never opens an audio, video, or serial interface."""
    if sys.platform != "win32":
        return []
    command = ("Get-CimInstance Win32_PnPEntity | Where-Object {"
               "$_.PNPDeviceID -like 'USB*VID_*'} | "
               "Select-Object Name,PNPDeviceID,Status | ConvertTo-Json -Compress")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                             "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;" + command],
                            capture_output=True, encoding="utf-8-sig", errors="replace", timeout=10,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True)
    records = json.loads(result.stdout or "[]")
    return records if isinstance(records, list) else [records]


def list_wasapi_inputs(audio_module=None) -> list[dict]:
    if audio_module is None:
        import sounddevice as audio_module
    hosts = audio_module.query_hostapis()
    return [{"index": index, "name": device["name"], "host_api": hosts[device["hostapi"]]["name"],
             "max_input_channels": device["max_input_channels"], "default_samplerate": device["default_samplerate"]}
            for index, device in enumerate(audio_module.query_devices())
            if device["max_input_channels"] and "WASAPI" in hosts[device["hostapi"]]["name"].upper()]


def choose_wasapi_input(name: str, *, channels: int = 4, usb_vid: str | None = None,
                        usb_pid: str | None = None, audio_module=None, usb_interfaces=None) -> dict:
    if usb_vid and usb_pid:
        wanted = (usb_vid.upper(), usb_pid.upper())
        interfaces = list_windows_usb_interfaces() if usb_interfaces is None else usb_interfaces
        if not any(usb_identity(item.get("PNPDeviceID", "")) == wanted for item in interfaces):
            raise RuntimeError(f"USB microphone {usb_vid}:{usb_pid} is not present")
    matches = [device for device in list_wasapi_inputs(audio_module)
               if name.casefold() in device["name"].casefold() and device["max_input_channels"] >= channels]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one WASAPI input named {name!r} with {channels} channels; found {len(matches)}")
    return matches[0]


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
    usb_vid: str | None = None,
    usb_pid: str | None = None,
) -> DshowDevice | None:
    devices = [device for device in list_dshow_devices(ffmpeg=ffmpeg) if device.kind == kind]
    if usb_vid and usb_pid:
        devices = [device for device in devices if device.usb_identity == (usb_vid.upper(), usb_pid.upper())]
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
