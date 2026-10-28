"""Scale-free binocular fixation and its per-eye display coordinates.

One head has one yaw/pitch command, not independently rotating eyes. Preserve
the observed disparity and remove only the common directional error. No metric
baseline, distance, or synthetic image warp is used.
"""
from __future__ import annotations
import math
import numpy as np


def _inside(point, target):
    return (np.isfinite(point).all() and target.x1 <= point[0] <= target.x2
            and target.y1 <= point[1] <= target.y2)


def make_fixation(rectifier, size, points, *, anchor_kind):
    rays=[]
    for eye, point in points.items():
        if not np.isfinite(point).all():
            return None
        ray, k = rectifier.bearing_rays(eye, [point], size)
        rays.append(ray[0])
    # Rectified rays start on the same z=1 plane before a common rotation.
    # Average BEFORE normalizing z, so an off-axis target is not eye-weighted.
    mean=np.mean(rays,axis=0)
    if not np.isfinite(mean).all() or mean[2] <= 1e-6:
        return None
    ray=mean/mean[2]
    focal=[float(k[0,0]),float(k[1,1])]
    if min(focal) <= 0:
        return None
    virtual=[float(size[0]/2+focal[0]*ray[0]),float(size[1]/2+focal[1]*ray[1])]
    projection=rectifier.direction_projection(size)
    return dict(mode='binocular' if len(points)==2 else 'single_eye',
                frame='face_fixation_virtual',size=list(size),anchor_kind=anchor_kind,
                points={eye:list(map(float,p)) for eye,p in points.items()},
                projections={eye:projection[eye] for eye in points},
                ray=ray.tolist(),focal_px=focal,virtual_u_px=virtual[0],virtual_v_px=virtual[1],
                metric_distance_used=False)


def matched_fixation(left, right, matched_left, matched_right, rectifier, size):
    """Called only after current-frame bidirectional texture/epipolar checks.

    Prefer homologous detector landmarks (normally the nose). If their local
    disparity or height disagrees with verified correspondences, use the
    matched face texture centroid in both views instead of mixing landmarks.
    """
    a,b=np.asarray(left.tracking_center),np.asarray(right.tracking_center)
    disparity=float(np.median(matched_left[:,0]-matched_right[:,0]))
    vertical=float(np.median(matched_left[:,1]-matched_right[:,1]))
    width=min(left.x2-left.x1,right.x2-right.x1)
    height=min(left.y2-left.y1,right.y2-right.y1)
    landmarks=(left.tracking_x is not None and right.tracking_x is not None
               and left.tracking_source==right.tracking_source
               and not left.tracking_source.endswith('_held')
               and _inside(a,left) and _inside(b,right)
               and abs((a[0]-b[0])-disparity)<=max(4.,.12*width)
               and abs((a[1]-b[1])-vertical)<=max(3.,.04*height))
    if not landmarks:
        a,b=np.median(matched_left,axis=0),np.median(matched_right,axis=0)
    return make_fixation(rectifier,size,{'left':a,'right':b},
                         anchor_kind='paired_face_landmarks' if landmarks else 'matched_face_texture')


def control_point(fixation, size):
    if not fixation or fixation.get('size') != list(size):
        return None
    point=(fixation.get('virtual_u_px'),fixation.get('virtual_v_px'))
    if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in point):
        return None
    return point


def fixation_overlay(target, eye_targets, size, *, offsets=(0.,0.), fresh=True):
    """Targets, goals and control all use the same current fixation geometry.

    The goal is a local image-servo setpoint at the observed disparity. It is
    recomputed from each new frame, not a promise of a calibrated final pose.
    """
    empty=dict(mode='none' if fresh else 'stale',eyes={},metric_distance_used=False)
    if target is None or not fresh or not target.fixation:
        return empty
    f=target.fixation
    if control_point(f,size) is None:
        return empty
    expected=('left','right') if f.get('mode')=='binocular' else (target.source_eye,)
    if any(eye not in f.get('points',{}) for eye in expected):
        return empty
    # No cached second-eye dot when the other view is absent, held or another ID.
    for eye in expected:
        if not any(t.face_id==target.face_id and not t.tracking_source.endswith('_held')
                   for t in eye_targets.get(eye,[])):
            return empty
    try:
        fx,fy=map(float,f['focal_px'])
        ray=np.asarray(f['ray'],dtype=float)
        if ray.shape!=(3,) or not np.isfinite(ray).all() or min(fx,fy)<=0:
            return empty
        offset=[max(-.45,min(.45,float(v))) for v in offsets]
        aim=np.array([offset[0]*size[0]/fx,offset[1]*size[1]/fy,1.])
        if not np.isfinite(aim).all():
            return empty
        views={}
        for eye in expected:
            p=np.asarray(f['projections'][eye],dtype=float)
            anchor=np.asarray(f['points'][eye],dtype=float)
            if p.shape!=(3,3) or anchor.shape!=(2,) or not np.isfinite(p).all() or not np.isfinite(anchor).all():
                return empty
            current=p@ray;desired=p@aim
            if min(current[2],desired[2])<=1e-6:
                return empty
            # Both goals retain the per-eye deviation from the shared ray.
            # At zero common error, both goals coincide with their own anchors.
            goal=anchor+desired[:2]/desired[2]-current[:2]/current[2]
            views[eye]=dict(target_px=anchor.tolist(),goal_px=goal.tolist(),
                            goal_visible=bool(0<=goal[0]<size[0] and 0<=goal[1]<size[1]))
        return dict(mode=f['mode'],person_id=target.face_id,anchor_kind=f['anchor_kind'],
                    eyes=views,raw_error=[float(fx*ray[0]/(size[0]/2)-offset[0]*2),
                                        float(fy*ray[1]/(size[1]/2)-offset[1]*2)],
                    metric_distance_used=False,disparity_preserved=True)
    except (KeyError,TypeError,ValueError,ZeroDivisionError):
        return empty
