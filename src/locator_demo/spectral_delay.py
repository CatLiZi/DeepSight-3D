"""Reliability-weighted binaural delay, using averaged cross spectra.

The covariance is averaged over multiple overlapping short frames. A single
FFT cannot estimate magnitude-squared coherence (it would be identically 1).
No source direction, user labels, camera coordinates or learned weights enter
this estimator. The returned uncertainty is diagnostic, not calibrated error.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
from scipy.signal import find_peaks

from .band_delay import frequency_delay_evidence, frequency_delay_conflict


@dataclass(frozen=True)
class DelayEvidence:
    lag_samples: float | None
    reason: str
    correlation: float = 0.0
    coherence: float = 0.0
    peak_ratio: float = 0.0
    speech_fraction: float = 0.0
    spectral_spread_hz: float = 0.0
    average_frames: int = 0
    bands: tuple[dict, ...] = ()
    band_disagreement: bool = False


class ReliabilityWeightedDelay:
    """Shared preprocessing; noise PSD is learned only from non-speech windows."""

    def __init__(self, sample_rate=48000):
        self.sample_rate = int(sample_rate)
        self.nfft = 4096
        self.frequencies = np.fft.rfftfreq(self.nfft, 1 / self.sample_rate)
        self.noise_power = None
        self.noise_windows = 0

    def reset(self):
        self.noise_power = None
        self.noise_windows = 0

    def spectra(self, pair):
        pair = np.asarray(pair, dtype=np.float64)
        segment = min(1024, 1 << (len(pair).bit_length() - 1))
        frames = np.lib.stride_tricks.sliding_window_view(pair, segment, axis=0)[::segment // 2]
        frames = frames - frames.mean(axis=-1, keepdims=True)
        window = np.hanning(segment)
        spectra = np.fft.rfft(frames * window / math.sqrt(float(window @ window)), n=self.nfft, axis=-1)
        power = np.mean(np.abs(spectra) ** 2, axis=0)
        cross = np.mean(spectra[:, 1] * np.conj(spectra[:, 0]), axis=0)
        coherence = np.clip(np.abs(cross) ** 2 / np.maximum(power[0] * power[1], 1e-30), 0., 1.)
        return power, cross, coherence, len(frames)

    def observe_noise(self, pair):
        power, _, _, _ = self.spectra(pair)
        if self.noise_power is None:
            self.noise_power = power.copy()
        else:
            # Slow upward adaptation avoids absorbing a missed speech onset.
            alpha = np.where(power > self.noise_power, .03, .12)
            self.noise_power += alpha * (power - self.noise_power)
        self.noise_windows += 1

    def estimate(self, pair, *, maximum_lag_samples):
        power, cross, coherence, frame_count = self.spectra(pair)
        freq = self.frequencies
        total = np.maximum(power.sum(axis=1), 1e-30)
        speech_band = (freq >= 100) & (freq <= 4500)
        speech_fraction = float(np.mean(power[:, speech_band].sum(axis=1) / total))
        combined = power.sum(axis=0)
        spectral_center = float(np.sum(freq * combined) / max(float(combined.sum()), 1e-30))
        spread = math.sqrt(float(np.sum((freq - spectral_center) ** 2 * combined) / max(float(combined.sum()), 1e-30)))
        basic = dict(speech_fraction=speech_fraction, spectral_spread_hz=spread, average_frames=frame_count)
        if speech_fraction < .35:
            return DelayEvidence(None, 'noise', **basic)
        if spread < 120:
            return DelayEvidence(None, 'narrowband_tone', **basic)

        gain = np.ones_like(freq)
        if self.noise_power is not None:
            gain = np.min(np.clip(1. - self.noise_power / np.maximum(power, 1e-30), 0., 1.), axis=0)
        support = (freq >= 200) & (freq <= min(6000, self.sample_rate * .45))
        # Partial whitening retains useful speech energy; full PHAT promoted
        # low-SNR bins to equal votes. Coherence and speech surplus weight bins.
        weights = coherence ** 2 * gain ** 2 * support
        base = weights / np.sqrt(np.maximum(np.abs(cross), 1e-30))
        weighted_cross = cross * base
        magnitude = np.abs(weighted_cross)
        evidence_sum = float(magnitude.sum())
        if evidence_sum < 1e-15:
            return DelayEvidence(None, 'low_snr', **basic)

        cc = np.fft.irfft(weighted_cross, n=self.nfft)
        # Search beyond the admissible interval to detect an out-of-range
        # strongest peak, rather than forcing it onto a physical endpoint.
        extent = math.ceil(maximum_lag_samples) + 10
        lags = np.arange(-extent, extent + 1)
        scores = np.abs(cc[lags % self.nfft])
        peak = int(np.argmax(scores))
        shift = float(lags[peak])
        if 0 < peak < len(scores) - 1:
            a, b, c = scores[peak - 1:peak + 2]
            denominator = a - 2 * b + c
            if abs(denominator) > 1e-30:
                shift += float(np.clip(.5 * (a - c) / denominator, -.5, .5))
        if abs(shift) > maximum_lag_samples + .25:
            return DelayEvidence(None, 'invalid_delay', **basic)

        norm = math.sqrt(float(np.sum(power[0] * base) * np.sum(power[1] * base)))
        phase = np.exp(2j * np.pi * freq * shift / self.sample_rate)
        correlation = float(np.clip(abs(np.sum(weighted_cross * phase)) / max(norm, 1e-30), 0., 1.))
        coherent = float(np.sum(coherence * magnitude) / evidence_sum)
        peaks, _ = find_peaks(scores)
        competitors = [float(scores[k]) for k in peaks
                       if abs(lags[k]) <= maximum_lag_samples + .25 and abs(lags[k] - shift) > 4]
        # Count competing local maxima, not neighbours on the same broad lobe.
        ratio = min(10., float(scores[peak]) / max(competitors, default=1e-30)) if competitors else 4.
        bands = frequency_delay_evidence(power, cross, coherence, base, freq,
            sample_rate=self.sample_rate, nfft=self.nfft, maximum_lag_samples=maximum_lag_samples)
        for band in bands:
            mask = (freq >= band['low_hz']) & (freq < band['high_hz'])
            band['speech_surplus'] = float(np.mean(gain[mask]))
        conflict = (correlation >= .45 and coherent >= .6 and ratio >= 1.15
                    and frequency_delay_conflict(shift, bands))
        detail = dict(correlation=correlation, coherence=coherent, peak_ratio=ratio,
                      bands=tuple(bands), band_disagreement=conflict, **basic)
        if correlation < .45 or coherent < .6:
            return DelayEvidence(None, 'low_coherence', **detail)
        if ratio < 1.15:
            return DelayEvidence(None, 'ambiguous_delay', **detail)
        if conflict:
            # Abstain on conflicting acoustic evidence; never substitute a band
            # angle or amplify the measured direction. Visual fallback remains.
            return DelayEvidence(None, 'ambiguous_delay', **detail)
        return DelayEvidence(shift, 'reliable_delay', **detail)
