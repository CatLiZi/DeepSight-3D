"""Fresh-image trigger with confirmation and separate enter/release bands."""
import math


class VisualAxisTrigger:
    def __init__(self):
        self.reset()

    def reset(self):
        self.direction = self.hits = 0
        self.active = False
        self.sequence = None
        self.since = 0.
        self.reason = 'waiting_observation'

    def update(self, error, *, band, sequence, now):
        release = band * .6
        if not math.isfinite(error) or abs(error) <= release:
            self.reset()
            self.reason = 'centered'
            return 0., band
        direction = 1 if error > 0 else -1
        if direction != self.direction:
            self.reset()
            self.direction = direction
            self.since = now
        if self.active:
            self.reason = 'tracking'
            return error, release
        if abs(error) <= band:
            self.reset()
            self.reason = 'inside_enter_band'
            return 0., band
        if sequence != self.sequence:
            self.hits += 1
            self.sequence = sequence
        self.active = self.hits >= 2 and now - self.since >= .04
        self.reason = 'tracking' if self.active else 'confirming_direction'
        return (error if self.active else 0.), release if self.active else band

    def snapshot(self):
        return dict(reason=self.reason, active=self.active, confirmation_frames=self.hits)
