"""Four-channel localization with separate moving-head and fixed-body pairs.

Cross-channel spectral coherence does not need a microphone pose transform.
Body azimuth does: only a validated pose context may turn it into a head ray.
No waveform averaging, guessed servo-degree mapping or fixed 4-mic geometry.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import math

import numpy as np

from .audio import AudioDirection, AudioEstimate
from .ear_localization import EarPairLocalizer
from .audio_policy import DEFAULT_AUDIO_POLICY


def usable(estimate,policy=DEFAULT_AUDIO_POLICY):
    return (estimate.direction != AudioDirection.UNKNOWN and not estimate.motor_suppressed
            and all(math.isfinite(v) for v in (estimate.azimuth_deg, estimate.confidence,
                estimate.doa_confidence, estimate.speech_confidence, estimate.azimuth_std_deg))
            and -90 <= estimate.azimuth_deg <= 90 and estimate.confidence >= policy.confidence
            and estimate.doa_confidence >= policy.fusion_doa and estimate.speech_confidence >= policy.speech
            and estimate.temporal_stability >= policy.stability and 1 <= estimate.azimuth_std_deg <= policy.max_spread_deg)


def common_spectral_support(pcm, ears, body, *, sample_rate=48000, nfft=4096):
    """Bounded, phase-independent reliability weights from all four sensors.

    Independent people/noise in the body pair must not grant speech permission
    to the ears. Require both body channels to share a coherent source with an
    ear, using averaged STFT covariance, not single-FFT 'coherence'.
    """
    values = np.asarray(pcm)
    invalid = {'shared_source_coherence':0., 'support_available':False}
    if values.ndim != 2 or values.shape[1] != 4 or len(values) < 1024:
        return None, invalid
    channels = ears.selected + body.selected
    if len(channels) != 4 or len(set(channels)) != 4:
        return None, invalid
    values = values[:, np.asarray(channels)-1].astype(np.float64) / 32768.
    if not np.isfinite(values).all():
        return None, invalid
    frames = np.lib.stride_tricks.sliding_window_view(values, 1024, axis=0)[::512]
    if len(frames)<4:
        return None, invalid
    frames = frames - frames.mean(axis=-1, keepdims=True)
    window = np.hanning(1024)
    spectra = np.fft.rfft(frames * window / math.sqrt(float(window@window)), n=nfft, axis=-1)
    power = np.mean(np.abs(spectra)**2, axis=0)
    def coherence(a,b):
        cross = np.mean(spectra[:,a]*np.conj(spectra[:,b]), axis=0)
        return np.clip(np.abs(cross)**2 / np.maximum(power[a]*power[b],1e-30),0,1)
    shared = np.minimum(np.maximum(coherence(0,2),coherence(1,2)),
                        np.maximum(coherence(0,3),coherence(1,3)))
    shared *= np.sqrt(coherence(2,3))
    freq = np.fft.rfftfreq(nfft, 1/sample_rate)
    band = (freq>=250)&(freq<=4500)
    energy = np.sqrt(np.maximum(power[:2].sum(axis=0)*power[2:].sum(axis=0),0))*band
    score = float(shared@energy/max(float(energy.sum()),1e-30))
    if not math.isfinite(score) or score < .55:
        return None, {**invalid,'shared_source_coherence':score}
    weights = .75 + .5*np.clip((shared-.2)/.7,0,1)
    return weights, {'shared_source_coherence':score,'support_available':True,
                     'frequency_weight_bounds':[.75,1.25]}


def combine_pair_directions(ear, body, *, head_yaw_deg=None, pose_uncertainty_deg=12.,policy=DEFAULT_AUDIO_POLICY):
    """Approximate direction consensus; never an exact 3-D or distance result."""
    info = {'body_direction_used':False,'body_weight':0.,'output_frame':'head',
            'status':'ear_fallback' if usable(ear,policy) else 'no_head_direction',
            'reason':'head_body_pose_unavailable','metric_distance':False}
    if head_yaw_deg is None or not math.isfinite(head_yaw_deg) or abs(head_yaw_deg)>180:
        return ear, info
    if not usable(body,policy):
        return ear, {**info,'reason':'body_direction_unreliable'}
    angle = body.azimuth_deg-head_yaw_deg
    if not -85 <= angle <= 85:
        return ear, {**info,'reason':'body_outside_head_front'}
    body_std = math.hypot(body.azimuth_std_deg, pose_uncertainty_deg)
    if not usable(ear,policy):
        result = replace(body, azimuth_deg=angle, azimuth_std_deg=max(18.,body_std),
                         confidence=min(.6,body.confidence*.7),
                         direction=AudioDirection.CENTER if abs(angle)<=8 else
                         AudioDirection.LEFT if angle>0 else AudioDirection.RIGHT,
                         noise_state='body_pair_fallback')
        return result, {**info,'status':'body_fallback','reason':'body_in_head_frame',
                        'body_direction_used':True,'body_weight':1.,'body_head_azimuth_deg':angle}
    separation = abs(angle-ear.azimuth_deg)
    gate = min(25., math.hypot(ear.azimuth_std_deg,body_std)*1.5)
    if separation > gate:
        return ear, {**info,'status':'pair_conflict','reason':'competing_directions_not_averaged',
                     'separation_deg':separation,'body_head_azimuth_deg':angle}
    ew = ear.confidence/max(ear.azimuth_std_deg,6.)**2
    bw = body.confidence/max(body_std,12.)**2
    weight = min(.35, bw/max(ew+bw,1e-20))
    angle = (1-weight)*ear.azimuth_deg+weight*angle
    result = replace(ear, azimuth_deg=angle,
                     direction=AudioDirection.CENTER if abs(angle)<=8 else
                     AudioDirection.LEFT if angle>0 else AudioDirection.RIGHT,
                     azimuth_std_deg=max(ear.azimuth_std_deg, separation*.5),
                     confidence=min(.98, ear.confidence+.05*weight*body.confidence))
    return result, {**info,'status':'pair_consensus','reason':'common_frame_consensus',
                    'body_direction_used':True,'body_weight':weight,'separation_deg':separation,
                    'body_head_azimuth_deg':body.azimuth_deg-head_yaw_deg}


class FourMicLocalizer:
    def __init__(self, ear_localizer, body_config, sample_rate=48000):
        if not ear_localizer.config.ready or not body_config.ready:
            raise ValueError('Both microphone pair mappings must be confirmed')
        if len(set(ear_localizer.config.selected+body_config.selected))!=4:
            raise ValueError('Ear and body pairs must cover four distinct channels')
        self.ears = ear_localizer
        self.policy=ear_localizer.policy
        self.assisted_ears = EarPairLocalizer(ear_localizer.config,sample_rate,policy=self.policy)
        self.body = EarPairLocalizer(body_config,sample_rate,policy=self.policy)
        self.sample_rate = sample_rate
        self.analysis_window_s = self.ears.analysis_window_s
        self.ear_config = self.ears.config
        self.body_config = body_config
        self.last_ear = self.last_body = AudioEstimate(AudioDirection.UNKNOWN,0,0,0)
        self.diagnostics = {'signal_valid':False,'reason':'awaiting_four_mic_audio'}
        self.fusion = {'status':'waiting','body_direction_used':False,'body_spectral_used':False,
                       'body_voice_used':False,'output_frame':'head'}
        self.last_speech_probability = None
        self._body_voice_active = False
        self._assisted_selected = False

    def _usable(self,estimate):
        return usable(estimate,self.policy)

    def _unknown(self, reason):
        self.last_ear = self.ears._unknown(reason)
        self.assisted_ears._unknown(reason)
        self.last_body = self.body._unknown(reason)
        self.fusion = {'status':reason,'body_direction_used':False,'body_spectral_used':False,
                       'body_voice_used':False,'output_frame':'head'}
        self.last_speech_probability = None
        self._body_voice_active = False
        self._assisted_selected = False
        self.diagnostics = {'signal_valid':False,'reason':reason}
        return self.last_ear

    def estimate(self, pcm, *, speech_probability=None, body_speech_probability=None,
                 sample_end=None, pose=None, observed_monotonic=None):
        self.last_body = self.body.estimate(pcm,speech_probability=body_speech_probability,
                                            sample_end=sample_end)
        body_candidate = (self.last_body.direction != AudioDirection.UNKNOWN
                          and self.last_body.doa_confidence >= self.policy.fusion_doa
                          and self.last_body.speech_confidence >= self.policy.speech)
        support, detail = (common_spectral_support(pcm,self.ear_config,self.body_config,
                                                   sample_rate=self.sample_rate)
                           if body_candidate else
                           (None,{'support_available':False,'shared_source_coherence':0.}))
        # An unrelated body voice or rubbing cannot open the ear VAD gate.
        use_body_voice = (support is not None and body_speech_probability is not None
                          and math.isfinite(body_speech_probability)
                          and body_speech_probability>=self.policy.speech
                          and (speech_probability is None or body_speech_probability>speech_probability))
        probability = body_speech_probability if use_body_voice else speech_probability
        if use_body_voice and not self._body_voice_active:
            # The ear-only VAD may have called the same coherent speech noise
            # before the body VAD confirmed it. Do not subtract that onset as a
            # stationary noise reference for the rest of the utterance.
            self.assisted_ears.spectral.reset()
        self._body_voice_active = use_body_voice
        baseline = self.ears.estimate(pcm,speech_probability=speech_probability,sample_end=sample_end)
        assisted = self.assisted_ears.estimate(pcm,speech_probability=probability,
                                              sample_end=sample_end,spectral_support=support)
        assisted_used = False
        self.last_ear = baseline
        if support is not None and self._usable(assisted):
            if self._usable(baseline):
                if abs(assisted.azimuth_deg-baseline.azimuth_deg)<=3.:
                    # Both independent histories agree. Keep baseline admission
                    # confidence and temporal guarantees; refine only the ray.
                    self.last_ear=replace(baseline,azimuth_deg=assisted.azimuth_deg,
                        tdoa_s=assisted.tdoa_s,direction=assisted.direction,
                        azimuth_std_deg=max(baseline.azimuth_std_deg,assisted.azimuth_std_deg))
                    assisted_used=True
            else:
                self.last_ear=assisted;assisted_used=True
        self.last_speech_probability = probability if assisted_used and not self._usable(baseline) else speech_probability
        self._assisted_selected = assisted_used
        yaw = None;pose_uncertainty=12.
        if isinstance(pose,dict):
            stamp=pose.get('observed_monotonic'); angle=pose.get('head_yaw_deg')
            if (isinstance(stamp,(float,int)) and isinstance(angle,(float,int))
                    and not isinstance(angle,bool) and math.isfinite(stamp) and math.isfinite(angle)
                    and observed_monotonic is not None and 0<=observed_monotonic-stamp<=1.5
                    and pose.get('valid') is True):
                yaw=float(angle)
                uncertainty=pose.get('pose_uncertainty_deg',12.)
                if (isinstance(uncertainty,bool) or not isinstance(uncertainty,(float,int))
                        or not math.isfinite(uncertainty) or not 0<=uncertainty<=25):
                    yaw=None
                else:pose_uncertainty=max(12.,float(uncertainty))
        body_for_direction = self.last_body
        if not self._usable(self.last_ear) and (body_speech_probability is None or body_speech_probability<self.policy.speech):
            # Spectral coherence alone cannot declare a new body-only speaker
            # when its neural voice detector is disabled or unavailable.
            body_for_direction=replace(self.last_body,direction=AudioDirection.UNKNOWN)
        result, info = combine_pair_directions(self.last_ear,body_for_direction,head_yaw_deg=yaw,
                                               pose_uncertainty_deg=pose_uncertainty,policy=self.policy)
        if info['body_direction_used']:
            # Keep the common AudioEstimate contract: a head-pair-equivalent
            # delay, explicitly labelled rather than a body delay in ear units.
            result=replace(result,tdoa_s=math.sin(math.radians(result.azimuth_deg))*self.ear_config.spacing_m/343.)
        self.fusion = {**info,**detail,
            'body_spectral_used':assisted_used and bool(self.assisted_ears.diagnostics.get('body_spectral_support_used')),
            'body_voice_used':(use_body_voice and assisted_used and not self._usable(baseline)) or info['status']=='body_fallback',
            'baseline_ear_preserved':self._usable(baseline),
            'pose_basis':pose.get('basis') if yaw is not None else None,
            'head_yaw_deg':yaw,'pose_uncertainty_deg':pose_uncertainty if yaw is not None else None,
            'pose_time_aligned':bool(pose and pose.get('time_aligned')) if yaw is not None else False,
            'body_head_azimuth_deg':info.get('body_head_azimuth_deg'),
            'tdoa_kind':'head_pair_equivalent' if info['body_direction_used'] else 'ear_pair_measured',
            'cross_head_body_tdoa_used':False,'physical_4mic_geometry_calibrated':False}
        selected_diagnostics=self.assisted_ears.diagnostics if assisted_used else self.ears.diagnostics
        self.diagnostics = {**selected_diagnostics,'signal_valid':self._usable(result),
                            'reason':info['status'] if self._usable(result) else result.noise_state,
                            'method':'four_mic_pair_spectral_consensus'}
        return result

    def metrics(self):
        def row(estimate, localizer, frame):
            return {**asdict(estimate),'direction':estimate.direction.value,
                    'frame':frame,'channels':localizer.config.selected,
                    'spacing_m':localizer.config.spacing_m,'usable':self._usable(estimate),
                    'diagnostics':dict(localizer.diagnostics)}
        return {'fusion':dict(self.fusion),
                'pairs':{'ears':row(self.last_ear,self.assisted_ears if self._assisted_selected else self.ears,'head'),
                         'body':row(self.last_body,self.body,'body')}}
