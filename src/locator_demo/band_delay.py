"""Cross-check frequency-band delay evidence without choosing a new bearing.

All inputs come from the caller's existing averaged spectra. Diagnostic band
peaks are sample delays, not calibrated angles or independent observations.
"""
import math
import numpy as np
from scipy.signal import find_peaks

BANDS = ((250, 1200), (1200, 2500), (2500, 5000))


def frequency_delay_evidence(power, cross, coherence, base, frequencies, *,
                             sample_rate, nfft, maximum_lag_samples):
    weighted = cross * base
    total = float(np.abs(weighted).sum())
    extent = math.ceil(maximum_lag_samples) + 10
    lags = np.arange(-extent, extent + 1)
    result = []
    for low, high in BANDS:
        mask = (frequencies >= low) & (frequencies < high)
        spectrum = weighted * mask
        energy = float(np.abs(spectrum).sum())
        band = dict(low_hz=low, high_hz=high, lag_samples=None, qualified=False,
                    weight_fraction=energy / max(total, 1e-30), coherence=0.,
                    correlation=0., peak_ratio=0., reason='low_support')
        if energy < 1e-15:
            result.append(band)
            continue
        cc = np.fft.irfft(spectrum, n=nfft)
        scores = np.abs(cc[lags % nfft])
        peak = int(np.argmax(scores))
        lag = float(lags[peak])
        if 0 < peak < len(scores) - 1:
            a, b, c = scores[peak - 1:peak + 2]
            denominator = a - 2 * b + c
            if abs(denominator) > 1e-30:
                lag += float(np.clip(.5 * (a - c) / denominator, -.5, .5))
        if abs(lag) > maximum_lag_samples + .25:
            band['reason'] = 'outside_physical_delay'
            result.append(band)
            continue
        peaks, _ = find_peaks(scores)
        competitors = [float(scores[k]) for k in peaks
                       if abs(lags[k]) <= maximum_lag_samples + .25 and abs(lags[k] - lag) > 4]
        ratio = min(10., float(scores[peak]) / max(competitors, default=1e-30)) if competitors else 4.
        norm = math.sqrt(float(np.sum(power[0] * base * mask) * np.sum(power[1] * base * mask)))
        phase = np.exp(2j * np.pi * frequencies * lag / sample_rate)
        correlation = float(np.clip(abs(np.sum(spectrum * phase)) / max(norm, 1e-30), 0., 1.))
        coherent = float(np.sum(coherence * np.abs(spectrum)) / energy)
        qualified = bool(coherent >= .6 and correlation >= .45 and ratio >= 1.15
                         and band['weight_fraction'] >= .02)
        band.update(lag_samples=lag, qualified=qualified, coherence=coherent,
                    correlation=correlation, peak_ratio=ratio,
                    reason='qualified_band' if qualified else 'ambiguous_or_weak_band')
        result.append(band)
    return result


def frequency_delay_conflict(global_lag, bands):
    """Only two qualified, agreeing upper bands can contradict the full band."""
    if global_lag is None or len(bands) != 3:
        return False
    mid, high = bands[1:]
    return bool(mid['qualified'] and high['qualified']
                and abs(mid['lag_samples'] - high['lag_samples']) <= 2
                and min(abs(mid['lag_samples'] - global_lag),
                        abs(high['lag_samples'] - global_lag)) > 4)
