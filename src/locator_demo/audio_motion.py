"""Motion-aware acoustic admission, separate from actuator pacing.

Movement is context, not evidence that a measured direction is wrong. Existing
speech, direction, temporal-stability and uncertainty checks still apply. No
direction is invented from microphone level or from a visible face.
"""
from dataclasses import replace
import math
from numbers import Real

from .audio import AudioDirection


def qualify_audio_during_motion(estimate, *, active):
    direction=getattr(estimate.direction,'value',estimate.direction)
    context={'active':bool(active),'policy':'acoustic_quality_not_movement_veto',
             'admitted':direction!='unknown' and not estimate.motor_suppressed,
             'reason':'stationary' if not active else 'acoustic_unavailable',
             'scope':'motion policy only; downstream freshness and selection checks remain',
             'motion_alone_disables_audio':False}
    if not active or direction=='unknown' or estimate.motor_suppressed:
        context['reason']=estimate.noise_state if direction=='unknown' else context['reason']
        return estimate,context
    values=(estimate.speech_confidence,estimate.confidence,estimate.doa_confidence,
            estimate.temporal_stability,estimate.azimuth_deg,estimate.azimuth_std_deg)
    numeric=all(isinstance(v,Real) and not isinstance(v,bool) and math.isfinite(v) for v in values)
    probabilities=values[:4]
    rejected_states={'noise','quiet','fan_or_motor','low_band_noise','denoise_guard',
        'ambiguous_delay','low_coherence','direction_transition','stale_audio','sample_clock_mismatch','channels_nearly_identical'}
    trusted=(numeric and estimate.noise_state not in rejected_states and all(0<=v<=1 for v in probabilities)
             and estimate.speech_confidence>=.5 and min(estimate.confidence,estimate.doa_confidence)>=.2
             and estimate.temporal_stability>=.5 and abs(estimate.azimuth_deg)<=90
             and 0<=estimate.azimuth_std_deg<=25)
    if trusted:
        context.update(admitted=True,reason='reliable_voice_during_motion')
        return estimate,context
    context.update(admitted=False,reason='motion_acoustic_uncertain')
    # These are the same acoustic-quality bounds used by the visual selector.
    # A motor flag never vetoes an estimate that satisfies them.
    return replace(estimate,direction=AudioDirection.UNKNOWN,confidence=0.,
        noise_state='motion_acoustic_uncertain',motor_suppressed=True,
        speech_confidence=estimate.speech_confidence if numeric else 0.,
        doa_confidence=estimate.doa_confidence if numeric else 0.,
        azimuth_deg=estimate.azimuth_deg if numeric else 0.,
        temporal_stability=estimate.temporal_stability if numeric else 0.,
        azimuth_std_deg=estimate.azimuth_std_deg if numeric else 25.),context
