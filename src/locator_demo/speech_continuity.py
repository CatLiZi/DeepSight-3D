"""Fill current-window speech gaps without replacing usable R45 evidence."""
from .ear_localization import EarPairLocalizer
from .pair_fusion import FourMicLocalizer,usable


class SpeechContinuityLocalizer:
    def __init__(self,baseline):
        self.baseline=baseline
        self.candidate=FourMicLocalizer(EarPairLocalizer(baseline.ear_config,baseline.sample_rate,
            protect_uncertain_noise=True,policy=baseline.policy),baseline.body_config,baseline.sample_rate)
        self.candidate.assisted_ears.protect_uncertain_noise=True
        self.candidate.body.protect_uncertain_noise=True
        self.selected=baseline;self.preserved=0;self.recovered=0

    def __getattr__(self,name):
        return getattr(self.selected,name)

    def _unknown(self,reason):
        self.candidate._unknown(reason)
        self.selected=self.baseline
        return self.baseline._unknown(reason)

    def estimate(self,pcm,*,speech_probability=None,body_speech_probability=None,
                 instantaneous_speech_probability=None,instantaneous_body_probability=None,**kwargs):
        # A separate noise reference and delay history reproduce the baseline.
        # Candidate resets or extra observations cannot change its output.
        baseline=self.baseline.estimate(pcm,
            speech_probability=instantaneous_speech_probability,
            body_speech_probability=instantaneous_body_probability,**kwargs)
        candidate=self.candidate.estimate(pcm,speech_probability=speech_probability,
            body_speech_probability=body_speech_probability,**kwargs)
        if usable(baseline,self.baseline.policy):
            self.selected=self.baseline;self.preserved+=1
            return baseline
        if usable(candidate,self.baseline.policy):
            self.selected=self.candidate;self.recovered+=1
            return candidate
        self.selected=self.baseline
        return baseline

    def metrics(self):
        result=self.selected.metrics()
        result['speech_continuity']={
            'selected':'current_window_recovery' if self.selected is self.candidate else 'baseline',
            'baseline_preserved_windows':self.preserved,'recovered_windows':self.recovered,
            'maximum_window_s':.192,'raw_probability_unchanged':True,
            'rule':'preserve usable baseline; recover only with fresh measured voice and current DSP evidence'}
        return result
