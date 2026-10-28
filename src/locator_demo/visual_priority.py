"""Visual target selection with an optional, finite acoustic search sector.

Face area is only a proximity proxy. Audio nominates visible candidates; it
never provides the final visual steering coordinate or a metric distance.
"""
from dataclasses import replace
import math

from .av_selector import AudioVisualSelector


class VisualPrioritySelector(AudioVisualSelector):
    def __init__(self, lost_timeout=.7, sector_half_width_deg=15.):
        super().__init__(lost_timeout)
        self.sector_half_width_deg=max(5.,min(30.,float(sector_half_width_deg)))
        self.challenger=None
        self.challenger_since=0.

    @staticmethod
    def _visible_area(target, size):
        coordinates=(target.x1,target.y1,target.x2,target.y2,target.score)
        if not all(math.isfinite(v) for v in coordinates) or target.score<.35:return 0.
        width=max(0.,min(size[0],target.x2)-max(0.,target.x1))
        height=max(0.,min(size[1],target.y2)-max(0.,target.y1))
        return width*height if min(width,height)>=12 else 0.

    def _rank_candidates(self,representatives,areas,*,size,audio,valid):
        """Association scores, not speaker probabilities or acoustic ranges."""
        maximum=max(areas.values(),default=1.)
        rows=[]
        for gid,target in representatives.items():
            frontal=target.frontal_score if math.isfinite(target.frontal_score) else 0.
            quality=max(0.,min(1.,.7*target.score+.3*frontal))
            near=math.sqrt(areas[gid]/maximum)
            row=dict(person_id=gid,face_area_px=areas[gid],relative_size_score=near,
                     visual_quality_score=quality,continuity_score=float(gid==self.person),
                     visual_score=.7*near+.3*quality,in_sector=False,angular_gap_deg=None,
                     center_gap_deg=None,azimuth_left_deg=None,face_interval_deg=None,
                     angular_score=0.,match_score=0.,score_mode='visual_only',bearing_source=None)
            bearing=self._bearing(target,size)
            center=bearing.get('azimuth_left_deg');interval=bearing.get('azimuth_interval_deg',[])
            if (isinstance(center,(int,float)) and math.isfinite(center) and len(interval)==2
                    and all(isinstance(v,(int,float)) and math.isfinite(v) for v in interval)):
                lo,hi=sorted(interval)
                if -90.<=lo<=hi<=90. and -90.<=center<=90.:
                    row.update(azimuth_left_deg=center,face_interval_deg=[lo,hi],bearing_source=bearing.get('source'))
                    if valid:
                        gap=max(0.,lo-audio.azimuth_deg,audio.azimuth_deg-hi)
                        center_gap=abs(center-audio.azimuth_deg)
                        sigma=bearing.get('sigma_deg',18.)
                        if not isinstance(sigma,(int,float)) or not math.isfinite(sigma):sigma=18.
                        sigma=max(self.sector_half_width_deg,math.sqrt(max(4.,audio.azimuth_std_deg)**2+max(4.,sigma)**2+6.**2))
                        angular=.6*math.exp(-.5*(gap/sigma)**2)+.4*math.exp(-.5*(center_gap/sigma)**2)
                        row.update(angular_gap_deg=gap,center_gap_deg=center_gap,
                                   in_sector=gap<=self.sector_half_width_deg,angular_score=angular)
            rows.append(row)
        eligible=[r for r in rows if r['in_sector']] if valid else []
        if eligible:
            maximum=max(r['face_area_px'] for r in eligible)
            for row in rows:
                row['score_mode']='audio_visual'
                if row['in_sector']:
                    row['relative_size_score']=math.sqrt(row['face_area_px']/maximum)
                    row['match_score']=(.52*row['angular_score']+.30*row['relative_size_score']+
                        .13*row['visual_quality_score']+.05*row['continuity_score'])
        else:
            for row in rows:row['match_score']=row['visual_score']
        rows.sort(key=lambda r:(r['match_score'],r['visual_score']),reverse=True)
        for rank,row in enumerate(rows,1):row['rank']=rank
        ranked=[r for r in rows if r['in_sector']]
        proposed=ranked[0]['person_id'] if ranked else None
        margin=ranked[0]['match_score']-ranked[1]['match_score'] if len(ranked)>1 else None
        reason='visible_faces_in_audio_sector' if ranked else 'visual_audio_conflict' if valid else None
        if margin is not None and margin<.035 and proposed!=self.person:
            proposed=None;reason='audio_visual_score_ambiguous'
        return rows,proposed,reason,margin

    def update(self, people, *, now, size, audio=None, frame_time=0.):
        observed={gid:[t for t in obs if self._visible_area(t,size)>0] for gid,obs in people.items()}
        observed={gid:obs for gid,obs in observed.items() if obs}
        reassociated=self._reassociate(observed,now)
        # A person seen by two eyes is counted once, not as twice the area.
        representatives={gid:max(obs,key=lambda t:(self._visible_area(t,size),t.score)) for gid,obs in observed.items()}
        areas={gid:self._visible_area(t,size) for gid,t in representatives.items()}
        largest=max(observed,key=lambda gid:(areas[gid],representatives[gid].score),default=None)
        valid,reason,age=self._audio_validity(audio,now,frame_time)
        fresh_audio=False
        if valid:
            valid,fresh_audio=self._admit_audio_observation(audio,now)
            if not valid:reason='replayed_audio_observation'
        sector=([max(-90.,audio.azimuth_deg-self.sector_half_width_deg),
                 min(90.,audio.azimuth_deg+self.sector_half_width_deg)] if valid else None)
        candidates,proposed,ranking_reason,score_margin=self._rank_candidates(
            representatives,areas,size=size,audio=audio,valid=valid)
        if ranking_reason:reason=ranking_reason
        confirmed=self._confirm_audio_candidate(proposed,audio,now,fresh_audio)
        winner=self.person;mode='visual_priority';switch_reason='visual_lock_retained'
        retain_speaker=self.lock_source=='audio_sector' and audio is not None
        if self.person in observed:
            self.last_seen=now;candidate=None
            if confirmed:
                candidate=proposed;switch_reason='confirmed_visible_audio_sector'
            elif retain_speaker:
                switch_reason='visible_speaker_retained_during_audio_gap'
            elif largest!=self.person and areas[largest]>=areas[self.person]*1.25:
                candidate=largest;switch_reason='sustained_larger_face'
            if candidate is not None and candidate!=self.person:
                if candidate!=self.challenger:self.challenger=candidate;self.challenger_since=now
                if now-self.challenger_since>=.35 and now-self.locked_at>=.6:winner=candidate
            else:self.challenger=None
            if confirmed and winner==proposed:mode='audio_sector_visual'
        elif self.person is not None and now-self.last_seen<self.lost_timeout:
            winner=None;mode='visual_reacquire_wait';switch_reason='brief_visual_loss'
            self.challenger=None
        else:
            winner=proposed if confirmed else largest
            mode='audio_sector_visual' if confirmed and winner is not None else 'visual_priority'
            switch_reason='visible_audio_sector' if confirmed else 'largest_reliable_face'
            self.challenger=None
        if winner is not None:
            if winner!=self.person:
                self.locked_at=now;self.last_observations={}
                self.lock_source='audio_sector' if mode=='audio_sector_visual' else 'visual'
            elif confirmed and winner==proposed:
                self.lock_source='audio_sector'
            elif audio is None:
                # None means that the caller explicitly disabled assistance;
                # an enabled but silent input is an UNKNOWN observation.
                self.lock_source='visual'
            self.person=winner;self.last_seen=now
            for target in observed[winner]:self.last_observations[target.source_eye]=(now,target)
        elif not observed and now-self.last_seen>=self.lost_timeout:self.person=None
        self.metrics=dict(policy='visual_priority',mode=mode,selected_person_id=winner,locked_person_id=self.person,
            largest_visible_person_id=largest,visual_basis='apparent_face_area_not_metric_distance',
            switch_reason=switch_reason,audio_usable=valid,audio_reason=reason,audio_age_s=age,
            candidate_person_id=proposed,audio_confirmation_hits=self.audio_hits,candidates=candidates,
            score_kind='association_match_not_speaker_probability',score_margin=score_margin,
            score_weights=dict(direction=.52,relative_face_size=.30,visual_quality=.13,continuity=.05),
            range_basis='relative_face_size_only_no_acoustic_range',acoustic_distance_available=False,
            sector_half_width_deg=self.sector_half_width_deg,sector_interval_deg=sector,
            azimuth_left_deg=audio.azimuth_deg if valid else None,continuity_reassociated=reassociated,
            front_back_resolved=False,speaker_identity_confirmed=False,absolute_angle_accuracy_verified=False,
            steering_source='fresh_visual_observation',metric_distance_enabled=False)
        return winner


def progressive_visual_step(logic, current, error_x, error_y, limits, *, measured=None):
    """Bound each fresh image correction; caller replans the continuous trajectory.

    Coarse / medium / fine bounds shrink as the image error falls. A raw image
    crossing the centre brakes the filtered command instead of following lag.
    All units are servo ticks, never an invented distance in metres.
    """
    effective=[];stages=[];caps=[]
    for index,(error,axis) in enumerate(((error_x,'yaw'),(error_y,'pitch'))):
        band=float(limits[axis+'_deadband']);maximum=int(limits['max_'+axis+'_delta'])
        raw=error if measured is None else measured[index]
        if not math.isfinite(error) or not math.isfinite(raw) or abs(error)<=band or abs(raw)<=band or raw*error<0:
            value=0.;stage='hold';cap=1
        else:
            value=math.copysign(min(abs(error),abs(raw)),error)
            if abs(value)>=max(.25,band*3):stage='coarse';fraction=1.
            elif abs(value)>=max(.10,band*2):stage='medium';fraction=.4
            else:stage='fine';fraction=.12
            cap=max(1,round(maximum*fraction))
        effective.append(value);stages.append(stage);caps.append(cap)
    # Image x is positive to the right; yaw_left_sign describes a LEFT jog.
    # Keep the new feedback path consistent with manual left/right controls.
    pose=logic.apply_visual_error(current,effective[0],effective[1],yaw_deadband=float(limits['yaw_deadband']),
        pitch_deadband=float(limits['pitch_deadband']),max_yaw_delta=caps[0],max_pitch_delta=caps[1],
        min_yaw_delta=min(int(limits['min_yaw_delta']),caps[0]),
        min_pitch_delta=min(int(limits['min_pitch_delta']),caps[1]))
    return pose,dict(mode='progressive_image_feedback',yaw_stage=stages[0],pitch_stage=stages[1],
        yaw_step_cap=caps[0],pitch_step_cap=caps[1],effective_error=effective,
        metric_distance_used=False,requires_new_visual_observation=True)
