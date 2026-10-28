"""Image-space pursuit, without estimating metres or a person's physical speed."""
from collections import deque
import math
import statistics

from .head import HeadPose


class ImageVelocity:
    """Fresh same-target observations only; camera/view changes reset velocity."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.previous = None
        self.samples = deque(maxlen=3)

    def update(self, error, *, identity, sequence, observed_at):
        if not all(math.isfinite(float(v)) for v in (*error, observed_at)):
            self.reset()
            return (0., 0.)
        current = (identity, sequence, float(observed_at), tuple(error))
        old = self.previous
        if old is not None and old[0] == identity and sequence == old[1]:
            return (0., 0.)
        self.previous = current
        if old is None or old[0] != identity or sequence < old[1]:
            self.samples.clear()
            return (0., 0.)
        dt = observed_at - old[2]
        if not .025 <= dt <= .4:
            self.samples.clear()
            return (0., 0.)
        velocity = tuple((a-b)/dt for a,b in zip(error, old[3]))
        if max(map(abs, velocity)) > 3.:
            # A detection jump is not evidence of a fast moving person.
            self.samples.clear()
            return (0., 0.)
        self.samples.append(velocity)
        if len(self.samples) < 2:
            return (0., 0.)
        return tuple(statistics.median(row[axis] for row in self.samples) for axis in (0,1))


def adaptive_visual_step(logic, estimated, commanded, error, measured, velocity, limits):
    """Large errors move farther and sooner; proximity and inward motion brake.

    An inactive axis retains its existing goal. Reissuing the intermediate
    planned position on every centred frame would repeatedly cancel motion.
    Velocity is normalized image displacement per second, including camera
    motion, not independently measured human motion. Only sustained outward
    image motion contributes a small, bounded prediction.
    """
    effective=[];stages=[];caps=[];urgencies=[]
    for axis,index in (('yaw',0),('pitch',1)):
        value,raw,speed=error[index],measured[index],velocity[index]
        band=float(limits[axis+'_deadband']);maximum=int(limits['max_'+axis+'_delta'])
        if not all(math.isfinite(float(v)) for v in (value,raw,speed)) or abs(raw)<=band or abs(value)<=band or raw*value<=0:
            effective.append(0.);stages.append('hold');caps.append(1);urgencies.append(0.)
            continue
        outward = min(2.,abs(speed)) if raw*speed>0 else 0.
        urgency=min(1.,max(0.,(abs(raw)-band)/max(.12,.45-band))+.22*outward)
        prediction=math.copysign(min(.08,.10*outward),raw)
        # A lagging EMA may limit smoothing, but must not reduce a clear large
        # image displacement to a perpetual tiny step.
        desired=math.copysign(min(abs(raw),max(abs(value),.85*abs(raw))),raw)+prediction
        effective.append(desired*(1.+.65*urgency))
        caps.append(max(1,round(maximum*(.10+.90*urgency))))
        urgencies.append(urgency)
        stages.append('coarse' if urgency>=.65 else 'medium' if urgency>=.22 else 'fine')
    candidate=logic.apply_visual_error(estimated,*effective,
        yaw_deadband=float(limits['yaw_deadband']),pitch_deadband=float(limits['pitch_deadband']),
        max_yaw_delta=caps[0],max_pitch_delta=caps[1],
        min_yaw_delta=min(int(limits['min_yaw_delta']),caps[0]),
        min_pitch_delta=min(int(limits['min_pitch_delta']),caps[1]))
    pose=logic.clamp_pose(HeadPose(candidate.yaw if effective[0] else commanded.yaw,
                                  candidate.pitch if effective[1] else commanded.pitch))
    duration=round(420-260*max(urgencies))
    return pose,dict(mode='progressive_image_feedback',controller='adaptive_image_velocity',yaw_stage=stages[0],pitch_stage=stages[1],
        effective_error=effective,yaw_step_cap=caps[0],pitch_step_cap=caps[1],
        image_velocity_norm_s=list(velocity),urgency=max(urgencies),move_time_ms=duration,
        metric_distance_used=False,physical_target_velocity_measured=False,
        requires_new_visual_observation=True,inactive_axes_keep_goal=True)
