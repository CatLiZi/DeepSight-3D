"""Matched binocular bearings independent of metric-distance acceptance.

Rays use the common rectification and approximate optical bisector axes.
No baseline length, Q, metric projection or XYZ is used here.
"""
from __future__ import annotations
from dataclasses import replace
import math
import cv2
import numpy as np
from .binocular import is_observed
from .stereo_geometry import StereoTargetGeometry
from .fixation import make_fixation, matched_fixation


class ScreenCenteredDirectionFrame:
    """Direction-only reference: the rectified image centre is zero degrees.

    Calibration acceptance, rectification, Q and metric projection are unchanged.
    """
    bearing_frame='screen_centered_optical'

    def __init__(self,rectifier):
        self.base=rectifier;size=rectifier.crop[2:]
        rays,_=rectifier.bearing_rays('left',[[size[0]/2,size[1]/2]],size)
        direction=rays[0]/np.linalg.norm(rays[0]);forward=np.array([0.,0.,1.])
        cross=np.cross(direction,forward);sine=float(np.linalg.norm(cross));cosine=float(np.dot(direction,forward))
        self.rotation=np.eye(3) if sine<1e-10 else cv2.Rodrigues(cross/sine*math.atan2(sine,cosine))[0]

    def __getattr__(self,name):return getattr(self.base,name)

    def bearing_rays(self,eye,points,size):
        rays,k=self.base.bearing_rays(eye,points,size)
        return (self.rotation@rays.T).T,k

    def direction_projection(self,size):
        return {eye:(np.asarray(p)@self.rotation.T).tolist() for eye,p in self.base.direction_projection(size).items()}

    def direction_view(self,size):
        rays,_=self.bearing_rays('left',[[0,size[1]/2],[size[0],size[1]/2]],size)
        bounds=sorted(math.degrees(math.atan2(-p[0],p[2])) for p in rays)
        return dict(frame=self.bearing_frame,zero_reference='rectified_image_center',
                    visible_azimuth_interval_deg=bounds,absolute_angle_accuracy_verified=False)


def bearing_from_ray(ray,k,size,*,source,sigma=8.,interval=None,confidence=.7,frame='rig_optical_bisector'):
    x,y,z=map(float,ray)
    if not np.isfinite([x,y,z]).all() or z<=1e-6:return None
    angle=math.degrees(math.atan2(-x,z));elevation=math.degrees(math.atan2(-y,math.hypot(x,z)))
    return {'frame':frame,'azimuth_left_deg':angle,'elevation_up_deg':elevation,
        'azimuth_interval_deg':interval or [angle-6.,angle+6.],'sigma_deg':sigma,'confidence':float(confidence),
        'virtual_u_px':float(k[0,0]*x/z+size[0]/2),'virtual_v_px':float(k[1,1]*y/z+size[1]/2),
        'source':source,'orientation_to_ears_calibrated':False,'metric_distance_enabled':False}


def annotate_eye_bearings(targets,rectifier,size):
    result={eye:[] for eye in ('left','right')}
    for eye in result:
        for target in targets.get(eye,[]):
            x,y=target.center
            rays,k=rectifier.bearing_rays(eye,[(x,y),(target.x1,y),(target.x2,y)],size)
            bounds=sorted(math.degrees(math.atan2(-p[0],p[2])) for p in rays[1:])
            bearing=bearing_from_ray(rays[0],k,size,source='single_eye_rectified_ray',sigma=12.,interval=bounds,
                                     frame=getattr(rectifier,'bearing_frame','rig_optical_bisector'))
            fixation=make_fixation(rectifier,size,{eye:target.tracking_center},anchor_kind='single_eye_face_landmark')
            result[eye].append(replace(target,bearing=bearing,fixation=fixation))
    return result


class AlignedBearingMatcher:
    def __init__(self):self.metrics={'status':'not_aligned','valid_pairs':0};self.rejections={}

    def _reject(self,key):self.rejections[key]=self.rejections.get(key,0)+1;return None

    def _pair(self,a,b,left,right,rectifier,size):
        original_left,original_right=left,right
        # Build LK pyramids around the candidate faces, not the whole 2 MP eye.
        pad=32
        x0=max(0,int(min(left.x1,right.x1))-pad);y0=max(0,int(min(left.y1,right.y1))-pad)
        x1=min(a.shape[1],int(max(left.x2,right.x2))+pad);y1=min(a.shape[0],int(max(left.y2,right.y2))+pad)
        if x1-x0<24 or y1-y0<24:return self._reject('small_roi')
        a=a[y0:y1,x0:x1];b=b[y0:y1,x0:x1]
        left=replace(left,x1=left.x1-x0,x2=left.x2-x0,y1=left.y1-y0,y2=left.y2-y0)
        right=replace(right,x1=right.x1-x0,x2=right.x2-x0,y1=right.y1-y0,y2=right.y2-y0)
        mask=np.zeros_like(a);cx,cy=left.center
        cv2.ellipse(mask,(round(cx),round(cy)),(max(3,round((left.x2-left.x1)*.34)),
            max(3,round((left.y2-left.y1)*.37))),0,0,360,255,-1)
        points=cv2.goodFeaturesToTrack(a,80,.015,4,mask=mask,blockSize=5)
        if points is None or len(points)<8:return self._reject('low_texture')
        initial=points.copy();initial[:,:,0]+=right.center[0]-left.center[0]
        options=dict(winSize=(21,21),maxLevel=4,criteria=(cv2.TERM_CRITERIA_EPS|cv2.TERM_CRITERIA_COUNT,30,.01),flags=cv2.OPTFLOW_USE_INITIAL_FLOW)
        found,forward,error=cv2.calcOpticalFlowPyrLK(a,b,points,initial,**options)
        if found is None:return self._reject('no_forward_flow')
        back,reverse,_=cv2.calcOpticalFlowPyrLK(b,a,found,points.copy(),**options)
        if back is None:return self._reject('no_reverse_flow')
        p,q,c=points.reshape(-1,2),found.reshape(-1,2),back.reshape(-1,2)
        fb=np.linalg.norm(p-c,axis=1);dy=np.abs(p[:,1]-q[:,1]);sign=1 if rectifier.center_right>0 else -1
        valid=(forward.ravel()>0)&(reverse.ravel()>0)&(fb<.9)&(dy<max(1.5,size[0]/1920*2.5))&(error.ravel()<25)
        valid&=(q[:,0]>=right.x1)&(q[:,0]<=right.x2)&(q[:,1]>=right.y1)&(q[:,1]<=right.y2)
        valid&=((p[:,0]-q[:,0])*sign>=-.7)
        p,q,fb,dy=p[valid],q[valid],fb[valid],dy[valid]
        if len(p)<8:return self._reject('inconsistent_flow')
        agreement=StereoTargetGeometry._patch_agreement(a,b,p,q);valid=agreement>.7
        p,q,fb,dy,agreement=p[valid],q[valid],fb[valid],dy[valid],agreement[valid]
        if len(p)<8:return self._reject('patch_mismatch')
        coverage=np.ptp(p,axis=0)/np.maximum([left.x2-left.x1,left.y2-left.y1],1.)
        eigen=np.linalg.eigvalsh(np.cov(p.T))
        if min(coverage)<.2 or eigen[0]<.02*eigen[1]:return self._reject('concentrated_features')
        p+=np.array([x0,y0],np.float32);q+=np.array([x0,y0],np.float32)
        lp,k=rectifier.bearing_rays('left',p,size);rp,_=rectifier.bearing_rays('right',q,size)
        # Same rectified depth plane => mean of corresponding rays is the
        # midpoint bearing, with no need to know the physical baseline length.
        ray=np.median((lp+rp)*.5,axis=0)
        confidence=.3*min(1.,len(p)/30)+.35*float(np.median(agreement))+.35*math.exp(-float(np.median(fb))/.5)
        bearing=bearing_from_ray(ray,k,size,source='binocular_feature_rays',sigma=6.,confidence=confidence,
                                 frame=getattr(rectifier,'bearing_frame','rig_optical_bisector'))
        if bearing is None:return self._reject('backward_ray')
        bounds=[t.bearing['azimuth_interval_deg'] for t in (left,right) if t.bearing]
        if bounds:bearing['azimuth_interval_deg']=[min(x[0] for x in bounds),max(x[1] for x in bounds)]
        bearing.update(inliers=len(p),epipolar_p95_px=float(np.percentile(dy,95)),patch_correlation=float(np.median(agreement)))
        fixation=matched_fixation(original_left,original_right,p,q,rectifier,size)
        return {'left_track':left.face_id,'right_track':right.face_id,'confidence':confidence,
                'bearing':bearing,'fixation':fixation,'kind':'aligned_feature_correspondence'}

    def match(self,images,targets,rectifier):
        self.rejections={}
        if rectifier is None:self.metrics={'status':'not_aligned','valid_pairs':0};return []
        gray={eye:cv2.cvtColor(im,cv2.COLOR_BGR2GRAY) for eye,im in images.items()};size=images['left'].shape[1::-1]
        observed={eye:sorted((t for t in targets.get(eye,[]) if is_observed(t)),key=lambda t:t.score,reverse=True)[:4] for eye in gray}
        candidates=[];attempts=0
        for left in observed['left']:
            height=max(1.,left.y2-left.y1)
            for right in observed['right']:
                if not .5<(right.y2-right.y1)/height<2 or abs(left.center[1]-right.center[1])>.4*height:continue
                attempts+=1;result=self._pair(gray['left'],gray['right'],left,right,rectifier,size)
                if result:candidates.append(result)
        unique=[]
        for item in candidates:
            others=[x['confidence'] for x in candidates if x is not item and (x['left_track']==item['left_track'] or x['right_track']==item['right_track'])]
            if not others or item['confidence']-max(others)>.10:unique.append(item)
        self.metrics={'status':'bearing_only','candidate_pairs':attempts,'valid_pairs':len(unique),'rejections':self.rejections,
                      'uses_metric_baseline':False,'distance_output_enabled':False,'absolute_angle_accuracy_verified':False}
        return unique
