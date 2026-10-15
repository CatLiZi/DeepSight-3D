from __future__ import annotations

import math
import struct
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .audio import AudioDirection, AudioDirectionSmoother, AudioEstimate, classify_audio_direction, rms, srp_phat_front_hemisphere
from .audio_denoise import DeepFilterNetStereoDenoiser, save_pcm16_stereo_to_wav
from .devices import choose_dshow_device


@dataclass(frozen=True)
class AudioDeviceStatus:
    ok: bool
    device_name: str
    message: str


class SimulatedAudioLocalizer:
    def __init__(self):
        self._phase = 0

    def read_estimate(self) -> AudioEstimate:
        self._phase = (self._phase + 1) % 120
        tdoa = math.sin(self._phase / 120.0 * math.tau) * 0.00022
        return classify_audio_direction(tdoa, 0.75)

    def status(self) -> AudioDeviceStatus:
        return AudioDeviceStatus(True, "simulated", "simulated audio localizer")


class FfmpegAudioLocalizer:
    def __init__(
        self,
        *,
        device_name: str | None = None,
        sample_rate: int = 16000,
        frame_ms: int = 40,
        ffmpeg: str = "ffmpeg",
        denoise: bool = False,
        denoise_dry_mix: float = 0.15,
        denoise_output_dir: str | None = None,
        recording_enabled: bool = True,
        use_denoised_for_localization: bool = False,
    ):
        self.requested_device_name = device_name
        self.device_name = device_name or ""
        self.sample_rate = int(sample_rate)
        self.frame_ms = int(frame_ms)
        self.ffmpeg = ffmpeg
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._latest_error = "stopped"
        self._reader: threading.Thread | None = None
        self._smoother = AudioDirectionSmoother(window_size=4, min_samples=2)
        self._latest_estimate = classify_audio_direction(0.0, 0.0)
        self._latest_raw_estimate = classify_audio_direction(0.0, 0.0)
        self._latest_denoised_estimate = classify_audio_direction(0.0, 0.0)
        self._latest_frame_time = 0.0
        self._noise_floor = 0.03
        history_frames = max(1, int(math.ceil(3.5 * 1000.0 / max(1, self.frame_ms))))
        self._recent_raw: deque[tuple[float, bytes]] = deque(maxlen=history_frames)
        self._denoise_enabled = bool(denoise)
        self._use_denoised_for_localization = bool(use_denoised_for_localization)
        self._denoise_dry_mix = float(denoise_dry_mix)
        self._denoise_output_dir = Path(denoise_output_dir) if denoise_output_dir else None
        self._denoiser: DeepFilterNetStereoDenoiser | None = None
        self._denoise_counter = 0
        self._denoise_speech_threshold = 0.35
        self._denoise_energy_threshold = 0.08
        self._human_voice_streak = 0
        self._max_physical_tdoa_s = 0.00026
        self._recording_enabled = bool(recording_enabled)
        self._recording_chunks: list[bytes] = []
        self._recording_denoised_chunks: list[bytes] = []
        self._recording_output_path: Path | None = None
        self._recording_denoised_output_path: Path | None = None
        self._recording_session_stamp: str | None = None

    def _resolve_device_name(self) -> str | None:
        if self.device_name:
            return self.device_name
        device = choose_dshow_device(
            "audio",
            [self.requested_device_name, "YDM2MIC", "Realtek", "Virtual Desktop Audio"],
            ffmpeg=self.ffmpeg,
        )
        if device is None:
            self._latest_error = "no DirectShow audio input found"
            return None
        self.device_name = device.name
        return self.device_name

    def _consume_stderr_if_exited(self) -> None:
        if self._process is None or self._process.poll() is None or self._process.stderr is None:
            return
        stderr = self._process.stderr.read().decode("utf-8", errors="replace").strip()
        if stderr:
            self._latest_error = stderr.splitlines()[-1]
        else:
            self._latest_error = f"ffmpeg exited with {self._process.returncode}"

    def start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        device_name = self._resolve_device_name()
        if device_name is None:
            return
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "dshow",
            "-i",
            f"audio={device_name}",
            "-ac",
            "2",
            "-ar",
            str(self.sample_rate),
            "-f",
            "s16le",
            "-",
        ]
        try:
            self._process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self._latest_error = "starting"
        except Exception as exc:
            self._process = None
            self._latest_error = f"audio error: {exc}"
            return
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _speech_confidence(self, left: list[float], right: list[float], energy: float) -> tuple[float, str]:
        try:
            import numpy as np

            mixed = (np.asarray(left, dtype=np.float64) + np.asarray(right, dtype=np.float64)) * 0.5
            if mixed.size < 8:
                return 0.0, "quiet"
            spectrum = np.abs(np.fft.rfft(mixed))
            freqs = np.fft.rfftfreq(mixed.size, 1.0 / self.sample_rate)
            full = float(np.sum(spectrum * spectrum)) + 1e-12
            voiced_band = (freqs >= 80.0) & (freqs < 250.0)
            speech_band = (freqs >= 250.0) & (freqs <= 1000.0)
            clarity_band = (freqs > 1000.0) & (freqs <= 4000.0)
            rumble_band = (freqs >= 20.0) & (freqs < 80.0)
            low_motor_band = (freqs >= 80.0) & (freqs < 180.0)
            voiced_ratio = float(np.sum(spectrum[voiced_band] * spectrum[voiced_band]) / full)
            speech_ratio = float(np.sum(spectrum[speech_band] * spectrum[speech_band]) / full)
            clarity_ratio = float(np.sum(spectrum[clarity_band] * spectrum[clarity_band]) / full)
            rumble_ratio = float(np.sum(spectrum[rumble_band] * spectrum[rumble_band]) / full)
            low_motor_ratio = float(np.sum(spectrum[low_motor_band] * spectrum[low_motor_band]) / full)
            noise_margin = energy / max(self._noise_floor * 1.6, 1e-4)
            speech_score = 0.90 * voiced_ratio + 1.45 * speech_ratio + 1.20 * clarity_ratio
            confidence = min(1.0, max(0.0, speech_score)) * min(1.0, noise_margin)
            if rumble_ratio > 0.55 and speech_ratio + clarity_ratio < 0.28:
                confidence *= 0.35
                return confidence, "fan_or_motor"
            if low_motor_ratio > 0.72 and clarity_ratio < 0.05 and speech_ratio < 0.12:
                confidence *= 0.55
                return confidence, "low_band_noise"
            if energy < max(0.045, self._noise_floor * 1.4):
                return min(confidence, 0.18), "quiet"
            if voiced_ratio >= 0.20 and speech_ratio + clarity_ratio >= 0.12:
                confidence = max(confidence, min(1.0, 0.25 + voiced_ratio + 0.5 * (speech_ratio + clarity_ratio)))
                return confidence, "voiced_speech"
            return confidence, "speech_like" if confidence >= 0.35 else "noise"
        except Exception:
            margin = energy / max(self._noise_floor * 1.6, 1e-4)
            confidence = min(1.0, max(0.0, (margin - 0.6) / 1.8))
            return confidence, "speech_like" if confidence >= 0.35 else "noise"

    def _estimate_from_raw(self, raw: bytes) -> AudioEstimate:
        values = struct.unpack("<" + "h" * (len(raw) // 2), raw)
        left = [values[i] / 32768.0 for i in range(0, len(values), 2)]
        right = [values[i] / 32768.0 for i in range(1, len(values), 2)]
        left_level = rms(left)
        right_level = rms(right)
        energy = max(left_level, right_level) * 12.0
        speech_confidence, noise_state = self._speech_confidence(left, right, energy)
        if speech_confidence < 0.20:
            self._noise_floor = 0.98 * self._noise_floor + 0.02 * max(energy, 0.001)
        level_sum = left_level + right_level
        if energy >= 0.08 and level_sum > 0:
            balance = (left_level - right_level) / level_sum
            if speech_confidence >= 0.25 and abs(balance) >= 0.30:
                tdoa = 0.00035 if balance > 0 else -0.00035
                if abs(tdoa) > self._max_physical_tdoa_s:
                    return classify_audio_direction(
                        0.0,
                        0.0,
                        min_energy=0.06,
                        speech_confidence=0.0,
                        doa_confidence=0.0,
                        noise_state="invalid_delay",
                    )
                return classify_audio_direction(
                    tdoa,
                    min(1.0, energy),
                    min_energy=0.08,
                    speech_confidence=speech_confidence,
                    doa_confidence=0.65,
                    peak_ratio=1.0 + abs(balance) * 3.0,
                    noise_state=noise_state,
                )
        srp = srp_phat_front_hemisphere(left, right, sample_rate=self.sample_rate)
        if abs(srp.tdoa_s) > self._max_physical_tdoa_s:
            return classify_audio_direction(
                0.0,
                0.0,
                min_energy=0.06,
                speech_confidence=0.0,
                doa_confidence=0.0,
                noise_state="invalid_delay",
            )
        return classify_audio_direction(
            srp.tdoa_s,
            min(1.0, energy),
            min_energy=0.08,
            speech_confidence=speech_confidence,
            doa_confidence=srp.confidence,
            peak_ratio=srp.peak_ratio,
            noise_state=noise_state,
            azimuth_deg=srp.azimuth_deg,
        )

    def _human_voice_double_check(self, raw_estimate: AudioEstimate, denoised_estimate: AudioEstimate) -> AudioEstimate:
        raw_human = (
            raw_estimate.noise_state in {"voiced_speech", "speech_like"}
            and raw_estimate.direction != AudioDirection.UNKNOWN
            and raw_estimate.speech_confidence >= 0.55
            and raw_estimate.energy >= 0.10
        )

        denoised_human = (
            denoised_estimate.noise_state in {"voiced_speech", "speech_like"}
            and denoised_estimate.direction != AudioDirection.UNKNOWN
            and denoised_estimate.speech_confidence >= 0.35
            and denoised_estimate.energy >= 0.08
        )

        if self._denoise_enabled and self._use_denoised_for_localization:
            stable_human = raw_human and (denoised_human or raw_estimate.speech_confidence >= 0.70)
            candidate = raw_estimate
        else:
            stable_human = raw_human
            candidate = raw_estimate

        if stable_human:
            self._human_voice_streak += 1
        else:
            self._human_voice_streak = 0

        if self._human_voice_streak >= 2:
            print(
                f"[VOICE_DECISION] status=HUMAN_VOICE streak={self._human_voice_streak} "
                f"direction={candidate.direction.value} speech={candidate.speech_confidence:.3f} "
                f"energy={candidate.energy:.3f} noise_state={candidate.noise_state} "
                f"azimuth={candidate.azimuth_deg:.1f}deg raw_state={raw_estimate.noise_state} denoised_state={denoised_estimate.noise_state}"
            )
            return candidate

        print(
            f"[VOICE_DECISION] status=NON_HUMAN streak={self._human_voice_streak} reason={candidate.noise_state or 'unknown'} "
            f"direction={candidate.direction.value} speech={candidate.speech_confidence:.3f} "
            f"energy={candidate.energy:.3f} raw_state={raw_estimate.noise_state} denoised_state={denoised_estimate.noise_state}"
        )
        return classify_audio_direction(
            0.0,
            0.0,
            min_energy=0.06,
            speech_confidence=0.0,
            doa_confidence=0.0,
            noise_state="noise",
        )

    def _read_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        sample_count = max(1, int(self.sample_rate * self.frame_ms / 1000))
        frame_bytes = sample_count * 2 * 2
        while process.poll() is None:
            raw = process.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                self._consume_stderr_if_exited()
                time.sleep(0.02)
                continue
            raw_estimate = self._estimate_from_raw(raw)
            if self._denoise_enabled:
                self._ensure_denoiser()
                if self._denoiser is not None:
                    try:
                        result = self._denoiser.process_pcm16(raw, sample_rate=self.sample_rate)
                        raw_denoised = result.pcm16_stereo
                    except Exception as exc:
                        raw_denoised = raw
                        self._latest_error = f"denoise error: {exc}"
                else:
                    raw_denoised = raw
            else:
                raw_denoised = raw
            denoised_estimate = self._estimate_from_raw(raw_denoised)
            downstream_estimate = self._human_voice_double_check(raw_estimate, denoised_estimate)
            estimate = self._smoother.update(downstream_estimate)
            if self._denoise_output_dir is not None and self._recording_enabled:
                self._append_recording_chunk(raw, estimate)
                self._append_recording_chunk(raw_denoised, estimate, denoised=True)
            with self._lock:
                self._recent_raw.append((time.time(), raw_denoised))
                self._latest_estimate = estimate
                self._latest_raw_estimate = raw_estimate
                self._latest_denoised_estimate = denoised_estimate
                self._latest_frame_time = time.time()
                self._latest_error = "running"

    def _ensure_denoiser(self) -> None:
        if self._denoiser is not None:
            return
        try:
            self._denoiser = DeepFilterNetStereoDenoiser(dry_mix=self._denoise_dry_mix)
        except Exception as exc:
            self._denoiser = None
            self._latest_error = f"denoise init error: {exc}"

    def set_recording_enabled(self, enabled: bool) -> None:
        self._recording_enabled = bool(enabled)

    def _append_recording_chunk(self, chunk: bytes, estimate: AudioEstimate, *, denoised: bool = False) -> None:
        if self._denoise_output_dir is None or not self._recording_enabled:
            return
        if self._recording_session_stamp is None:
            self._recording_session_stamp = time.strftime("%Y%m%d_%H%M%S")
        if denoised:
            if self._recording_denoised_output_path is None:
                self._denoise_output_dir.mkdir(parents=True, exist_ok=True)
                self._recording_denoised_output_path = (
                    self._denoise_output_dir / f"speech_capture_{self._recording_session_stamp}.wav"
                )
            self._recording_denoised_chunks.append(chunk)
            return
        if self._recording_output_path is None:
            self._denoise_output_dir.mkdir(parents=True, exist_ok=True)
            self._recording_output_path = (
                self._denoise_output_dir / f"speech_capture_raw_{self._recording_session_stamp}.wav"
            )
        self._recording_chunks.append(chunk)

    def stop(self) -> None:
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None
        if self._recording_output_path is not None and self._recording_chunks:
            try:
                save_pcm16_stereo_to_wav(self._recording_output_path, b"".join(self._recording_chunks), self.sample_rate)
                print(f"[denoise] wrote raw audio to {self._recording_output_path}")
            except Exception as exc:
                print(f"[denoise] write failed: {exc}")
            self._recording_chunks.clear()
            self._recording_output_path = None
        if self._recording_denoised_output_path is not None and self._recording_denoised_chunks:
            try:
                save_pcm16_stereo_to_wav(
                    self._recording_denoised_output_path,
                    b"".join(self._recording_denoised_chunks),
                    self.sample_rate,
                )
                print(f"[denoise] wrote denoised audio to {self._recording_denoised_output_path}")
            except Exception as exc:
                print(f"[denoise] denoised write failed: {exc}")
            self._recording_denoised_chunks.clear()
            self._recording_denoised_output_path = None
        self._recording_session_stamp = None

    def status(self) -> AudioDeviceStatus:
        running = self._process is not None and self._process.poll() is None
        if not running:
            self._consume_stderr_if_exited()
        return AudioDeviceStatus(running, self.device_name or "auto", "running" if running else self._latest_error)

    def read_estimate(self) -> AudioEstimate:
        with self._lock:
            self.start()
            if self._process is None:
                return classify_audio_direction(0.0, 0.0)
            return self._latest_estimate

    @staticmethod
    def _estimate_to_dict(estimate: AudioEstimate) -> dict[str, Any]:
        return {
            "direction": estimate.direction.value,
            "confidence": round(estimate.confidence, 3),
            "speech_confidence": round(estimate.speech_confidence, 3),
            "doa_confidence": round(estimate.doa_confidence, 3),
            "energy": round(estimate.energy, 3),
            "azimuth_deg": round(estimate.azimuth_deg, 2),
        }

    def comparison_metrics(self) -> dict[str, Any]:
        with self._lock:
            raw_est = self._latest_raw_estimate
            denoised_est = self._latest_denoised_estimate
            using = "raw" if not self._use_denoised_for_localization or not self._denoise_enabled else "denoised"
        return {
            "using_for_downstream": using,
            "raw": self._estimate_to_dict(raw_est),
            "denoised": self._estimate_to_dict(denoised_est),
            "delta": {
                "confidence": round(denoised_est.confidence - raw_est.confidence, 3),
                "speech_confidence": round(denoised_est.speech_confidence - raw_est.speech_confidence, 3),
                "doa_confidence": round(denoised_est.doa_confidence - raw_est.doa_confidence, 3),
                "energy": round(denoised_est.energy - raw_est.energy, 3),
            },
        }

    def recent_mono_audio(self, *, seconds: float = 2.0) -> tuple[int, list[float]]:
        cutoff = time.time() - max(0.1, float(seconds))
        with self._lock:
            chunks = [raw for ts, raw in self._recent_raw if ts >= cutoff]
        if not chunks:
            return self.sample_rate, []
        values = struct.unpack("<" + "h" * (sum(len(chunk) for chunk in chunks) // 2), b"".join(chunks))
        mono = [
            ((values[i] + values[i + 1]) * 0.5) / 32768.0
            for i in range(0, len(values) - 1, 2)
        ]
        return self.sample_rate, mono


AudioFactory = Callable[[], FfmpegAudioLocalizer | SimulatedAudioLocalizer]
