"""通过 sounddevice/WASAPI 录制 YDPI4MIC 的四声道 WAV，不使用 COM4。"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import sys
import wave

import sounddevice as sd


SAMPLE_RATE = 48000
CHANNELS = 4
SAMPLE_WIDTH = 2  # int16 每个样本 2 字节


def find_microphone(name: str = "YDPI4MIC") -> tuple[int, dict]:
    """按名称和 WASAPI 接口选择设备；设备编号随插拔变化，不写死为 12。"""
    apis = sd.query_hostapis()
    matches = []
    for index, device in enumerate(sd.query_devices()):
        api_name = apis[device["hostapi"]]["name"]
        if ("WASAPI" in api_name
                and name.casefold() in device["name"].casefold()
                and device["max_input_channels"] >= CHANNELS):
            matches.append((index, dict(device)))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one WASAPI input matching {name!r}; found {len(matches)}")
    return matches[0]


def record_wav(output: Path, seconds: float = 3, name: str = "YDPI4MIC") -> dict:
    """保存四路交错 PCM 音频；不代表已验证四路与物理咪头的对应关系。"""
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("seconds must be finite and positive")
    total_frames = round(seconds * SAMPLE_RATE)
    if total_frames < 1:
        raise ValueError("Duration is shorter than one audio frame")
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")

    index, device = find_microphone(name)
    # 共享模式已在本机验证。独占模式不是本样例的采集方式。
    settings = sd.WasapiSettings(exclusive=False)
    sd.check_input_settings(device=index, channels=CHANNELS, dtype="int16",
                            samplerate=SAMPLE_RATE, extra_settings=settings)
    output.parent.mkdir(parents=True, exist_ok=True)
    overflows = 0
    recorded = 0

    with sd.RawInputStream(device=index, channels=CHANNELS, dtype="int16",
                           samplerate=SAMPLE_RATE, blocksize=960,
                           extra_settings=settings) as stream:
        with output.open("xb") as file:
            with wave.open(file, "wb") as wav:
                wav.setnchannels(CHANNELS)
                wav.setsampwidth(SAMPLE_WIDTH)
                wav.setframerate(SAMPLE_RATE)
                while recorded < total_frames:
                    count = min(960, total_frames - recorded)
                    data, overflowed = stream.read(count)
                    pcm = bytes(data)
                    if len(pcm) != count * CHANNELS * SAMPLE_WIDTH:
                        raise RuntimeError("Unexpected audio block length")
                    # 每一帧包含 [CH1, CH2, CH3, CH4]，然后是下一帧的四路。
                    wav.writeframesraw(pcm)
                    recorded += count
                    overflows += int(overflowed)

    return {"device": device["name"], "backend": "WASAPI shared",
            "channels": CHANNELS, "sample_rate": SAMPLE_RATE, "sample_bits": 16,
            "frames": recorded, "seconds": recorded / SAMPLE_RATE,
            "overflow_count": overflows, "output": str(output.resolve())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=3)
    parser.add_argument("--device-name", default="YDPI4MIC")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    output = args.output or Path(__file__).parent / "output" / f"microphone-4ch-{stamp}.wav"
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(record_wav(output, args.seconds, args.device_name),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
