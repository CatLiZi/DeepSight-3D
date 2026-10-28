"""Time-weighted acoustic evidence, a persistent target, and switch hysteresis.

Scores rank reliable voiced observations; they are not speaker identity or
semantic importance probabilities. The position model uses the existing control
gain in servo units, not a measured degree-to-servo calibration. No historical
estimate is emitted as a new AudioEstimate or sent to visual speaker selection.
"""
from collections import deque
from dataclasses import dataclass
import math
from numbers import Integral, Real


def finite(value):
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


@dataclass
class SoundTrack:
    id: int
    center: float
    seen: float
    weight: float = 0.
    duration: float = 0.
    observations: int = 0


class DominantSoundLock:
    window_s = 2.5
    decay_s = 1.5
    minimum_active_s = .24
    minimum_weight_s = .08
    merge_degrees = 15.
    switch_ratio = 1.25
    switch_margin_s = .04
    challenge_active_s = .4
    challenge_dwell_s = .25
    first_active_s = .10
    first_weight_s = .035
    idle_replace_s = 1.0

    def __init__(self):
        self.reset()

    def reset(self):
        self.tracks = {}
        self.events = deque(maxlen=512)
        self.poses = deque(maxlen=512)
        self.watermark = None
        self.next_id = 1
        self.core_id = None
        self.goal_yaw = None
        self.last_goal_update = 0.
        self.challenger = None
        self.challenger_since = self.challenger_last = 0.
        self.challenger_hits = 0
        self.switches = 0
        self.live_matches_core = False
        self.fresh_core_observation = False
        self.reason = 'collecting_direction_evidence'
        self.current_yaw = 1500.
        self.units_per_degree = 1.
        self.left_sign = 1
        self.limits = (800, 2200)
        self.command_window = 340

    @staticmethod
    def _quality(audio, now):
        direction = getattr(getattr(audio, 'direction', None), 'value', getattr(audio, 'direction', None))
        if direction not in ('left', 'right', 'center', 'left_front', 'right_front') or audio.motor_suppressed:
            return None
        values = (audio.speech_confidence, audio.confidence, audio.doa_confidence,
                  audio.temporal_stability, audio.azimuth_std_deg, audio.azimuth_deg, audio.observed_monotonic)
        if not all(finite(v) for v in values):
            return None
        speech, confidence, doa, stability, spread, angle, observed = values
        if not all(0 <= v <= 1 for v in (speech, confidence, doa, stability)):
            return None
        if (speech < .5 or min(confidence, doa) < .2 or stability < .5 or
                not 0 <= spread <= 25 or abs(angle) > 90 or observed <= 0 or
                not -.02 <= now - observed <= .35):
            return None
        if audio.noise_state in {'noise', 'quiet', 'fan_or_motor', 'low_band_noise', 'denoise_guard',
                                  'ambiguous_delay', 'low_coherence', 'direction_transition', 'stale_audio',
                                  'sample_clock_mismatch', 'channels_nearly_identical'}:
            return None
        return speech * math.sqrt(min(confidence, doa) * stability) * math.exp(-.5 * (spread / 30.) ** 2)

    def _pose_at(self, observed):
        if not self.poses:
            return self.current_yaw
        previous = self.poses[0]
        for current in self.poses:
            if current[0] >= observed:
                if current[0] <= previous[0]:
                    return current[1]
                t = max(0., min(1., (observed - previous[0]) / (current[0] - previous[0])))
                return previous[1] + t * (current[1] - previous[1])
            previous = current
        return previous[1]

    def _summarize(self, now):
        while self.events and now - self.events[0][0] > self.window_s:
            self.events.popleft()
        for track in self.tracks.values():
            track.weight = track.duration = 0.
            track.observations = 0
        for observed, identity, duration, quality in self.events:
            track = self.tracks.get(identity)
            if track is not None:
                track.weight += duration * quality * math.exp(-max(0., now - observed) / self.decay_s)
                track.duration += duration
                track.observations += 1
        for identity in list(self.tracks):
            if identity != self.core_id and now - self.tracks[identity].seen > self.window_s:
                del self.tracks[identity]

    def _new_goal(self, track, now):
        # A bounded, finite position is stored once. Silence never adds another
        # old relative angle to it. New reliable observations can refine it.
        value = max(self.current_yaw - self.command_window,
                    min(self.current_yaw + self.command_window, track.center))
        self.goal_yaw = round(max(self.limits[0], min(self.limits[1], value)))
        self.last_goal_update = now

    def _choose(self, now, observed_id):
        # Acquisition and competition are different situations. A single
        # reliable new source need not defeat an absent incumbent.
        recent=[t for t in self.tracks.values() if now-t.seen<=.25]
        quick=[t for t in recent if t.observations>=3 and t.duration+1e-9>=self.first_active_s
               and t.weight>=self.first_weight_s and t.weight/max(t.duration,.001)>=.3]
        quick.sort(key=lambda t:t.weight,reverse=True)
        if quick:
            best=quick[0];core=self.tracks.get(self.core_id)
            isolated=all(t.id==best.id or t.weight<best.weight*.5 for t in recent)
            empty=core is None
            idle=core is not None and now-core.seen>=self.idle_replace_s
            if best.id==observed_id and isolated and (empty or idle and best.id!=self.core_id):
                if not empty:self.switches+=1
                self.core_id=best.id;self._new_goal(best,now)
                self.challenger=None;self.challenger_hits=0
                self.reason='first_reliable_direction' if empty else 'new_direction_after_quiet'
                return
        minimum_duration = self.minimum_active_s if self.core_id is None else self.challenge_active_s
        minimum_observations = 3 if self.core_id is None else 4
        ranked = sorted((t for t in self.tracks.values()
                         if t.duration + 1e-9 >= minimum_duration and
                         t.weight >= self.minimum_weight_s and t.observations >= minimum_observations),
                        key=lambda t: (t.weight, -t.id), reverse=True)
        if not ranked:
            return
        best = ranked[0]
        if self.core_id is None:
            if best.duration + 1e-9 >= self.minimum_active_s and best.weight >= self.minimum_weight_s and best.observations >= 3:
                self.core_id = best.id
                self._new_goal(best, now)
                self.reason = 'core_direction_locked'
            return
        core = self.tracks[self.core_id]
        stronger = (best.id != self.core_id and best.duration + 1e-9 >= self.challenge_active_s and
                    best.weight >= self.minimum_weight_s and best.observations >= 4 and
                    best.weight > core.weight * self.switch_ratio + self.switch_margin_s)
        if not stronger:
            self.challenger = None
            self.challenger_hits = 0
            return
        # A competitor must confirm itself. Fresh packets from a third
        # direction cannot extend its dwell or count as additional votes.
        if best.id != observed_id:
            if now - self.challenger_last > .35:
                self.challenger = None
                self.challenger_hits = 0
            return
        if self.challenger != best.id or now - self.challenger_last > .35:
            self.challenger = best.id
            self.challenger_since = now
            self.challenger_hits = 0
        self.challenger_last = now
        self.challenger_hits += 1
        self.reason = 'stronger_direction_confirming'
        if self.challenger_hits >= 3 and now - self.challenger_since + 1e-9 >= self.challenge_dwell_s:
            self.core_id = best.id
            self._new_goal(best, now)
            self.switches += 1
            self.challenger = None
            self.challenger_hits = 0
            self.reason = 'stronger_direction_selected'

    def update(self, audio, *, now, yaw, units_per_degree, left_sign, yaw_min, yaw_max, command_window):
        self.live_matches_core = False
        self.fresh_core_observation = False
        if not all(finite(v) for v in (now, yaw, units_per_degree)) or units_per_degree <= 0 or left_sign not in (-1, 1):
            self.reason = 'invalid_pose_model'
            return self.snapshot()
        self.current_yaw = float(yaw)
        self.units_per_degree = float(units_per_degree)
        self.left_sign = left_sign
        self.limits = (yaw_min, yaw_max)
        self.command_window = command_window
        self.poses.append((now, float(yaw)))
        self._summarize(now)
        observed = getattr(audio, 'observed_monotonic', None)
        sequence = getattr(audio, 'sample_sequence', None)
        identity_valid = (finite(observed) and observed > 0 and isinstance(sequence, Integral) and
                          not isinstance(sequence, bool) and sequence >= 0 and -.02 <= now - observed <= .35)
        if not identity_valid:
            self.reason = 'holding_without_current_direction' if self.core_id is not None else 'awaiting_fresh_direction'
            return self.snapshot()
        previous = self.watermark
        key = (int(sequence), float(observed))
        canonical = key == previous
        if previous is not None and not canonical and observed <= previous[1]:
            self.reason = 'replayed_direction_ignored'
            return self.snapshot()
        fresh = not canonical
        gap = previous is None or observed - previous[1] > .35
        reset_stream = previous is not None and fresh and sequence <= previous[0]
        if gap or reset_stream:
            self.events.clear()
            self.tracks = {k: t for k, t in self.tracks.items() if k == self.core_id}
            self.challenger = None
            self.challenger_hits = 0
            self._summarize(now)
        if fresh:
            self.watermark = key
        quality = self._quality(audio, now)
        if quality is None:
            self.reason = 'holding_without_current_direction' if self.core_id is not None else 'collecting_direction_evidence'
            return self.snapshot()
        duration = .02 if gap or reset_stream else min(.1, max(0., observed - previous[1]))
        target = self._pose_at(observed) + left_sign * units_per_degree * audio.azimuth_deg
        nearest = min(self.tracks.values(), key=lambda t: abs(t.center - target), default=None)
        if nearest is None or abs(nearest.center - target) > self.merge_degrees * units_per_degree:
            if not fresh:
                return self.snapshot()
            if len(self.tracks) >= 8:
                removable = [t for t in self.tracks.values() if t.id != self.core_id]
                if removable:
                    del self.tracks[min(removable, key=lambda t: t.weight).id]
            nearest = SoundTrack(self.next_id, target, observed)
            self.next_id += 1
            self.tracks[nearest.id] = nearest
        if fresh:
            alpha = 1. - math.exp(-duration / .25)
            nearest.center += alpha * (target - nearest.center)
            nearest.seen = observed
            self.events.append((observed, nearest.id, duration, quality))
            self._summarize(now)
            self.reason = 'core_direction_retained' if self.core_id is not None else 'collecting_direction_evidence'
            self._choose(now, nearest.id)
        self.live_matches_core = self.core_id is not None and nearest.id == self.core_id
        self.fresh_core_observation = fresh and self.live_matches_core
        if fresh and self.live_matches_core and now - self.last_goal_update >= .25:
            core = self.tracks[self.core_id]
            if self.goal_yaw is None or abs(core.center - self.goal_yaw) >= 5:
                self._new_goal(core, now)
        return self.snapshot()

    def snapshot(self):
        normalizer = self.decay_s * (1. - math.exp(-self.window_s / self.decay_s))
        core = self.tracks.get(self.core_id)
        relative = ((core.center - self.current_yaw) / (self.left_sign * self.units_per_degree)) if core else None
        return dict(locked=core is not None, core_id=self.core_id, goal_yaw=self.goal_yaw,
            relative_azimuth_deg=max(-90., min(90., relative)) if relative is not None else None,
            live_matches_core=self.live_matches_core, fresh_core_observation=self.fresh_core_observation,
            reason=self.reason, switches=self.switches,
            current_score=round(100. * core.weight / normalizer, 2) if core else 0.,
            current_support_s=round(core.duration, 3) if core else 0.,
            historical=bool(core and not self.live_matches_core),
            score_kind='recency_weighted_voiced_time_not_identity_probability',
            position_model='existing_control_gain_in_servo_units_not_angle_calibration',
            policy=dict(window_s=self.window_s, decay_s=self.decay_s, minimum_active_s=self.minimum_active_s,
                first_active_s=self.first_active_s,first_weight_s=self.first_weight_s,idle_replace_s=self.idle_replace_s,
                switch_ratio=self.switch_ratio, switch_margin_s=self.switch_margin_s,
                challenger_active_s=self.challenge_active_s, challenger_dwell_s=self.challenge_dwell_s),
            candidates=[dict(id=t.id, goal_estimate=round(t.center, 2),
                             relative_azimuth_deg=round((t.center-self.current_yaw)/(self.left_sign*self.units_per_degree), 2),
                             score=round(100. * t.weight / normalizer, 2),
                             voiced_s=round(t.duration, 3), observations=t.observations, selected=t.id == self.core_id)
                        for t in sorted(self.tracks.values(), key=lambda t: t.weight, reverse=True)[:8]])
