from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class DenoiseResult:
    pcm16_stereo: bytes
    latency_ms: float
    input_rms: float
    output_rms: float


class DeepFilterNetStereoDenoiser:
    """DeepFilterNet wrapper for interleaved stereo PCM16 audio.

    The class accepts stereo audio, enhances both channels together, and returns
    stereo audio with the original sample rate and number of samples. It is meant
    for an auxiliary enhancement/ASD path. Do not feed the enhanced channels back
    into GCC-PHAT; localization should continue using the original stereo PCM.
    """

    def __init__(self, *, dry_mix: float = 0.15) -> None:
        if not 0.0 <= dry_mix <= 1.0:
            raise ValueError("dry_mix must be between 0 and 1")

        try:
            import torch
            import torchaudio.functional as audio_functional
            from df.enhance import enhance, init_df
        except ImportError as exc:
            raise RuntimeError(
                "DeepFilterNet backend is unavailable. Install it with: "
                "pip install deepfilternet torch torchaudio"
            ) from exc

        self._torch = torch
        self._audio_functional = audio_functional
        self._enhance = enhance
        self._model, self._df_state, _ = init_df()
        self._model.eval()
        self._model_sample_rate = int(self._df_state.sr())
        self._dry_mix = float(dry_mix)
        self._lock = threading.Lock()

    @property
    def model_sample_rate(self) -> int:
        return self._model_sample_rate

    @staticmethod
    def _rms(audio: np.ndarray) -> float:
        if audio.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))

    def process_pcm16(self, raw: bytes, *, sample_rate: int) -> DenoiseResult:
        import time

        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if len(raw) % 4 != 0:
            raise ValueError("Stereo PCM16 byte length must be divisible by 4")

        interleaved = np.frombuffer(raw, dtype="<i2")
        if interleaved.size == 0:
            return DenoiseResult(raw, 0.0, 0.0, 0.0)

        stereo = interleaved.reshape(-1, 2).astype(np.float32) / 32768.0
        original_samples = int(stereo.shape[0])
        input_rms = self._rms(stereo)

        # DeepFilterNet uses [channels, time].
        audio = self._torch.from_numpy(np.ascontiguousarray(stereo.T)).float()

        if sample_rate != self._model_sample_rate:
            audio = self._audio_functional.resample(
                audio,
                orig_freq=sample_rate,
                new_freq=self._model_sample_rate,
            )

        started = time.perf_counter()
        with self._lock, self._torch.inference_mode():
            enhanced = self._enhance(self._model, self._df_state, audio)
        latency_ms = (time.perf_counter() - started) * 1000.0

        if sample_rate != self._model_sample_rate:
            enhanced = self._audio_functional.resample(
                enhanced,
                orig_freq=self._model_sample_rate,
                new_freq=sample_rate,
            )

        # Resampling and model padding can change the length by a few samples.
        if enhanced.shape[-1] > original_samples:
            enhanced = enhanced[..., :original_samples]
        elif enhanced.shape[-1] < original_samples:
            missing = original_samples - int(enhanced.shape[-1])
            enhanced = self._torch.nn.functional.pad(enhanced, (0, missing))

        dry = self._torch.from_numpy(np.ascontiguousarray(stereo.T)).float()
        enhanced = (1.0 - self._dry_mix) * enhanced + self._dry_mix * dry

        # One common gain keeps the relative L/R level relationship intact.
        peak = float(enhanced.abs().max().item()) if enhanced.numel() else 0.0
        if peak > 0.999:
            enhanced = enhanced * (0.999 / peak)

        enhanced_np = (
            enhanced.transpose(0, 1)
            .contiguous()
            .cpu()
            .numpy()
            .astype(np.float32)
        )
        output_rms = self._rms(enhanced_np)
        output_pcm = np.clip(enhanced_np * 32767.0, -32768, 32767).astype("<i2")

        return DenoiseResult(
            pcm16_stereo=output_pcm.tobytes(),
            latency_ms=latency_ms,
            input_rms=input_rms,
            output_rms=output_rms,
        )


def save_pcm16_stereo_to_wav(path: Path | str, pcm16_stereo: bytes, sample_rate: int) -> None:
    import wave

    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path_obj), mode="wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16_stereo)
