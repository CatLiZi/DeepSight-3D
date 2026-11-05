"""Sound owns the destination; vision may refine only its associated face.

The acoustic destination is a retained, pose-compensated goal, not a new sound
measurement. It is supplied by DominantSoundLock; this module never promotes
raw levels, held overlay angles or an arbitrary large face into a destination.
"""
from types import SimpleNamespace
import math

from .visual_priority import VisualPrioritySelector


class AudioPrimarySelector(VisualPrioritySelector):
    arrival_band_deg = 12.0
    acquisition_window_s = 2.0

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.acoustic_target = {}
        self.token = None
        self.arrived_at = None
        self.candidate = None
        self.candidate_since = 0.
        self.candidate_hits = 0
        self.last_frame_time = 0.
        self.conflict_since = None
        self.conflict_hits = 0

    def _release_face(self):
        self.person = None
        self.last_observations.clear()
        self.smoothed.clear()
        self.candidate = None
        self.candidate_hits = 0
        self.conflict_since = None
        self.conflict_hits = 0

    def update(self, people, *, now, size, audio=None, frame_time=0.):
        context = self.acoustic_target
        token = (context.get('epoch'), context.get('core_id'))
        if token != self.token:
            self._release_face()
            self.arrived_at = None
            self.token = token
        angle = context.get('relative_azimuth_deg')
        stamp = context.get('updated_monotonic', 0.)
        context_valid = (context.get('locked') and context.get('geometry_valid')
            and isinstance(angle, (int, float)) and math.isfinite(angle) and -90 <= angle <= 90
            and isinstance(stamp, (int, float)) and math.isfinite(stamp)
            and -.02 <= now - stamp <= .5)
        # Standalone callers can omit frame_time; the actual pipeline supplies
        # the USB capture clock. Duplicated frames cannot confirm acquisition.
        frame_stamp = frame_time or now
        fresh_frame = (math.isfinite(frame_stamp) and -.02 <= now-frame_stamp <= .35
                       and frame_stamp > self.last_frame_time)
        if fresh_frame:
            if frame_stamp-self.last_frame_time > .35:
                self.candidate = None
                self.candidate_hits = 0
            self.last_frame_time = frame_stamp
        observed = {gid: [t for t in obs if self._visible_area(t, size) > 0]
                    for gid, obs in people.items()} if fresh_frame else {}
        observed = {gid: obs for gid, obs in observed.items() if obs}
        valid_audio, audio_reason, audio_age = self._audio_validity(audio, now, frame_time)
        fresh_audio = False
        if valid_audio:
            valid_audio, fresh_audio = self._admit_audio_observation(audio, now)
        rows, proposed, _, margin = self._rank_candidates(
            {gid: max(obs, key=lambda t: self._visible_area(t, size)) for gid, obs in observed.items()},
            {gid: max(self._visible_area(t, size) for t in obs) for gid, obs in observed.items()},
            size=size, audio=SimpleNamespace(azimuth_deg=angle, azimuth_std_deg=8.), valid=bool(context_valid))
        row_by_id = {row['person_id']: row for row in rows}
        phase, reason, winner = 'waiting_sound', 'awaiting_confirmed_sound', None
        if not context_valid:
            self._release_face()
        else:
            self._reassociate(observed, now)
            # A changed confirmed core preempts immediately. Within the same
            # spatial cluster, sustained fresh disagreement can also release a
            # face; one noisy observation cannot steal the visual assist.
            conflict = (self.person in row_by_id and valid_audio
                        and context.get('live_matches_core')
                        and not row_by_id[self.person]['in_sector'])
            if conflict and fresh_audio:
                if self.conflict_since is None:
                    self.conflict_since = now
                self.conflict_hits += 1
                if self.conflict_hits >= 3 and now-self.conflict_since >= .18:
                    self._release_face()
                    self.arrived_at = None
            elif not conflict:
                self.conflict_since = None
                self.conflict_hits = 0
            if self.person in observed:
                winner = self.person
                phase, reason = 'visual_refine', 'sound_face_visual_assist'
            elif self.person is not None and now-self.last_seen < self.lost_timeout:
                phase, reason = 'face_lost', 'sound_face_temporarily_lost'
            else:
                self.person = None
                phase, reason = 'turn_to_sound', 'turning_to_confirmed_sound'
                near_goal = abs(angle) <= self.arrival_band_deg
                if near_goal:
                    if self.arrived_at is None:
                        self.arrived_at = now
                    if valid_audio and context.get('live_matches_core'):
                        self.arrived_at = now
                    # After finishing a short utterance, allow a bounded window
                    # to find its face. A minutes-old goal cannot recruit a new
                    # bystander merely because they enter the same picture.
                    can_acquire = now-self.arrived_at <= self.acquisition_window_s
                    phase, reason = 'acquire_face', 'looking_for_face_in_sound_sector'
                    if not can_acquire:
                        phase, reason = 'hold_sound', 'sound_goal_held_without_face'
                    if can_acquire and proposed is not None:
                        if proposed != self.candidate:
                            self.candidate, self.candidate_since, self.candidate_hits = proposed, now, 0
                        self.candidate_hits += 1
                        if self.candidate_hits >= 2 and now-self.candidate_since >= .1:
                            winner = proposed
                            phase, reason = 'visual_refine', 'sound_face_acquired'
                    else:
                        self.candidate = None
                        self.candidate_hits = 0
                else:
                    self.candidate = None
                    self.candidate_hits = 0
            if winner is not None:
                self.person, self.last_seen = winner, now
                self.lock_source = 'confirmed_sound'
                for target in observed[winner]:
                    self.last_observations[target.source_eye] = (now, target)
        self.metrics = dict(policy='audio_primary', mode=phase, switch_reason=reason,
            acoustic_epoch=token[0], acoustic_core_id=token[1], selected_person_id=winner,
            locked_person_id=self.person, audio_usable=valid_audio, audio_reason=audio_reason,
            audio_age_s=audio_age, candidates=rows, score_margin=margin,
            azimuth_left_deg=angle if context_valid else None,
            sector_half_width_deg=self.sector_half_width_deg,
            sector_interval_deg=[max(-90., angle-self.sector_half_width_deg),
                                 min(90., angle+self.sector_half_width_deg)] if context_valid else None,
            steering_source='fresh_visual_observation' if winner is not None else 'confirmed_acoustic_goal',
            acquisition_hits=self.candidate_hits, primary='audio', visual_assist_interruptible=True,
            acoustic_distance_available=False, metric_distance_enabled=False,
            score_kind='association_match_not_speaker_probability',
            range_basis='relative_face_size_only_no_acoustic_range')
        return winner
