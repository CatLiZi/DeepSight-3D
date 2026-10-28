"""Causal admission of new delay evidence, without extrapolating a direction."""
from collections import deque


class DelayConsensus:
    """Require sustained evidence for an abrupt change, in seconds not polls.

    A rejected candidate never returns the preceding direction. Confirmation
    protects an abrupt change across opposite sides, away from the centre;
    same-side magnitudes can vary with reverberation without losing the coarse
    bearing. Silence/capture gaps reset the reference.
    Confirmation tolerances are delay samples, not calibrated angular errors.
    """

    def __init__(self,sample_rate=48000):
        self.sample_rate=sample_rate
        self.reset()

    def reset(self):
        self.reference=None
        self.accepted_at=None
        self.pending=deque(maxlen=12)

    def commit(self,filtered_delay):
        """Use the caller's reliable median, rather than a second raw filter."""
        self.reference=filtered_delay

    def accept(self,delay,now):
        """Return (admitted, reset_history); never invent an output delay."""
        if self.reference is None or now-self.accepted_at>.30:
            self.reference=delay;self.accepted_at=now;self.pending.clear()
            return True,True
        opposite_sides=(delay*self.reference<0 and
                        min(abs(delay),abs(self.reference))*self.sample_rate>=3.)
        if not opposite_sides or abs(delay-self.reference)*self.sample_rate<=6.:
            self.accepted_at=now;self.pending.clear()
            return True,False
        if self.pending and (now-self.pending[-1][0]>.08 or
                            abs(delay-self.pending[-1][1])*self.sample_rate>2.):
            self.pending.clear()
        self.pending.append((now,delay))
        if len(self.pending)>=3 and now-self.pending[0][0]>=.08-1e-9:
            # Admit the actual new observation. The caller still requires its
            # ordinary multi-window confidence before downstream use.
            self.reference=delay;self.accepted_at=now;self.pending.clear()
            return True,True
        return False,False
