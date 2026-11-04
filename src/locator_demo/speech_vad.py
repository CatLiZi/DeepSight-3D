"""Local streaming Silero ONNX VAD, with explicit spectral-only fallback.

Protocol follows snakers4/silero-vad OnnxWrapper (MIT), pinned acquisition
metadata and license are kept alongside the local model. No network calls.
"""
from __future__ import annotations
from pathlib import Path
from collections import deque
import time
import numpy as np
from scipy.signal import firwin,lfilter


class StreamingSpeechVAD:
    def __init__(self,model_path=None,sample_rate=48000):
        self.sample_rate=sample_rate
        self.model_path=Path(model_path or Path(__file__).resolve().parents[2]/'models/silero_vad.onnx')
        self.session=None;self.error='';self.calls=0;self.total_ms=0.
        self.factor=sample_rate//16000
        if self.factor<1 or sample_rate%16000:raise ValueError('Speech VAD expects a multiple of 16 kHz')
        self.taps=firwin(63,.9/self.factor) if self.factor>1 else np.ones(1)
        try:
            import onnxruntime as ort
            options=ort.SessionOptions();options.inter_op_num_threads=1;options.intra_op_num_threads=1
            self.session=ort.InferenceSession(str(self.model_path),sess_options=options,providers=['CPUExecutionProvider'])
        except Exception as exc:self.error=f'{type(exc).__name__}: {exc}'
        self.reset()

    def reset(self):
        self.state=np.zeros((2,2,128),np.float32);self.context=np.zeros((2,64),np.float32)
        self.pending=np.empty((0,2),np.float32);self.last_end=None
        self.probability=0.;self.per_ear=[0.,0.];self.completed=0
        self.resample_state=np.zeros((len(self.taps)-1,2));self.resample_count=0
        self._window_evidence=deque(maxlen=64)
        self.analysis_probability=0.

    def update(self,pair_pcm,*,sample_start):
        if self.session is None:return None
        pair=np.asarray(pair_pcm,np.float32)
        if pair.ndim!=2 or pair.shape[1]!=2 or not np.isfinite(pair).all():
            self.reset();return None
        end=sample_start+len(pair)
        if self.last_end is not None and end<=self.last_end:return self.probability
        if self.last_end is not None and (sample_start>self.last_end or end-self.last_end>self.sample_rate*.3):self.reset()
        begin=0 if self.last_end is None else max(0,self.last_end-sample_start)
        fresh=pair[begin:]/32768.;self.last_end=end
        # Capture chunks are multiples of 3 at 48 kHz; raw recording is untouched.
        if self.factor>1:
            filtered,self.resample_state=lfilter(self.taps,[1.],fresh,axis=0,zi=self.resample_state)
            offset=(-self.resample_count)%self.factor
            audio=filtered[offset::self.factor].astype(np.float32);self.resample_count+=len(fresh)
        else:audio=fresh.astype(np.float32)
        self.pending=np.concatenate((self.pending,audio))
        while len(self.pending)>=512:
            chunk=self.pending[:512].T;self.pending=self.pending[512:]
            start=time.perf_counter()
            try:
                probability,self.state=self.session.run(None,{'input':np.concatenate((self.context,chunk),axis=1),
                    'state':self.state,'sr':np.array(16000,np.int64)})
            except Exception as exc:
                self.error=str(exc);self.session=None;self.reset();return None
            self.calls+=1;self.completed+=1;self.total_ms+=(time.perf_counter()-start)*1000
            self.context=chunk[:,-64:].copy();self.per_ear=np.asarray(probability).reshape(-1).tolist()
            p=float(max(self.per_ear))
            self.probability=p if self.completed==1 else .45*self.probability+.55*p
            # Timestamp each actual inference on the input PCM clock, not the
            # polling clock. The direction estimator uses a longer PCM window
            # than one neural chunk; its tail alone cannot describe that window.
            chunk_end=end-len(self.pending)*self.factor
            self._window_evidence.append((chunk_end,self.probability))
        return self.probability

    def probability_for_window(self, *, sample_start, sample_end):
        """Measured speech support inside this exact, at most 192 ms PCM window.

        This is a peak of observed chunk scores, not a new calibrated speech
        probability, silence extrapolation, or a cached direction. Invalid,
        future, stale or overlong queries cannot borrow old voice evidence.
        """
        if self.session is None:return None
        if (isinstance(sample_start,bool) or isinstance(sample_end,bool)
                or not isinstance(sample_start,(int,np.integer))
                or not isinstance(sample_end,(int,np.integer))
                or sample_start<0 or sample_end!=self.last_end
                or not 0<sample_end-sample_start<=round(.192*self.sample_rate)):
            self.analysis_probability=0.
            return 0.
        self.analysis_probability=max((p for end,p in self._window_evidence
            if sample_start<end<=sample_end),default=0.)
        return self.analysis_probability

    def metrics(self):
        return {'backend':'silero_onnx_cpu' if self.session is not None else 'spectral_fallback',
                'available':self.session is not None,'speech_probability':round(self.probability,4),
                'instantaneous_speech_probability':round(self.probability,4),
                'analysis_window_speech_support':round(self.analysis_probability,4),
                'analysis_score_kind':'maximum measured chunk score inside current PCM window, at most 192 ms',
                'per_ear_probability':self.per_ear,'inference_calls':self.calls,
                'mean_inference_ms':self.total_ms/max(1,self.calls),'completed_chunks':self.completed,
                'error':self.error,'model_path':str(self.model_path),'trained_here':False,
                'channels':'selected ears only; no body channels','output':'speech activity, not speaker identity'}


def update_window_probability(vad,pair_pcm,*,sample_start):
    probability=vad.update(pair_pcm,sample_start=sample_start)
    window=getattr(vad,'probability_for_window',None)
    return window(sample_start=sample_start,sample_end=sample_start+len(pair_pcm)) if callable(window) else probability
