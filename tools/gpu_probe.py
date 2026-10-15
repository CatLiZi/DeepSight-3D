"""Single-GPU capacity probe for the locator demo.

This answers one concrete question: when TalkNet (PyTorch/CUDA) and SCRFD
(onnxruntime-gpu/CUDA) both run inside the *same* process, does the single
local GPU have room for both, and do their CUDA/cuDNN runtimes collide?

Run it on every machine before a demo:

    python tools/gpu_probe.py

It loads each runtime in stages, recording VRAM deltas so you can see exactly
which stage costs how much. Nothing here is used by the app itself.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def gpu_used_mb() -> float | None:
    """System-wide VRAM in use, in MiB.

    Per-*process* VRAM is not available on Windows: the WDDM driver reports
    ``[N/A]`` for every row of ``--query-compute-apps=pid,used_memory``. So we
    track the device-wide figure instead. It includes unrelated desktop apps,
    which is why the deltas between stages still tell the story but the
    absolute number is only a rough guide. PyTorch's own allocator stats
    (below) give exact numbers for the torch side.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip().splitlines()[0]
        return float(out.strip())
    except Exception:
        return None


def torch_vram_mb() -> tuple[float, float] | None:
    """(allocated, reserved) MiB from PyTorch's caching allocator."""
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        return (
            torch.cuda.memory_allocated() / 1024**2,
            torch.cuda.memory_reserved() / 1024**2,
        )
    except Exception:
        return None


def gpu_total_mb() -> tuple[str, float] | None:
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip().splitlines()[0]
        name, total = [item.strip() for item in out.split(",")]
        return name, float(total)
    except Exception:
        return None


class Stage:
    """Tracks VRAM cost of each stage against the previous baseline."""

    def __init__(self) -> None:
        self.baseline = gpu_used_mb()
        self.rows: list[tuple[str, float, float, float, str]] = []

    def record(self, name: str, note: str = "") -> None:
        used = gpu_used_mb()
        delta = None if (used is None or self.baseline is None) else used - self.baseline
        stats = torch_vram_mb()
        torch_mb = stats[1] if stats else -1.0
        self.rows.append(
            (
                name,
                used if used is not None else -1.0,
                delta if delta is not None else -1.0,
                torch_mb,
                note,
            )
        )
        if used is not None and self.baseline is not None:
            self.baseline = used

    def report(self) -> None:
        print()
        print(f"{'stage':<32}{'GPU used':>11}{'delta':>11}{'torch rsv':>11}  note")
        print("-" * 100)
        for name, used, delta, torch_mb, note in self.rows:
            # -1.0 is the sentinel for "could not measure"; real deltas may be
            # negative, which is meaningful (the stage freed memory).
            u = f"{used:.0f}" if used != -1.0 else "n/a"
            d = f"{delta:+.0f}" if delta != -1.0 else "n/a"
            t = f"{torch_mb:.0f}" if torch_mb != -1.0 else "-"
            print(f"{name:<32}{u:>11}{d:>11}{t:>11}  {note}")


def check_torch(stage: Stage) -> bool:
    print("\n[1/4] PyTorch CUDA context")
    try:
        import torch
    except Exception as exc:
        print(f"    torch not importable: {type(exc).__name__}: {exc}")
        return False
    print(f"    torch {torch.__version__}, cuda build {torch.version.cuda}")
    if not torch.cuda.is_available():
        print("    cuda NOT available")
        return False
    props = torch.cuda.get_device_properties(0)
    print(f"    device: {props.name}, sm_{props.major}{props.minor}, {props.total_memory / 1024**2:.0f} MiB")
    # Force real context creation, which is where the memory actually lands.
    x = torch.zeros(64, 64, device="cuda")
    (x @ x).sum().item()
    stage.record("torch cuda context", "reserved by driver + cuDNN")
    return True


def check_onnxruntime(stage: Stage) -> bool:
    print("\n[2/4] ONNX Runtime CUDA provider")
    try:
        import onnxruntime as ort
    except Exception as exc:
        print(f"    onnxruntime not importable: {type(exc).__name__}: {exc}")
        return False
    providers = list(ort.get_available_providers())
    print(f"    onnxruntime {ort.__version__}")
    print(f"    providers: {providers}")
    if "CUDAExecutionProvider" not in providers:
        print("    CUDAExecutionProvider absent")
        return False
    if hasattr(ort, "preload_dlls"):
        try:
            ort.preload_dlls()
            print("    preload_dlls(): ok (this is what avoids the Windows DLL clash)")
        except Exception as exc:
            print(f"    preload_dlls() failed: {type(exc).__name__}: {exc}")
    model = ROOT / "models" / "scrfd.onnx"
    if not model.exists():
        print(f"    {model} missing, skipping session test")
        return False
    try:
        session = ort.InferenceSession(
            str(model),
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        )
    except Exception as exc:
        print(f"    CUDA session failed: {type(exc).__name__}: {exc}")
        return False
    actual = session.get_providers()
    print(f"    session bound to: {actual}")
    stage.record("onnxruntime cuda session", "SCRFD on GPU" if actual[0] == "CUDAExecutionProvider" else "fell back to CPU")
    # Run one inference so lazy allocations are included in the number.
    name = session.get_inputs()[0].name
    shape = [d if isinstance(d, int) else 1 for d in session.get_inputs()[0].shape]
    import numpy as np

    feed = {name: np.zeros(shape, dtype=np.float32)}
    try:
        session.run(None, feed)
    except Exception as exc:
        print(f"    inference failed: {type(exc).__name__}: {exc}")
    stage.record("onnxruntime after 1 infer", "includes workspace/arena")
    return actual[0] == "CUDAExecutionProvider"


def check_talknet(stage: Stage) -> bool:
    print("\n[3/4] TalkNet (weights + real inference)")
    try:
        import numpy as np

        from locator_demo.talknet_adapter import is_available, score_face
        from locator_demo.vision import FaceTarget
    except Exception as exc:
        print(f"    adapter import failed: {type(exc).__name__}: {exc}")
        return False
    ok, message = is_available()
    print(f"    is_available: {ok} -- {message}")
    if not ok:
        return False
    stage.record("talknet model loaded", message.split(":")[0])

    import cv2

    width, height, fps = 640, 480, 25
    window_s = float(os.environ.get("LOCATOR_TALKNET_WINDOW_S", "2.0"))
    rng = np.random.default_rng(0)
    base = rng.integers(60, 200, size=(height, width, 3), dtype=np.uint8)
    frames = []
    for i in range(int(window_s * fps) + 2):
        img = cv2.GaussianBlur(base, (0, 0), 3)
        cv2.ellipse(img, (width // 2, height // 2), (110, 140), 0, 0, 360, (200, 180, 160), -1)
        ok_jpeg, buf = cv2.imencode(".jpg", img)
        frames.append((i / fps, buf.tobytes(), width, height))

    sample_rate = 16000
    audio = (rng.standard_normal(int(window_s * sample_rate)) * 0.05).tolist()
    target = FaceTarget(x1=210.0, y1=110.0, x2=430.0, y2=390.0, face_id=1, score=0.9)
    try:
        score = score_face(
            target=target,
            frame_width=width,
            frame_height=height,
            frames=frames,
            audio_samples=audio,
            sample_rate=sample_rate,
        )
    except Exception as exc:
        print(f"    inference failed: {type(exc).__name__}: {exc}")
        return False
    print(f"    inference ok, speaking probability = {score:.4f}")
    stage.record("talknet after inference", "activations for 1 face")
    return True


def check_coexistence(stage: Stage) -> None:
    print("\n[4/4] Coexistence check (both CUDA runtimes live at once)")
    ok_torch = stage.rows and any("torch" in r[0] for r in stage.rows)
    ok_ort = any("onnxruntime" in r[0] for r in stage.rows)
    if ok_torch and ok_ort:
        print("    PyTorch and ONNX Runtime are both resident. This is the demo configuration.")
    elif ok_torch:
        print("    Only PyTorch is resident. SCRFD is on CPU; TalkNet still uses the GPU.")
    elif ok_ort:
        print("    Only ONNX Runtime is resident. SCRFD uses the GPU; TalkNet unavailable.")
    else:
        print("    Neither CUDA runtime is resident. Everything runs on CPU.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe single-GPU capacity for the locator demo.")
    parser.add_argument("--skip-talknet", action="store_true", help="do not load TalkNet weights")
    args = parser.parse_args()

    info = gpu_total_mb()
    print("=" * 100)
    print("Locator demo GPU probe")
    print("=" * 100)
    if info:
        name, total = info
        print(f"GPU: {name}, {total:.0f} MiB total")
    else:
        print("GPU: could not query nvidia-smi")
    print(f"process: pid {os.getpid()}, GPU already in use: {_fmt(gpu_used_mb())}")
    print(f"torch alloc conf: {os.environ.get('PYTORCH_CUDA_ALLOC_CONF', '(unset)')}")

    stage = Stage()
    check_torch(stage)
    check_onnxruntime(stage)
    if not args.skip_talknet:
        check_talknet(stage)
    else:
        print("\n[3/4] TalkNet skipped by flag")
    check_coexistence(stage)
    stage.report()
    print()
    return 0


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f} MiB"


if __name__ == "__main__":
    raise SystemExit(main())
