"""Head-only, two-ear localization; unused USB channels never enter the DSP."""
from __future__ import annotations

from dataclasses import dataclass
from collections import deque
import math

import numpy as np

from .audio import AudioDirection, AudioEstimate
from .spectral_delay import ReliabilityWeightedDelay
from .delay_track import DelayConsensus


@dataclass(frozen=True)
class EarPairConfig:
    mode: str = "disabled"
    left_channel: int | None = None  # USB channels are numbered 1..4.
    right_channel: int | None = None
    mapping_confirmed: bool = False
    spacing_m: float | None = None
    delay_offset_us: float = 0.0

    def __post_init__(self):
        if self.mode not in ("disabled", "ear_pair"):
            raise ValueError("Unknown localization mode")
        for ch in (self.left_channel, self.right_channel):
            if ch is not None and (isinstance(ch, bool) or ch not in range(1, 5)):
                raise ValueError("Ear channels must be numbered 1..4")
        if self.left_channel is not None and self.left_channel == self.right_channel:
            raise ValueError("Left and right ears require distinct channels")
        if self.spacing_m is not None and not .02 <= self.spacing_m <= .5:
            raise ValueError("Ear spacing must be 0.02..0.5 metres")
        if not math.isfinite(self.delay_offset_us) or abs(self.delay_offset_us) > 1000:
            raise ValueError("Invalid channel delay compensation")

    @property
    def selected(self):
        return [ch for ch in (self.left_channel, self.right_channel) if ch is not None]

    @property
    def ready(self):
        return bool(self.mode == "ear_pair" and self.mapping_confirmed and len(self.selected) == 2 and self.spacing_m)


class EarPairLocalizer:
    def __init__(self, config: EarPairConfig, sample_rate=48000):
        self.config, self.sample_rate = config, sample_rate
        self.diagnostics = {"signal_valid": False, "reason": "ear_mapping_unconfigured",
                            "method": "coherence_weighted_gcc", "analysis_window_s": .192}
        self._angle = None
        self._delays=deque(maxlen=7)
        self._bad_windows = 0
        self.spectral = ReliabilityWeightedDelay(sample_rate)
        self.analysis_window_s = .192
        self.delay_track = DelayConsensus(sample_rate)
        self._sample_end = None
        self._observation_time = 0.

    def _unknown(self, reason, energy=0.0, *, preserve_candidate=False, preserve_frequency_evidence=False):
        self.diagnostics.update(signal_valid=False, reason=reason)
        if not preserve_frequency_evidence:
            self.diagnostics.update(frequency_disagreement=False, frequency_evidence_current=False)
        self._angle = None
        self._bad_windows += 1
        if not preserve_candidate or self._bad_windows >= 3:
            self._delays.clear()
        if reason in {'audio_gap', 'audio_restarted', 'sample_clock_mismatch', 'stale_audio'}:
            self.spectral.reset()
        if reason in {'audio_gap', 'audio_restarted', 'sample_clock_mismatch'}:
            self._sample_end = None
        if not preserve_candidate:
            self.delay_track.reset()
        self.diagnostics['noise_windows'] = self.spectral.noise_windows
        return AudioEstimate(AudioDirection.UNKNOWN, 0, 0, energy, noise_state=reason)

    def estimate(self, four_channel_pcm, *, speech_probability=None, sample_end=None):
        if not self.config.ready:
            return self._unknown("ear_mapping_unconfigured")
        values = np.asarray(four_channel_pcm)
        if values.ndim != 2 or values.shape[1] != 4 or len(values) < self.sample_rate * .02:
            return self._unknown("insufficient_audio")
        if sample_end is not None:
            if isinstance(sample_end,bool) or not isinstance(sample_end,(int,np.integer)) or sample_end<len(values):
                return self._unknown('invalid_sample_clock')
            if self._sample_end is not None and sample_end<=self._sample_end:
                return self._unknown('stale_audio')
            self._sample_end=int(sample_end)
            self._observation_time=sample_end/self.sample_rate
        else:
            # Standalone legacy callers have no PCM clock. Live capture and
            # replay supply sample_end; they never count repeated polls.
            self._observation_time+=.02
        # Select BEFORE any energy, speech, direction or mono calculation.
        pair = values[:, np.array(self.config.selected) - 1].astype(np.float64) / 32768.0
        if not np.isfinite(pair).all():
            return self._unknown("invalid_audio")
        if np.any(np.mean(np.abs(pair) >= .999, axis=0) > .01):
            return self._unknown("ear_clipping")
        pair -= pair.mean(axis=0)
        levels = np.sqrt(np.mean(pair * pair, axis=0))
        energy = min(1.0, float(levels.max()) * 12)
        self.diagnostics.update(ear_rms=levels.tolist())
        if float(levels.min()) < 2 / 32768 or energy < .06:
            return self._unknown("quiet", energy)
        diff = float(np.sqrt(np.mean((pair[:, 0] - pair[:, 1]) ** 2)))
        if diff < 1.5 / 32768 and float(levels.min()) > 15 / 32768:
            return self._unknown("channels_nearly_identical", energy)
        if speech_probability is not None:
            if not math.isfinite(speech_probability) or not 0 <= speech_probability <= 1:
                return self._unknown('invalid_speech_probability', energy)
            if speech_probability < .45:
                self.spectral.observe_noise(pair)
                return self._unknown('no_voice_activity', energy)
        max_tau = self.config.spacing_m / 343.0
        offset = self.config.delay_offset_us * 1e-6
        evidence = self.spectral.estimate(pair, maximum_lag_samples=(max_tau + abs(offset)) * self.sample_rate)
        self.diagnostics.update(method='coherence_weighted_gcc', analysis_window_s=len(pair)/self.sample_rate,
            normalized_correlation=evidence.correlation, spectral_coherence=evidence.coherence,
            peak_ratio=evidence.peak_ratio, frequency_reliability=list(evidence.bands),
            spectral_spread_hz=evidence.spectral_spread_hz, coherence_average_frames=evidence.average_frames,
            noise_windows=self.spectral.noise_windows, physical_max_delay_us=max_tau*1e6,
            frequency_disagreement=evidence.band_disagreement, frequency_evidence_current=True)
        if evidence.lag_samples is None:
            return self._unknown(evidence.reason, energy,
                preserve_candidate=evidence.reason in ('ambiguous_delay','low_coherence','low_snr','invalid_delay'),
                preserve_frequency_evidence=True)
        delay = evidence.lag_samples / self.sample_rate - offset
        if abs(delay) > max_tau + .25/self.sample_rate:
            return self._unknown('invalid_delay',energy)
        admitted,changed=self.delay_track.accept(delay,self._observation_time)
        if not admitted:
            self.diagnostics.update(pending_delay_samples=delay*self.sample_rate,
                transition_confirmation='fresh PCM time; no extrapolated output')
            return self._unknown('direction_transition',energy,preserve_candidate=True)
        if changed:self._delays.clear()
        self._bad_windows = 0
        speech = evidence.speech_fraction if speech_probability is None else float(speech_probability)
        ratio = evidence.peak_ratio
        confidence = float(np.clip(.5*evidence.correlation+.3*evidence.coherence+
                                    .2*min(1.,(ratio-1.)/.5),0.,1.))
        angle = math.degrees(math.asin(float(np.clip(delay / max_tau, -1, 1))))
        self._delays.append(delay)
        center=float(np.median(self._delays));spread=float(np.median(np.abs(np.asarray(self._delays)-center)))
        stability=float(min(1.,len(self._delays)/3)*np.exp(-spread*self.sample_rate/3.))
        if stability>=.5:self.delay_track.commit(center)
        self._angle=math.degrees(math.asin(float(np.clip(center/max_tau,-1,1))))
        sensitivity=math.sqrt(max(.03,1-(center/max_tau)**2))
        std=max(6.,math.degrees(max(.5/self.sample_rate,spread)/(max_tau*sensitivity)))
        direction = AudioDirection.CENTER if abs(self._angle) <= 8 else (
            AudioDirection.LEFT if self._angle > 0 else AudioDirection.RIGHT)
        self.diagnostics.update(signal_valid=True,reason="ear_pair",raw_azimuth_deg=angle,
                                temporal_stability=stability,azimuth_std_deg=std,angle_calibrated=False)
        # The first candidate is inspectable but is not actionable evidence.
        actionable_confidence = confidence if stability >= .5 else 0.
        return AudioEstimate(direction, actionable_confidence, delay, energy, self._angle,
                             speech, confidence, ratio, "speech_like",azimuth_std_deg=std,temporal_stability=stability)
