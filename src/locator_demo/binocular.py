"""Conservative, uncalibrated two-view observations and control-view arbitration.

Appearance matches are tentative associations, never calibrated stereo geometry
or biometric identities. No coordinates are averaged across camera views.
"""
from __future__ import annotations

from dataclasses import replace
import math

import cv2
import numpy as np
from .av_selector import AudioVisualSelector
from .visual_priority import VisualPrioritySelector


def is_observed(target):
    return not target.tracking_source.endswith("_held")


def appearance_descriptor(image, target):
    h, w = image.shape[:2]
    x1, y1 = max(0, int(target.x1)), max(0, int(target.y1))
    x2, y2 = min(w, int(target.x2)), min(h, int(target.y2))
    if x2 - x1 < 12 or y2 - y1 < 12:
        return None
    crop = cv2.resize(image[y1:y2, x1:x2], (24, 24))
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gray -= gray.mean()
    norm = np.linalg.norm(gray)
    if norm < 50:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    histogram = cv2.calcHist([hsv], [0, 1], None, [12, 8], [0, 180, 0, 256]).ravel()
    histogram /= max(1e-6, np.linalg.norm(histogram))
    return gray.ravel() / norm, histogram


def association_score(a, b, descriptors, width, height):
    """Loose geometric gates plus strong appearance; no assumed epipolar line."""
    ta, tb = a[1], b[1]
    ha, hb = ta.y2 - ta.y1, tb.y2 - tb.y1
    ax, ay = ta.center
    bx, by = tb.center
    if min(ha, hb) <= 0 or abs(math.log(ha / hb)) > .4:
        return None
    if abs(ay - by) / height > .10 or abs(ax - bx) / width > .25:
        return None
    da, db = descriptors.get((a[0], ta.face_id)), descriptors.get((b[0], tb.face_id))
    if da is None or db is None:
        return None
    texture = float(np.dot(da[0], db[0]))
    colour = float(np.dot(da[1], db[1]))
    score = .75 * texture + .25 * colour
    return score if texture >= .65 and colour >= .75 and score >= .82 else None


class BinocularFusion:
    def __init__(self, tracking_eye="auto",*,guided=False,visual_priority=False,sector_half_width_deg=15.):
        self.tracking_eye = tracking_eye
        self.ids, self.seen, self.pending_pairs = {}, {}, {}
        self.next_id = 1
        self.eye = self.person = None
        self.challenger = None
        self.challenger_since = 0.
        self.person_challenger = None
        self.person_challenger_since = 0.
        self.transition = None
        self.transition_since = 0.
        self.transition_hits = 0
        self.last_update = None
        self.selector=(VisualPrioritySelector(sector_half_width_deg=sector_half_width_deg) if visual_priority else AudioVisualSelector()) if guided else None

    def _reset_confirmation(self, now):
        """Keep track labels, but do not bridge a stale observation interval.

        The 0.75 s boundary matches the existing published-frame freshness
        window. Ordinary skipped frames and short one-eye occlusions retain
        their current handling; a longer gap requires fresh evidence again.
        """
        self.pending_pairs.clear()
        self.transition = None
        self.transition_since = now
        self.transition_hits = 0
        self.challenger = self.person_challenger = None
        self.challenger_since = self.person_challenger_since = now
        if self.selector:
            self.selector.audio_candidate = None
            self.selector.audio_hits = 0
            self.selector.audio_since = now
            self.selector.last_audio_sequence = None
            if isinstance(self.selector, VisualPrioritySelector):
                self.selector.challenger = None
                self.selector.challenger_since = now

    def _id(self, key, now):
        if key not in self.ids:
            self.ids[key] = self.next_id
            self.next_id += 1
        self.seen[key] = now
        return self.ids[key]

    @staticmethod
    def _value(target):
        return .65 * target.score + .2 * min(1., (target.y2 - target.y1) / 240) + .15 * target.speaker_score

    def update(self, images, targets, health, *, now, geometric_matches=None,audio=None,frame_time=0.):
        gap = None if self.last_update is None else now - self.last_update
        confirmation_reset = gap is not None and gap > .75
        if confirmation_reset:
            self._reset_confirmation(now)
        self.last_update = now
        h, w = next(iter(images.values())).shape[:2]
        for key in list(self.seen):
            if now - self.seen[key] > 1.:
                self.seen.pop(key)
                self.ids.pop(key, None)
        observations = [(eye, t) for eye in ("left", "right") for t in targets.get(eye, [])
                        if health[eye]["valid"] and is_observed(t)]
        descriptors = {(eye, t.face_id): appearance_descriptor(images[eye], t) for eye, t in observations}
        for eye, t in observations:
            self._id((eye, t.face_id), now)
        left = [o for o in observations if o[0] == "left"]
        right = [o for o in observations if o[0] == "right"]
        geometry={(m["left_track"],m["right_track"]):m for m in geometric_matches or []}
        scores = {key:1. for key in geometry}
        geometry_left={key[0] for key in geometry}; geometry_right={key[1] for key in geometry}
        for a in left:
            for b in right:
                if a[1].face_id in geometry_left or b[1].face_id in geometry_right:
                    continue
                score = association_score(a, b, descriptors, w, h)
                if score is not None:
                    scores[(a[1].face_id, b[1].face_id)] = score
        accepted = []
        current_pairs = set()
        for (lid, rid), score in scores.items():
            # Both directions must be unique by a useful margin. Similar people
            # remain separate instead of being silently collapsed into one ID.
            alternatives = [v for (l, r), v in scores.items() if (l == lid or r == rid) and (l, r) != (lid, rid)]
            if alternatives and score - max(alternatives) < .08:
                continue
            key = (lid, rid)
            current_pairs.add(key)
            first, hits = self.pending_pairs.get(key, (now, 0))
            self.pending_pairs[key] = (first, hits + 1)
            if hits + 1 < 3 or now - first < .12:
                continue
            lk, rk = ("left", lid), ("right", rid)
            gids = {self.ids[lk], self.ids[rk]}
            # Never merge groups that already contain two fresh observations
            # from the same eye.
            members = [k for k in self.ids if self.ids[k] in gids and k in descriptors]
            if any(sum(k[0] == eye for k in members) > 1 for eye in ("left", "right")):
                continue
            gid = self.person if self.person in gids else min(gids)
            for member in self.ids:
                if self.ids[member] in gids:
                    self.ids[member] = gid
            accepted.append({"left_track": lid, "right_track": rid, "person_id": gid,
                             "score": round(score, 3), "status": geometry[key].get('kind','calibrated_correspondence') if key in geometry else "tentative_appearance"})
        self.pending_pairs = {k: v for k, v in self.pending_pairs.items() if k in current_pairs}

        # A formerly matched pair must pass today's tests too. If both views
        # now disagree or are ambiguous, keep their observations separate.
        accepted_keys = {(p["left_track"], p["right_track"]) for p in accepted}
        for a in left:
            for b in right:
                lk, rk = ("left", a[1].face_id), ("right", b[1].face_id)
                if self.ids[lk] == self.ids[rk] and (a[1].face_id, b[1].face_id) not in accepted_keys:
                    self.ids[rk] = self.next_id
                    self.next_id += 1

        mapped = {eye: [] for eye in ("left", "right")}
        people = {}
        positions={p["person_id"]:geometry[(p["left_track"],p["right_track"])]["position"] for p in accepted
                   if geometry.get((p["left_track"],p["right_track"]),{}).get('position') is not None}
        bearings={p['person_id']:geometry[(p['left_track'],p['right_track'])]['bearing'] for p in accepted
                  if geometry.get((p['left_track'],p['right_track']),{}).get('bearing') is not None}
        fixations={p['person_id']:geometry[(p['left_track'],p['right_track'])]['fixation'] for p in accepted
                   if geometry.get((p['left_track'],p['right_track']),{}).get('fixation') is not None}
        for eye, target in observations:
            gid = self.ids[(eye, target.face_id)]
            item = replace(target,face_id=gid,source_eye=eye,stereo_position=positions.get(gid),bearing=bearings.get(gid,target.bearing),fixation=fixations.get(gid,target.fixation))
            mapped[eye].append(item)
            people.setdefault(gid, []).append(item)
        eligible = {gid: obs for gid, obs in people.items()
                    if self.tracking_eye == "auto" or any(t.source_eye == self.tracking_eye for t in obs)}
        winner=(self.selector.update(eligible,now=now,size=(w,h),audio=audio,frame_time=frame_time) if self.selector else
                max(eligible,key=lambda gid:max(self._value(t) for t in eligible[gid]),default=None))
        if not self.selector and self.person in eligible and winner != self.person:
            old_value = max(self._value(t) for t in eligible[self.person])
            new_value = max(self._value(t) for t in eligible[winner])
            if new_value < old_value + .18:
                winner = self.person
                self.person_challenger = None
            else:
                if self.person_challenger != winner:
                    self.person_challenger, self.person_challenger_since = winner, now
                if now - self.person_challenger_since < .4:
                    winner = self.person
        else:
            self.person_challenger = None
        chosen = None
        reason = "no_faces"
        if winner is not None:
            candidates = [t for t in eligible[winner]
                          if self.tracking_eye == "auto" or t.source_eye == self.tracking_eye]
            chosen = max(candidates, key=self._value)
            existing = next((t for t in candidates if t.source_eye == self.eye), None)
            if existing is not None and self._value(existing) >= self._value(chosen) - .12:
                chosen = existing
            if existing is not None and chosen.source_eye != self.eye:
                candidate_key = (winner, chosen.source_eye)
                if candidate_key != self.challenger:
                    self.challenger, self.challenger_since = candidate_key, now
                if now - self.challenger_since < .4:
                    chosen = existing
            else:
                self.challenger = None
            selected = chosen.source_eye
            reason = "target_retained" if (selected, winner) == (self.eye, self.person) else "observed_face"
        else:
            usable = [eye for eye in ("left", "right") if health[eye]["valid"]]
            if self.tracking_eye != "auto":
                selected = self.tracking_eye if self.tracking_eye in usable else None
            elif self.eye in usable:
                selected = self.eye
            else:
                selected = max(usable, key=lambda eye: health[eye].get("quality_score", 0), default=None)
            if selected is None:
                reason = "no_usable_eye"
        transition = (selected, winner, chosen is not None and chosen.stereo_position is not None)
        if transition != self.transition:
            self.transition, self.transition_since, self.transition_hits = transition, now, 0
        self.transition_hits += 1
        ready = chosen is not None and self.transition_hits >= 3 and now - self.transition_since >= .12
        if chosen is not None and not ready:
            reason = "confirming_view"
        changed = selected != self.eye
        self.eye, self.person = selected, winner
        if self.selector:chosen=self.selector.smooth_target(chosen,now,(w,h))
        return {"eye": selected, "target": chosen, "targets": mapped, "eye_changed": changed,
                "control_ready": ready,
                "summary": {"enabled": True, "mode": "dual_2d", "selected_eye": selected,
                            "selected_person_id": winner, "selection_reason": reason, "control_ready": ready,
                            "confirmation_reset_after_gap": confirmation_reset,
                            "observation_gap_s": gap,
                            "associations": accepted,
                            "people": [{"person_id": gid, "eyes": sorted(t.source_eye for t in obs),
                                        "association": "calibrated_correspondence" if gid in positions else "aligned_feature_correspondence" if gid in bearings else "tentative_appearance" if len(obs) == 2 else "single_view",
                                        "position":positions.get(gid),
                                        "bearing":bearings.get(gid, next((t.bearing for t in obs if t.bearing),None)),
                                        "observations": [{"eye": t.source_eye, "score": round(t.score, 3)} for t in obs]}
                                       for gid, obs in people.items()],
                            "calibrated": False, "depth_enabled": False,
                            "uses_two_eye_bearing":chosen is not None and chosen.bearing is not None and chosen.bearing.get('source')=='binocular_feature_rays',
                            "uses_two_eye_fixation":chosen is not None and chosen.fixation is not None and chosen.fixation.get('mode')=='binocular',
                            "target_selection":dict(self.selector.metrics) if self.selector else {'mode':'legacy_visual_arbitration'},
                            "coordinate_frame": "selected_eye_pixels",
                            "note": "Appearance association is tentative; depth and common head angles require calibration"}}
