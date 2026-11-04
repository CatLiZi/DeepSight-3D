"""Independent bounded-context VAD recovery; the primary pipeline is preserved.

Only real current PCM is used. This is not a speech timer or a second direction
guess. The auxiliary lane must pass stricter voice, raw acoustic and fresh
confirmation checks before it can fill a missing primary estimate.
"""
from dataclasses import replace
import math
import numpy as np
from scipy.ndimage import uniform_filter1d

from .ear_localization import EarPairLocalizer
from .pair_fusion import FourMicLocalizer,usable
from .speech_vad import StreamingSpeechVAD,update_window_probability


def spectral_shape_change(window,channels):
    """Gain-invariant variation, not a speech classifier or pitch cutoff."""
    values=np.asarray(window,dtype=float)
    if (values.ndim!=2 or values.shape[1]!=4 or len(values)<4096
            or not np.isfinite(values).all()):return 0.
    x=values[:,tuple(channels)]
    frames=np.lib.stride_tricks.sliding_window_view(x,1024,axis=0)[::512]
    frames=frames-frames.mean(-1,keepdims=True)
    spectrum=np.fft.rfft(frames*np.hanning(1024),n=2048,axis=-1)
    power=np.mean(np.abs(spectrum)**2,axis=0)
    cross=np.mean(spectrum[:,1]*np.conj(spectrum[:,0]),axis=0)
    coherence=np.clip(np.abs(cross)**2/np.maximum(power[0]*power[1],1e-30),0,1)
    frequencies=np.fft.rfftfreq(2048,1/48000)
    band=(frequencies>=100)&(frequencies<=4500)
    # Diffuse noise must not make a stationary common tone appear phonetic.
    # Average across independent short frames before measuring coherence.
    weights=(coherence>=.6)*coherence**2
    magnitude=np.abs(spectrum[:,:,band])*weights[None,None,band]
    # A ~117 Hz spectral envelope suppresses phase/leakage changes caused by
    # amplitude modulation; actual formant/pitch changes are still observable.
    magnitude=uniform_filter1d(magnitude,size=5,axis=-1,mode='nearest')
    variation=[]
    for channel in range(2):
        a=magnitude[:,channel];level=np.linalg.norm(a,axis=1)
        a=a[level>=max(1e-12,float(np.median(level))*.3)]
        if len(a)<4:return 0.
        norm=a/np.maximum(np.linalg.norm(a,axis=1,keepdims=True),1e-20)
        distances=np.clip(1-norm@norm.T,0,1)
        variation.append(float(np.median(distances[np.triu_indices(len(a),3)])))
    return min(variation)


class BoundedContextLocalizer:
    def __init__(self,baseline,*,vad_factory=None):
        self.baseline=self.selected=baseline
        self.policy=baseline.policy
        self.sample_rate=baseline.sample_rate
        if self.sample_rate!=48000:raise ValueError('Context recovery requires the four-mic 48 kHz stream')
        self.recovery_policy=replace(self.policy,speech=self.policy.context_recovery_speech)
        self.recovery=FourMicLocalizer(EarPairLocalizer(baseline.ear_config,self.sample_rate,
            protect_uncertain_noise=True,policy=self.recovery_policy),baseline.body_config,self.sample_rate)
        self.recovery.assisted_ears.protect_uncertain_noise=True
        self.recovery.body.protect_uncertain_noise=True
        factory=vad_factory or StreamingSpeechVAD
        self.voices=[factory(sample_rate=self.sample_rate) for _ in range(2)]
        self.alternate_voices=[factory(sample_rate=self.sample_rate) for _ in range(2)]
        self.renewals=0;self.recovered=0;self.preserved=0
        self._epoch=None;self._alternate_epoch=None;self._last_end=None
        self._reset_confirmation()
        self._scores=[0.,0.];self._variation=0.;self._state='waiting'
        self.output_speech_probability=None;self.used_for_output=False
        self.diagnostics=dict(baseline.diagnostics)

    def __getattr__(self,name):return getattr(self.selected,name)

    def _reset_confirmation(self):
        self._hits=0;self._first_time=None;self._last_time=None;self._last_angle=None

    def reset_voice_context(self):
        for voice in self.voices+self.alternate_voices:voice.reset()
        self._epoch=None;self._alternate_epoch=None;self._last_end=None;self._scores=[0.,0.]
        self.output_speech_probability=None
        self._reset_confirmation();self._state='reset'

    def _unknown(self,reason):
        self.reset_voice_context();self.recovery._unknown(reason)
        self.selected=self.baseline;self.used_for_output=False
        result=self.baseline._unknown(reason)
        self.diagnostics=dict(self.baseline.diagnostics)
        return result

    def _confirmed(self,estimate,end):
        now=end/self.sample_rate
        if (self._last_time is not None and
                (now-self._last_time>.2 or abs(estimate.azimuth_deg-self._last_angle)>12)):
            self._reset_confirmation()
        if self._first_time is None:self._first_time=now
        self._hits+=1;self._last_time=now;self._last_angle=estimate.azimuth_deg
        return self._hits>=max(3,self.policy.required_hits) and now-self._first_time>=self.policy.context_recovery_confirmation_s-1e-9

    def estimate(self,pcm,*,speech_probability=None,body_speech_probability=None,**kwargs):
        original=self.baseline.estimate(pcm,speech_probability=speech_probability,
            body_speech_probability=body_speech_probability,**kwargs)
        self.selected=self.baseline;self.used_for_output=False
        if usable(original,self.policy):self.preserved+=1
        self.output_speech_probability=(body_speech_probability
            if self.baseline.fusion.get('status')=='body_fallback' else self.baseline.last_speech_probability)
        self._state='primary';self._variation=0.
        values=np.asarray(pcm);end=kwargs.get('sample_end')
        valid=(values.ndim==2 and values.shape[1]==4 and np.issubdtype(values.dtype,np.number)
            and len(values)>=self.sample_rate*.02 and np.isfinite(values).all()
            and isinstance(end,(int,np.integer)) and not isinstance(end,bool) and end>=len(values))
        if not valid:
            self.reset_voice_context();self._state='invalid_pcm_clock'
        elif self._last_end is not None and end<=self._last_end:
            self._state='duplicate_or_rewound_pcm';self._scores=[0.,0.];self._reset_confirmation()
            if end<self._last_end:
                self.reset_voice_context();self.recovery._unknown('audio_restarted')
        elif not all(voice.session is not None for voice in self.voices+self.alternate_voices):
            self._state='model_unavailable';self._scores=[0.,0.];self._reset_confirmation()
        else:
            begin=end-len(values)
            if self._last_end is not None and begin>self._last_end:
                self.reset_voice_context();self.recovery._unknown('audio_gap')
            period=round(self.policy.context_recovery_period_s*self.sample_rate)
            epoch=end//period
            alternate_epoch=(end+period//2)//period
            if self._epoch is not None and epoch!=self._epoch:
                for voice in self.voices:voice.reset()
                self.renewals+=1
            if self._alternate_epoch is not None and alternate_epoch!=self._alternate_epoch:
                for voice in self.alternate_voices:voice.reset()
                self.renewals+=1
            self._alternate_epoch=alternate_epoch
            self._epoch=epoch;self._last_end=end
            scores=[update_window_probability(voice,values[:,np.asarray(config.selected)-1],sample_start=begin)
                for voice,config in zip(self.voices,(self.recovery.ear_config,self.recovery.body_config))]
            alternate=[update_window_probability(voice,values[:,np.asarray(config.selected)-1],sample_start=begin)
                for voice,config in zip(self.alternate_voices,(self.recovery.ear_config,self.recovery.body_config))]
            if any(p is None or not math.isfinite(p) or not 0<=p<=1 for p in scores+alternate):
                self._scores=[0.,0.];self._state='model_unavailable';self._reset_confirmation()
            else:
                scores=[max(a,b) for a,b in zip(scores,alternate)]
                self._scores=scores
                recovery_kwargs={k:v for k,v in kwargs.items() if k not in
                    ('instantaneous_speech_probability','instantaneous_body_probability')}
                candidate=self.recovery.estimate(values,speech_probability=scores[0],body_speech_probability=scores[1],**recovery_kwargs)
                if usable(original,self.policy):
                    self._reset_confirmation()
                elif usable(candidate,self.recovery_policy):
                    config=(self.recovery.body_config if self.recovery.fusion.get('status')=='body_fallback'
                            else self.recovery.ear_config)
                    self._variation=spectral_shape_change(values,np.asarray(config.selected)-1)
                    if self._variation<self.policy.context_recovery_shape_change:
                        self._state='stationary_spectrum';self._reset_confirmation()
                    elif self._confirmed(candidate,end):
                        self.selected=self.recovery;self.used_for_output=True;self._state='recovered'
                        self.output_speech_probability=candidate.speech_confidence
                        self.recovered+=1;original=candidate
                    else:self._state='confirming_fresh_recovery'
                else:
                    self._reset_confirmation();self._state=('voice_below_recovery_requirement'
                        if max(scores)<self.recovery_policy.speech else candidate.noise_state)
        self.diagnostics={**self.selected.diagnostics,'signal_valid':usable(original,self.policy)}
        return original

    def metrics(self):
        return {**self.selected.metrics(),'context_recovery':{
            'state':self._state,'selected':'bounded_context' if self.used_for_output else 'primary',
            'voice_support':max(self._scores),'ear_voice_support':self._scores[0],'body_voice_support':self._scores[1],
            'speech_requirement':self.recovery_policy.speech,'renewal_period_s':self.policy.context_recovery_period_s,
            'context_groups':2,'context_offset_s':self.policy.context_recovery_period_s/2,
            'renewals':self.renewals,'fresh_hits':self._hits,'required_hits':max(3,self.policy.required_hits),
            'minimum_confirmation_s':self.policy.context_recovery_confirmation_s,
            'shape_change':self._variation,'minimum_shape_change':self.policy.context_recovery_shape_change,
            'shape_filter':{'coherence_minimum':.6,'spectral_envelope_bins':5,'band_hz':[100,4500]},
            'recovered_windows':self.recovered,'preserved_primary_windows':self.preserved,
            'model_available':all(v.session is not None for v in self.voices+self.alternate_voices),
            'rule':'independent same-model lane; stricter voice, changing spectral shape, raw pair agreement and fresh confirmation',
            'probability_calibrated':False,'raw_pcm_unchanged':True}}
