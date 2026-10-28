"""Voice-direction assisted visual lock, with visual fallback and hysteresis.

This associates an acoustic direction with visual candidates. It does not infer
speaker identity, separate simultaneous talkers, or resolve binaural front/back.
"""
from __future__ import annotations
from dataclasses import replace
import math
from numbers import Integral, Real

_AUDIO_FRESHNESS_S = .35


def _finite_number(value):
    # Reject booleans and coercible strings as well as NaN/Inf. In particular,
    # comparisons such as `nan < threshold` are false, so threshold checks on
    # their own can accidentally approve corrupt evidence.
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


class AudioVisualSelector:
    def __init__(self,lost_timeout=.7):
        self.lost_timeout=lost_timeout;self.person=None;self.last_seen=0.;self.locked_at=0.
        self.audio_candidate=None;self.audio_since=0.;self.audio_hits=0;self.last_audio_sequence=None
        self._last_audio_observation=None
        self.lock_source='visual';self.smoothed={};self.metrics={'mode':'visual_fallback'}
        self.last_observations={}

    def _reassociate(self,observed,now):
        """Recover a short detector ID reset in the same eye, not an identity.

        Preserve both eye histories across a brief one-eye interruption. A
        recent lock, very high box overlap and close angular direction must
        agree, with a unique candidate. No old coordinate is returned.
        """
        if self.person is None or self.person in observed or now-self.last_seen>.35:return False
        candidates=[]
        for gid,targets in observed.items():
            best=0.
            for target in targets:
                previous=self.last_observations.get(target.source_eye)
                if previous is None or now-previous[0]>.9:continue
                old=previous[1]
                if not old.bearing or not target.bearing:continue
                angle=abs(old.bearing['azimuth_left_deg']-target.bearing['azimuth_left_deg'])
                intersection=max(0.,min(old.x2,target.x2)-max(old.x1,target.x1))*max(0.,min(old.y2,target.y2)-max(old.y1,target.y1))
                overlap=intersection/max(1.,old.area+target.area-intersection)
                if overlap>=.70 and angle<=5.:best=max(best,overlap)
            if best:candidates.append((best,gid))
        candidates.sort(reverse=True)
        if not candidates or (len(candidates)>1 and candidates[0][0]-candidates[1][0]<.12):return False
        old=self.person;self.person=candidates[0][1]
        if old in self.smoothed:self.smoothed[self.person]=self.smoothed.pop(old)
        return True

    @staticmethod
    def _visual_value(target):
        size=min(1.,math.sqrt(max(0.,target.area))/240.)
        return .65*target.score+.2*target.frontal_score+.1*size+.05*min(1.,target.mouth_motion_score)

    @staticmethod
    def _bearing(target,size):
        if target.bearing:return target.bearing
        # Explicitly approximate fallback; no nominal FOV is treated as a calibration.
        fx=size[0]/(2*math.tan(math.radians(112/2)))
        angles=[math.degrees(math.atan2(size[0]/2-u,fx)) for u in (target.center[0],target.x1,target.x2)]
        return {'azimuth_left_deg':angles[0],'azimuth_interval_deg':sorted(angles[1:]),
                'sigma_deg':18.,'confidence':.45,'source':'nominal_fov_fallback'}

    def _audio_validity(self,audio,now,frame_time):
        if audio is None:return False,'audio_disabled',None
        observed=getattr(audio,'observed_monotonic',0.)
        if not all(_finite_number(v) for v in (now,observed,frame_time)):
            return False,'invalid_audio_video_clock',None
        age=now-observed
        if observed<=0 or not -.02<=age<=_AUDIO_FRESHNESS_S:return False,'stale_audio',age
        # Zero is the existing standalone-caller sentinel for an unspecified
        # video clock. A supplied clock must be finite, nonnegative and close.
        if frame_time<0:return False,'invalid_audio_video_clock',age
        if frame_time and abs(frame_time-observed)>.45:return False,'audio_video_time_gap',age
        if frame_time:
            # A small pair difference alone can approve two old observations,
            # or a frame timestamp in the future. This only gates association;
            # the raw sound display and the visual control clock stay separate.
            video_age=now-frame_time
            if video_age<-.02-1e-9:return False,'future_video_for_audio',age
            if video_age>_AUDIO_FRESHNESS_S+1e-9:return False,'stale_video_for_audio',age
        if audio.motor_suppressed:return False,'motor_guard',age
        if getattr(audio.direction,'value',audio.direction)=='unknown':return False,audio.noise_state,age
        probabilities=(audio.speech_confidence,audio.confidence,audio.doa_confidence,audio.temporal_stability)
        if not all(_finite_number(v) and 0.<=v<=1. for v in probabilities):
            return False,'invalid_audio_probability',age
        if audio.speech_confidence<.5:return False,'low_speech_probability',age
        if min(audio.confidence,audio.doa_confidence)<.20:return False,'low_direction_confidence',age
        if audio.temporal_stability<.5:return False,'direction_not_stable',age
        if not all(_finite_number(v) for v in (audio.azimuth_deg,audio.azimuth_std_deg)) or not -90.<=audio.azimuth_deg<=90. or not 0.<=audio.azimuth_std_deg<=25.:
            return False,'uncertain_direction',age
        if not isinstance(audio.sample_sequence,Integral) or isinstance(audio.sample_sequence,bool) or audio.sample_sequence<0:
            return False,'invalid_audio_sequence',age
        return True,'usable_direction',age

    def _clear_audio_confirmation(self,now):
        self.audio_candidate=None;self.audio_hits=0;self.audio_since=now
        self.last_audio_sequence=None

    def _admit_audio_observation(self,audio,now):
        """Return (usable, fresh); two clocks must advance for a new vote.

        A canonical cached sample may retain existing evidence, but cannot add
        a vote. A newer receive time with a reset/reused counter starts a new
        confirmation run. Replayed clocks never lower the accepted watermark.
        A gap beyond the existing freshness budget also starts a new run;
        votes from a paused or stalled stream cannot confirm a later nominee.
        This cannot identify a hardware stream; restart handling is conservative.
        """
        current=(audio.sample_sequence,audio.observed_monotonic)
        previous=self._last_audio_observation
        if previous is None or current[1]>previous[1]:
            if previous is not None and (current[0]<=previous[0] or
                    current[1]-previous[1]>_AUDIO_FRESHNESS_S+1e-9):
                self._clear_audio_confirmation(now)
            self._last_audio_observation=current
            return True,True
        if current==previous:return True,False
        self._clear_audio_confirmation(now)
        return False,False

    def _confirm_audio_candidate(self,proposed,audio,now,fresh):
        if proposed is None:self._clear_audio_confirmation(now)
        elif proposed!=self.audio_candidate:
            self.audio_candidate=proposed;self.audio_since=now;self.audio_hits=int(fresh)
            self.last_audio_sequence=audio.sample_sequence if fresh else None
        elif fresh:
            if not self.audio_hits:self.audio_since=now
            self.audio_hits+=1;self.last_audio_sequence=audio.sample_sequence
        return proposed is not None and self.audio_hits>=3 and now-self.audio_since>=.18

    def update(self,people,*,now,size,audio=None,frame_time=0.):
        observed={gid:list(obs) for gid,obs in people.items() if obs}
        reassociated=self._reassociate(observed,now)
        visual={gid:max(self._visual_value(t) for t in obs) for gid,obs in observed.items()}
        valid,reason,age=self._audio_validity(audio,now,frame_time)
        fresh_audio=False
        if valid:
            valid,fresh_audio=self._admit_audio_observation(audio,now)
            if not valid:reason='replayed_audio_observation'
        scores=[];proposed=None
        if valid:
            for gid,obs in observed.items():
                target=max(obs,key=lambda t:(bool(t.bearing and t.bearing.get('source')=='binocular_feature_rays'),self._visual_value(t)))
                bearing=self._bearing(target,size);center=bearing['azimuth_left_deg'];lo,hi=bearing['azimuth_interval_deg']
                gap=max(0.,lo-audio.azimuth_deg,audio.azimuth_deg-hi)
                sigma=math.sqrt(max(4.,audio.azimuth_std_deg)**2+bearing['sigma_deg']**2+6.**2)
                likelihood=.65*math.exp(-.5*(gap/sigma)**2)+.35*math.exp(-.5*((center-audio.azimuth_deg)/sigma)**2)
                scores.append({'person_id':gid,'visual_score':round(visual[gid],4),'azimuth_left_deg':center,
                    'angular_gap_deg':gap,'audio_likelihood':likelihood,'bearing_source':bearing['source']})
            ranked=sorted(scores,key=lambda row:row['audio_likelihood'],reverse=True)
            if ranked and ranked[0]['audio_likelihood']>=.65:
                if len(ranked)==1 or ranked[0]['audio_likelihood']-ranked[1]['audio_likelihood']>=.12:
                    proposed=ranked[0]['person_id']
                else:reason='angular_ambiguity'
            elif ranked:reason='audio_outside_visible_faces'
        audio_confirmed=self._confirm_audio_candidate(proposed,audio,now,fresh_audio)
        mode='visual_fallback';winner=self.person
        if self.person in observed:
            self.last_seen=now
            if audio_confirmed:
                if proposed==self.person:mode='audio_guided';self.lock_source='audio'
                elif now-self.audio_since>=.35 and (self.lock_source!='audio' or now-self.locked_at>=.6):
                    winner=proposed;mode='audio_guided'
            # Quiet, weak/ambiguous audio never evicts a fresh visual lock.
        elif self.person is not None and now-self.last_seen<self.lost_timeout:
            winner=None;mode='visual_reacquire_wait'
        else:
            winner=proposed if audio_confirmed else max(visual,key=visual.get,default=None)
            mode='audio_guided' if audio_confirmed and winner is not None else 'visual_fallback'
        if winner is not None:
            if winner!=self.person:self.locked_at=now;self.lock_source='audio' if mode=='audio_guided' else 'visual'
            if winner!=self.person:self.last_observations={}
            self.person=winner;self.last_seen=now
            for target in observed[winner]:self.last_observations[target.source_eye]=(now,target)
        elif not observed and now-self.last_seen>=self.lost_timeout:self.person=None
        self.metrics={'mode':mode,'selected_person_id':winner,'locked_person_id':self.person,
            'audio_usable':valid,'audio_reason':reason,'audio_age_s':age,'candidate_person_id':proposed,
            'audio_confirmation_hits':self.audio_hits,'candidates':scores,
            'azimuth_left_deg':audio.azimuth_deg if valid else None,'front_back_resolved':False,
            'speaker_identity_confirmed':False,'absolute_angle_accuracy_verified':False}
        self.metrics['continuity_reassociated']=reassociated
        return winner

    def smooth_target(self,target,now,size):
        if target is not None and target.fixation:
            f=target.fixation
            # Do not mix a monocular ray with a binocular midpoint, or two
            # different anchor kinds. The downstream smooth servo stays intact.
            key=(target.face_id,f['mode'],f['anchor_kind'],target.source_eye if f['mode']=='single_eye' else 'both')
            current=(f['virtual_u_px']/size[0],f['virtual_v_px']/size[1])
            previous=self.smoothed.get(key)
            if previous and 0<now-previous[0]<.5:
                alpha=1-math.exp(-(now-previous[0])/.10)
                current=tuple(old+alpha*(new-old) for old,new in zip(previous[1],current))
            self.smoothed={gid:value for gid,value in self.smoothed.items() if now-value[0]<1.}
            self.smoothed[key]=(now,current)
            return replace(target,fixation={**f,'virtual_u_px':current[0]*size[0],
                'virtual_v_px':current[1]*size[1],'control_filter':'time_based_ema_100ms'})
        if target is None or not target.bearing or target.bearing.get('frame') not in ('rig_optical_bisector','screen_centered_optical'):return target
        key=target.face_id;current=(target.bearing['virtual_u_px']/size[0],target.bearing['virtual_v_px']/size[1])
        previous=self.smoothed.get(key)
        if previous and 0<now-previous[0]<.5:
            elapsed=now-previous[0];alpha=1-math.exp(-elapsed/.10)
            current=tuple(old+alpha*(new-old) for old,new in zip(previous[1],current))
        self.smoothed={gid:value for gid,value in self.smoothed.items() if now-value[0]<1.}
        self.smoothed[key]=(now,current)
        bearing={**target.bearing,'virtual_u_px':current[0]*size[0],'virtual_v_px':current[1]*size[1],
                 'control_filter':'time_based_ema_100ms'}
        return replace(target,bearing=bearing)
