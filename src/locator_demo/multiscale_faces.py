"""Bounded auxiliary face search, preserving native coordinates and full FOV."""
from __future__ import annotations

from dataclasses import replace


def iou(a, b):
    intersection = max(0, min(a.x2, b.x2) - max(a.x1, b.x1)) * max(0, min(a.y2, b.y2) - max(a.y1, b.y1))
    return intersection / max(1, a.area + b.area - intersection)


def merge_faces(targets, threshold=.35):
    result = []
    for target in sorted(targets, key=lambda t: t.score, reverse=True):
        if not any(iou(target, other) >= threshold for other in result):
            result.append(target)
    return result


class AdaptiveFaceSearch:
    def __init__(self, interval=.18):
        self.interval = interval
        self.last = -1e9
        self.tile = self.calls = 0
        self.roi = None
        self.roi_seen = -1e9
        self.last_region = None
        self.shape = None

    def search(self, image, primary, detect_crop, *, now):
        h, w = image.shape[:2]
        if self.shape != (h, w):
            self.shape = (h, w)
            self.roi = None
        if now - self.last < self.interval:
            return list(primary)
        self.last = now
        self.calls += 1
        # Always retain the full-frame detections. One extra bounded pass alternates
        # between a recent small face and overlapping tiles spanning the whole view.
        small = [t for t in primary if t.y2 - t.y1 < h * .22]
        if small:
            self.roi = max(small, key=lambda t: t.score)
            self.roi_seen = now
        if self.roi is not None and now - self.roi_seen < 1 and self.calls % 2:
            target = self.roi
            rw = min(w, max(320, round((target.x2 - target.x1) * 3)))
            rh = min(h, max(320, round((target.y2 - target.y1) * 3)))
            cx, cy = target.center
            x, y = max(0, min(w-rw, round(cx-rw/2))), max(0, min(h-rh, round(cy-rh/2)))
            kind = "tracked_roi"
        else:
            rw, rh = max(1, round(w * .5)), max(1, round(h * .65))
            x = round((self.tile % 3) / 2 * (w-rw))
            y = (self.tile // 3) * (h-rh)
            self.tile = (self.tile + 1) % 6
            kind = "search_tile"
        self.last_region = {"kind": kind, "xywh": [x, y, rw, rh]}
        local = detect_crop(image[y:y+rh, x:x+rw]) or []
        mapped = [replace(t, x1=max(0, t.x1+x), y1=max(0, t.y1+y),
                          x2=min(w, t.x2+x), y2=min(h, t.y2+y),
                          tracking_x=t.tracking_center[0]+x, tracking_y=t.tracking_center[1]+y,
                          tracking_source=t.tracking_source+"_native_roi") for t in local]
        if mapped:
            self.roi = max(mapped, key=lambda t: t.score)
            self.roi_seen = now
        return merge_faces([*primary, *mapped])
