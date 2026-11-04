"""Feedback-anchored horizontal array geometry; no serial IO or motor writes."""
from collections import deque
import math
from numbers import Real


def finite(x):
    return isinstance(x,Real) and not isinstance(x,bool) and math.isfinite(x)


def fit_yaw_calibration(points):
    """Fit actual feedback ticks versus measured physical left-positive degrees.

    Require measurements on both sides of zero. Never use steering gains or
    infer a physical angle from an unmeasured end-stop span.
    """
    if len(points)<3 or any(len(p)!=2 or not all(finite(x) for x in p) for p in points):
        raise ValueError('Need at least three finite [feedback ticks, measured left-positive degrees] points')
    points=sorted(points)
    positions=[p[0] for p in points];angles=[p[1] for p in points]
    if len(set(positions))!=len(points) or not min(angles)<0<max(angles) or min(abs(a) for a in angles)>2:
        raise ValueError('Need unique feedback positions, both angular sides and a near-zero measurement')
    if any(not 500<=p<=2500 for p in positions) or any(abs(a)>90 for a in angles):
        raise ValueError('Calibration outside supported position/angle range')
    steps=[b-a for a,b in zip(angles,angles[1:])]
    if not (all(d>0 for d in steps) or all(d<0 for d in steps)):
        raise ValueError('Measured angles must be monotonic with feedback position')
    xm=sum(positions)/len(points);ym=sum(angles)/len(points)
    slope=sum((x-xm)*(y-ym) for x,y in points)/sum((x-xm)**2 for x in positions)
    scale=abs(1/slope);zero=xm-ym/slope
    residual=max(abs(y-(x-zero)*slope) for x,y in points)
    if not 1<=scale<=30 or not 500<=zero<=2500 or residual>3:
        raise ValueError('Scale or fit residual invalid; remeasure rather than stretch angles')
    return dict(body_yaw_units_per_degree=scale,body_yaw_zero_position=zero,
                measured_left_sign=1 if slope>0 else -1,maximum_residual_deg=residual,
                measured_position_range=[min(positions),max(positions)],samples=len(points))


class FeedbackAlignedYaw:
    def __init__(self):
        self.samples=deque(maxlen=256);self.token=None
        self.status={'reason':'awaiting_pose'}
        self.was_moving=False;self.settled_since=None

    def _reject(self,reason):
        self.status={'reason':reason,'body_angle_available':False}
        return None

    def _at(self,t):
        rows=list(self.samples)
        for a,b in zip(rows,rows[1:]):
            if a[0]<=t<=b[0] and 0<b[0]-a[0]<=.30:
                return a[1]+(b[1]-a[1])*(t-a[0])/(b[0]-a[0])
        if rows and abs(t-rows[-1][0])<1e-6:return rows[-1][1]
        return None

    def resolve(self,*,now,wall_now,observed,window_s,yaw,pitch,estimated_yaw,moving,
                scale,zero,sign,yaw_limits,pitch_center,session):
        if not finite(now):return self._reject('invalid_audio_pose_clock')
        if session!=self.token or self.samples and now<self.samples[-1][0]:
            self.samples.clear();self.token=session;self.was_moving=False;self.settled_since=None
        if not all(finite(x) for x in (now,wall_now,observed,window_s,estimated_yaw)) or not 0<window_s<=.192001 or not 0<=now-observed<=.30:
            return self._reject('invalid_audio_pose_clock')
        if not yaw or not pitch:return self._reject('missing_feedback')
        for row in (yaw,pitch):
            if row.get('valid') is not True or not finite(row.get('position')) or not finite(row.get('ts')) or not 0<=wall_now-row['ts']<=1.5:
                return self._reject('invalid_or_stale_feedback')
        if not yaw_limits[0]<=yaw['position']<=yaw_limits[1] or not yaw_limits[0]<=estimated_yaw<=yaw_limits[1]:
            return self._reject('yaw_outside_limits')
        if abs(pitch['position']-pitch_center)>10:
            return self._reject('pitch_geometry_not_calibrated')
        if not finite(scale) or not 1<=scale<=30 or not finite(zero) or isinstance(sign,bool) or sign not in (-1,1):
            return self._reject('yaw_angle_calibration_missing')
        if not self.samples or now>self.samples[-1][0]:self.samples.append((now,float(estimated_yaw)))
        start=observed-window_s;mid=observed-window_s/2
        if moving:
            self.was_moving=True;self.settled_since=None
            stamp=now-(wall_now-yaw['ts'])
            predicted_feedback=self._at(stamp)
            center=self._at(mid);first=self._at(start);last=self._at(observed)
            if any(x is None for x in (predicted_feedback,center,first,last)):
                return self._reject('awaiting_time_aligned_feedback_history')
            correction=yaw['position']-predicted_feedback
            if abs(correction)/scale>6:return self._reject('feedback_trajectory_disagreement')
            span=abs(last-first)/scale
            if span>10:return self._reject('audio_window_rotation_too_large')
            position=center+correction
            uncertainty=12.+span/2+abs(correction)/scale
            basis='measured_scale_feedback_anchored_trajectory_at_audio_midpoint'
        else:
            if self.was_moving:
                if self.settled_since is None:self.settled_since=now
                if start<self.settled_since:return self._reject('audio_window_overlaps_settling')
            position=yaw['position'];span=0.;uncertainty=12.
            basis='measured_scale_actual_stationary_feedback'
        angle=(position-zero)/(scale*sign)
        if not finite(angle) or abs(angle)>180:return self._reject('invalid_head_angle')
        self.status={'reason':'time_aligned_yaw_available','body_angle_available':True,
                     'head_yaw_deg':angle,'basis':basis,'window_rotation_deg':span,
                     'pose_uncertainty_deg':uncertainty,'pose_time':mid,
                     'feedback_age_s':wall_now-yaw['ts'],'metric_geometry':False}
        return dict(valid=True,head_yaw_deg=angle,basis=basis,observed_monotonic=observed,
                    pose_uncertainty_deg=uncertainty,window_rotation_deg=span,
                    time_aligned=True,pose_time=mid)
