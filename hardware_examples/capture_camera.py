"""通过 FFmpeg/DirectShow 抓取新相机的一帧图像，不使用 COM 串口。"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import shutil
import subprocess


def capture_jpeg(
    device: str = "USB Video Device",
    size: str = "3840x1080",
    input_fps: float = 60,
    eye: str = "full",
    width: int | None = None,
) -> bytes:
    """返回 JPEG 字节；双目原图为左右拼接，可选择 full/left/right。"""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg was not found on PATH")
    if eye not in {"full", "left", "right"}:
        raise ValueError("eye must be full, left, or right")
    if width is not None and (width < 2 or width % 2):
        raise ValueError("width must be a positive even integer")

    filters = []
    if eye == "left":
        filters.append("crop=iw/2:ih:0:0")
    elif eye == "right":
        filters.append("crop=iw/2:ih:iw/2:0")
    if width is not None:
        filters.append(f"scale={width}:-2")

    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "dshow", "-rtbufsize", "128M",
        "-video_size", size, "-framerate", str(input_fps),
        "-vcodec", "mjpeg", "-i", f"video={device}", "-an",
    ]
    if filters:
        command += ["-vf", ",".join(filters)]
    command += ["-frames:v", "1", "-c:v", "mjpeg", "-q:v", "3",
                "-f", "image2pipe", "pipe:1"]

    # stdout 管道中传输的是图像字节，不是串口数据。
    result = subprocess.run(command, capture_output=True, timeout=15, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    if not result.stdout.startswith(b"\xff\xd8") or not result.stdout.endswith(b"\xff\xd9"):
        raise RuntimeError("The camera did not return a complete JPEG frame")
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="USB Video Device")
    parser.add_argument("--size", default="3840x1080")
    parser.add_argument("--input-fps", type=float, default=60)
    parser.add_argument("--eye", choices=["full", "left", "right"], default="full")
    parser.add_argument("--width", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    output = args.output or Path(__file__).parent / "output" / f"camera-{args.eye}-{stamp}.jpg"
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    jpeg = capture_jpeg(args.device, args.size, args.input_fps, args.eye, args.width)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as file:
        file.write(jpeg)
    print(f"Saved: {output.resolve()} ({len(jpeg)} bytes)")


if __name__ == "__main__":
    main()
