from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import asin, degrees, sin, radians, sqrt
from collections import deque
from typing import Sequence


class AudioDirection(str, Enum):
    LEFT = "left"
    RIGHT = "right"
    CENTER = "center"
    LEFT_FRONT = "left_front"
    RIGHT_FRONT = "right_front"
    LEFT_BACK = "left_back"
    RIGHT_BACK = "right_back"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AudioEstimate:
    direction: AudioDirection
    confidence: float
    tdoa_s: float
    energy: float
    azimuth_deg: float = 0.0
    speech_confidence: float = 0.0
    doa_confidence: float = 0.0
    peak_ratio: float = 0.0
    noise_state: str = "unknown"
    motor_suppressed: bool = False


@dataclass(frozen=True)
class GccPhatResult:
    tdoa_s: float
    peak_ratio: float
    confidence: float
    azimuth_deg: float = 0.0


_MIRRORED_DIRECTIONS = {
    AudioDirection.LEFT: AudioDirection.RIGHT,
    AudioDirection.RIGHT: AudioDirection.LEFT,
    AudioDirection.LEFT_FRONT: AudioDirection.RIGHT_FRONT,
    AudioDirection.RIGHT_FRONT: AudioDirection.LEFT_FRONT,
    AudioDirection.LEFT_BACK: AudioDirection.RIGHT_BACK,
    AudioDirection.RIGHT_BACK: AudioDirection.LEFT_BACK,
}


def apply_audio_channel_mapping(estimate: AudioEstimate, *, swap_channels: bool) -> AudioEstimate:
    if not swap_channels:
        return estimate
    return AudioEstimate(
        direction=_MIRRORED_DIRECTIONS.get(estimate.direction, estimate.direction),
        confidence=estimate.confidence,
        tdoa_s=-estimate.tdoa_s,
        energy=estimate.energy,
        azimuth_deg=-estimate.azimuth_deg,
        speech_confidence=estimate.speech_confidence,
        doa_confidence=estimate.doa_confidence,
        peak_ratio=estimate.peak_ratio,
        noise_state=estimate.noise_state,
        motor_suppressed=estimate.motor_suppressed,
    )


def with_motor_suppression(estimate: AudioEstimate, *, suppressed: bool) -> AudioEstimate:
    if not suppressed:
        return estimate
    return AudioEstimate(
        direction=AudioDirection.UNKNOWN,
        confidence=min(estimate.confidence, 0.15),
        tdoa_s=estimate.tdoa_s,
        energy=estimate.energy,
        azimuth_deg=estimate.azimuth_deg,
        speech_confidence=estimate.speech_confidence,
        doa_confidence=estimate.doa_confidence,
        peak_ratio=estimate.peak_ratio,
        noise_state="motor_guard",
        motor_suppressed=True,
    )


class AudioDirectionSmoother:
    def __init__(self, *, window_size: int = 4, min_samples: int = 2, min_margin: float = 0.5):
        self.window_size = max(1, int(window_size))
        self.min_samples = max(1, int(min_samples))
        self.min_margin = float(min_margin)
        self._samples: deque[AudioEstimate] = deque(maxlen=self.window_size)

    def update(self, estimate: AudioEstimate) -> AudioEstimate:
        self._samples.append(estimate)
        candidates = [
            sample
            for sample in self._samples
            if sample.direction not in (AudioDirection.UNKNOWN, AudioDirection.CENTER)
            and sample.confidence >= 0.35
        ]
        if len(candidates) < self.min_samples:
            return AudioEstimate(
                AudioDirection.UNKNOWN,
                estimate.confidence,
                estimate.tdoa_s,
                estimate.energy,
                estimate.azimuth_deg,
                estimate.speech_confidence,
                estimate.doa_confidence,
                estimate.peak_ratio,
                estimate.noise_state,
                estimate.motor_suppressed,
            )

        scores: dict[AudioDirection, float] = {}
        weighted_tdoa: dict[AudioDirection, float] = {}
        weighted_azimuth: dict[AudioDirection, float] = {}
        max_energy: dict[AudioDirection, float] = {}
        max_speech: dict[AudioDirection, float] = {}
        max_doa: dict[AudioDirection, float] = {}
        max_peak_ratio: dict[AudioDirection, float] = {}
        for sample in candidates:
            weight = max(0.0, float(sample.confidence))
            scores[sample.direction] = scores.get(sample.direction, 0.0) + weight
            weighted_tdoa[sample.direction] = weighted_tdoa.get(sample.direction, 0.0) + sample.tdoa_s * weight
            weighted_azimuth[sample.direction] = weighted_azimuth.get(sample.direction, 0.0) + sample.azimuth_deg * weight
            max_energy[sample.direction] = max(max_energy.get(sample.direction, 0.0), sample.energy)
            max_speech[sample.direction] = max(max_speech.get(sample.direction, 0.0), sample.speech_confidence)
            max_doa[sample.direction] = max(max_doa.get(sample.direction, 0.0), sample.doa_confidence)
            max_peak_ratio[sample.direction] = max(max_peak_ratio.get(sample.direction, 0.0), sample.peak_ratio)

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        winner, winner_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        if winner_score - runner_up < self.min_margin:
            return AudioEstimate(
                AudioDirection.UNKNOWN,
                estimate.confidence,
                estimate.tdoa_s,
                estimate.energy,
                estimate.azimuth_deg,
                estimate.speech_confidence,
                estimate.doa_confidence,
                estimate.peak_ratio,
                estimate.noise_state,
                estimate.motor_suppressed,
            )

        confidence = min(1.0, winner_score / max(1, len(candidates)))
        tdoa = weighted_tdoa[winner] / max(0.001, winner_score)
        azimuth = weighted_azimuth[winner] / max(0.001, winner_score)
        return AudioEstimate(
            winner,
            confidence,
            tdoa,
            max_energy[winner],
            azimuth,
            max_speech[winner],
            max_doa[winner],
            max_peak_ratio[winner],
            estimate.noise_state,
            estimate.motor_suppressed,
        )


def rms(samples: Sequence[float]) -> float:
    if not samples:
        return 0.0
    return sqrt(sum(float(sample) * float(sample) for sample in samples) / len(samples))


def gcc_phat_tdoa(
    left: Sequence[float],
    right: Sequence[float],
    *,
    sample_rate: int,
    max_tau: float = 0.001,
) -> float:
    """Estimate left-minus-right time delay using FFT GCC-PHAT when available."""
    return gcc_phat(left, right, sample_rate=sample_rate, max_tau=max_tau).tdoa_s


def gcc_phat(
    left: Sequence[float],
    right: Sequence[float],
    *,
    sample_rate: int,
    max_tau: float = 0.001,
) -> GccPhatResult:
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if not left or not right:
        return GccPhatResult(0.0, 0.0, 0.0)
    try:
        import numpy as np

        left_values, right_values, n = _prepare_pair_for_fft(left, right, np)
        spectrum = _phat_spectrum(left_values, right_values, n, np)
        correlation = np.fft.irfft(spectrum, n=n)
        max_shift = max(1, min(int(round(max_tau * sample_rate)), n // 2))
        window = np.concatenate((correlation[-max_shift:], correlation[: max_shift + 1]))
        peak_index = int(np.argmax(np.abs(window)))
        lag = peak_index - max_shift
        abs_window = np.abs(window)
        peak = float(abs_window[peak_index])
        if len(abs_window) > 1:
            masked = abs_window.copy()
            masked[max(0, peak_index - 1) : min(len(masked), peak_index + 2)] = 0.0
            second = float(np.max(masked))
        else:
            second = 0.0
        peak_ratio = peak / max(second, 1e-9)
        confidence = min(1.0, max(0.0, (peak_ratio - 1.0) / 4.0))
        tdoa_s = lag / float(sample_rate)
        return GccPhatResult(tdoa_s, peak_ratio, confidence, azimuth_from_tdoa(tdoa_s))
    except Exception:
        return _bounded_cross_correlation(left, right, sample_rate=sample_rate, max_tau=max_tau)


def srp_phat_front_hemisphere(
    left: Sequence[float],
    right: Sequence[float],
    *,
    sample_rate: int,
    mic_spacing_m: float = 0.12,
    speed_of_sound_m_s: float = 343.0,
    angle_step_deg: float = 3.0,
) -> GccPhatResult:
    """Estimate local azimuth by sweeping only the front 180-degree hemisphere.

    Positive azimuth means the source is to the robot head's left, matching
    classify_audio_direction's positive-TDOA convention.
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if mic_spacing_m <= 0 or not left or not right:
        return GccPhatResult(0.0, 0.0, 0.0, 0.0)
    max_tau = mic_spacing_m / max(1e-6, speed_of_sound_m_s)
    try:
        import numpy as np

        left_values, right_values, n = _prepare_pair_for_fft(left, right, np, pre_emphasis=0.95)
        spectrum = _phat_spectrum(left_values, right_values, n, np)
        freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)
        weighted_window = None
        band_count = 0
        # Low voiced speech is intentionally kept, while higher bands get
        # stronger localization weight because their TDOA peaks are sharper.
        for low_hz, high_hz, weight in (
            (80.0, 250.0, 0.55),
            (250.0, 1000.0, 1.0),
            (1000.0, 4000.0, 1.15),
        ):
            mask = (freqs >= low_hz) & (freqs <= min(high_hz, sample_rate / 2.0))
            if not np.any(mask):
                continue
            band_spectrum = spectrum * mask
            correlation = np.fft.irfft(band_spectrum, n=n)
            max_shift = max(1, min(int(round(max_tau * sample_rate)), n // 2))
            window = np.concatenate((correlation[-max_shift:], correlation[: max_shift + 1]))
            scale = float(np.max(np.abs(window))) or 1.0
            normalized = np.abs(window) / scale
            weighted_window = normalized * weight if weighted_window is None else weighted_window + normalized * weight
            band_count += 1
        if weighted_window is None or band_count == 0:
            return gcc_phat(left, right, sample_rate=sample_rate, max_tau=max_tau)
        weighted_window = weighted_window / float(band_count)
        max_shift = (len(weighted_window) - 1) // 2
        angles = np.arange(-90.0, 90.0 + max(0.5, angle_step_deg), max(0.5, angle_step_deg))
        scores: list[tuple[float, float, float]] = []
        for angle in angles:
            tdoa = sin(radians(float(angle))) * mic_spacing_m / speed_of_sound_m_s
            lag = tdoa * sample_rate
            score = _interpolated_abs_score(weighted_window, lag + max_shift, np)
            scores.append((score, float(angle), tdoa))
        scores.sort(key=lambda item: item[0], reverse=True)
        best_score, best_angle, best_tdoa = scores[0]
        second = 0.0
        for score, angle, _tdoa in scores[1:]:
            if abs(angle - best_angle) >= max(6.0, angle_step_deg * 2.0):
                second = score
                break
        if second <= 0.0 and len(scores) > 1:
            second = scores[1][0]
        peak_ratio = float(best_score) / max(float(second), 1e-9)
        values = np.asarray([score for score, _angle, _tdoa in scores], dtype=np.float64)
        median = float(np.median(values))
        median_contrast = max(0.0, (float(best_score) - median) / max(float(best_score), 1e-9))
        peak_contrast = max(0.0, (peak_ratio - 1.0) / 1.8)
        confidence = min(1.0, max(peak_contrast, median_contrast))
        return GccPhatResult(float(best_tdoa), peak_ratio, confidence, float(best_angle))
    except Exception:
        return gcc_phat(left, right, sample_rate=sample_rate, max_tau=max_tau)


def _prepare_pair_for_fft(
    left: Sequence[float],
    right: Sequence[float],
    np,
    *,
    pre_emphasis: float = 0.0,
):
    left_values = np.asarray(left, dtype=np.float64)
    right_values = np.asarray(right, dtype=np.float64)
    if pre_emphasis:
        left_values = np.concatenate(([left_values[0]], left_values[1:] - pre_emphasis * left_values[:-1]))
        right_values = np.concatenate(([right_values[0]], right_values[1:] - pre_emphasis * right_values[:-1]))
    left_values = left_values - float(np.mean(left_values))
    right_values = right_values - float(np.mean(right_values))
    if len(left_values) > 1:
        window = np.hanning(len(left_values))
        left_values = left_values * window
        right_values = right_values * window
    n = 1
    while n < len(left_values) + len(right_values):
        n <<= 1
    return left_values, right_values, n


def _phat_spectrum(left_values, right_values, n: int, np):
    spectrum = np.fft.rfft(left_values, n=n) * np.conj(np.fft.rfft(right_values, n=n))
    magnitude = np.abs(spectrum)
    return spectrum / np.maximum(magnitude, 1e-12)


def _interpolated_abs_score(window, index: float, np) -> float:
    low = int(np.floor(index))
    high = min(len(window) - 1, low + 1)
    low = max(0, low)
    fraction = max(0.0, min(1.0, index - low))
    return float((1.0 - fraction) * window[low] + fraction * window[high])


def _bounded_cross_correlation(
    left: Sequence[float],
    right: Sequence[float],
    *,
    sample_rate: int,
    max_tau: float,
) -> GccPhatResult:
    max_lag = max(1, int(round(max_tau * sample_rate)))
    best_lag = 0
    best_score = float("-inf")
    left_values = [float(value) for value in left]
    right_values = [float(value) for value in right]
    for lag in range(-max_lag, max_lag + 1):
        score = 0.0
        count = 0
        for i, left_value in enumerate(left_values):
            j = i + lag
            if 0 <= j < len(right_values):
                score += left_value * right_values[j]
                count += 1
        if count and score > best_score:
            best_score = score
            best_lag = lag
    return GccPhatResult(-best_lag / float(sample_rate), 1.0, 0.25)


def azimuth_from_tdoa(
    tdoa_s: float,
    *,
    mic_spacing_m: float = 0.12,
    speed_of_sound_m_s: float = 343.0,
) -> float:
    if mic_spacing_m <= 0:
        return 0.0
    value = max(-1.0, min(1.0, float(tdoa_s) * speed_of_sound_m_s / mic_spacing_m))
    return degrees(asin(value))


def classify_audio_direction(
    tdoa_s: float,
    energy: float,
    *,
    center_deadband_s: float = 0.00006,
    min_energy: float = 0.12,
    max_abs_tdoa_s: float = 0.00035,
    speech_confidence: float | None = None,
    doa_confidence: float = 0.0,
    peak_ratio: float = 0.0,
    noise_state: str = "unknown",
    motor_suppressed: bool = False,
    azimuth_deg: float | None = None,
) -> AudioEstimate:
    azimuth = azimuth_from_tdoa(tdoa_s) if azimuth_deg is None else float(azimuth_deg)
    speech = min(1.0, max(0.0, energy if speech_confidence is None else speech_confidence))
    doa = min(1.0, max(0.0, doa_confidence))
    confidence = min(1.0, max(0.0, energy))
    if motor_suppressed:
        return AudioEstimate(
            AudioDirection.UNKNOWN,
            min(confidence, 0.15),
            tdoa_s,
            energy,
            azimuth,
            speech,
            doa,
            peak_ratio,
            "motor_guard",
            True,
        )
    if energy < min_energy or speech < 0.15:
        return AudioEstimate(AudioDirection.UNKNOWN, confidence, tdoa_s, energy, azimuth, speech, doa, peak_ratio, noise_state)
    if abs(tdoa_s) <= center_deadband_s:
        return AudioEstimate(AudioDirection.CENTER, confidence, tdoa_s, energy, azimuth, speech, doa, peak_ratio, noise_state)
    direction = AudioDirection.LEFT if tdoa_s > 0 else AudioDirection.RIGHT
    delay_score = min(1.0, abs(tdoa_s) / max_abs_tdoa_s)
    combined = confidence * delay_score * max(0.35, speech) * max(0.35, doa)
    return AudioEstimate(
        direction,
        max(combined, 0.35),
        tdoa_s,
        energy,
        azimuth,
        speech,
        doa,
        peak_ratio,
        noise_state,
        motor_suppressed,
    )


def direction_to_sector(direction: AudioDirection, visual_confirmed: bool) -> AudioDirection:
    if direction == AudioDirection.LEFT:
        return AudioDirection.LEFT_FRONT if visual_confirmed else AudioDirection.LEFT_BACK
    if direction == AudioDirection.RIGHT:
        return AudioDirection.RIGHT_FRONT if visual_confirmed else AudioDirection.RIGHT_BACK
    return direction
