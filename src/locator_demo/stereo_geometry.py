"""Metric target surface estimates from calibrated, rectified stereo pixels."""
from __future__ import annotations

import math
import cv2
import numpy as np

from .binocular import is_observed


class StereoTargetGeometry:
    def __init__(self,min_distance=.20,max_distance=8.):
        self.min_distance=min_distance; self.max_distance=max_distance
        self.metrics={"status":"not_calibrated","valid_pairs":0}
        self.rejections={}

    def _reject(self,reason):
        self.rejections[reason]=self.rejections.get(reason,0)+1
        return None

    @staticmethod
    def _patch_agreement(gray_left,gray_right,a,b):
        scores=[]
        for p,q in zip(a,b):
            left=cv2.getRectSubPix(gray_left,(9,9),tuple(map(float,p))).astype(float).ravel()
            right=cv2.getRectSubPix(gray_right,(9,9),tuple(map(float,q))).astype(float).ravel()
            left-=left.mean();right-=right.mean()
            norm=np.linalg.norm(left)*np.linalg.norm(right)
            scores.append(float(left@right/norm) if norm>81*4 else -1.)
        return np.array(scores)

    def _pair(self,gray_left,gray_right,left,right,projections,rectifier):
        height,width=gray_left.shape
        x1,y1,x2,y2=map(int,(left.x1,left.y1,left.x2,left.y2))
        if x2-x1<20 or y2-y1<24:
            return self._reject('small_target')
        mask=np.zeros_like(gray_left)
        cv2.ellipse(mask,tuple(np.rint(left.center).astype(int)),
                    (max(3,int((x2-x1)*.34)),max(3,int((y2-y1)*.37))),0,0,360,255,-1)
        points=cv2.goodFeaturesToTrack(gray_left,maxCorners=80,qualityLevel=.015,minDistance=4,mask=mask,blockSize=5)
        if points is None or len(points)<8:return self._reject('insufficient_texture')
        initial=points.copy(); initial[:,:,0]+=right.center[0]-left.center[0]
        lk=dict(winSize=(21,21),maxLevel=4,
                criteria=(cv2.TERM_CRITERIA_EPS|cv2.TERM_CRITERIA_COUNT,30,.01),
                flags=cv2.OPTFLOW_USE_INITIAL_FLOW)
        found,forward,error=cv2.calcOpticalFlowPyrLK(gray_left,gray_right,points,initial,**lk)
        if found is None:return self._reject('forward_flow_failed')
        back,back_ok,_=cv2.calcOpticalFlowPyrLK(gray_right,gray_left,found,points.copy(),**lk)
        if back is None:return self._reject('backward_flow_failed')
        a,b,c=points.reshape(-1,2),found.reshape(-1,2),back.reshape(-1,2)
        fb=np.linalg.norm(a-c,axis=1)
        dy=np.abs(a[:,1]-b[:,1]); disparity=a[:,0]-b[:,0]
        p_l,p_r=projections["left"],projections["right"]
        sign=1 if p_r[0,3]<0 else -1
        min_disp=max(1.,p_l[0,0]*rectifier.baseline/self.max_distance)
        max_disp=p_l[0,0]*rectifier.baseline/self.min_distance
        inside=(b[:,0]>=right.x1)&(b[:,0]<=right.x2)&(b[:,1]>=right.y1)&(b[:,1]<=right.y2)
        valid=(forward.ravel()>0)&(back_ok.ravel()>0)&inside&(fb<.9)&(dy<max(1.5,width/1920*2.5))
        valid&=(error.ravel()<25)&(disparity*sign>min_disp)&(disparity*sign<max_disp)
        if valid.sum()<8:return self._reject('inconsistent_correspondences')
        a,b=a[valid],b[valid]; fb,dy=fb[valid],dy[valid]
        agreement=self._patch_agreement(gray_left,gray_right,a,b)
        keep=agreement>.7
        a,b,fb,dy,agreement=a[keep],b[keep],fb[keep],dy[keep],agreement[keep]
        if len(a)<8:return self._reject('patch_mismatch')
        homogeneous=cv2.triangulatePoints(p_l,p_r,a.T.astype(float),b.T.astype(float))
        nonzero=np.abs(homogeneous[3])>1e-9
        if not nonzero.all():
            homogeneous=homogeneous[:,nonzero]; a,b=a[nonzero],b[nonzero]; fb,dy,agreement=fb[nonzero],dy[nonzero],agreement[nonzero]
        xyz=(homogeneous[:3]/homogeneous[3]).T
        finite=np.isfinite(xyz).all(axis=1)&(xyz[:,2]>self.min_distance)&(xyz[:,2]<self.max_distance)
        xyz,a,b,fb,dy,agreement=xyz[finite],a[finite],b[finite],fb[finite],dy[finite],agreement[finite]
        if len(xyz)<8:return self._reject('invalid_depth')
        z=np.median(xyz[:,2]); mad=np.median(np.abs(xyz[:,2]-z))
        keep=np.abs(xyz[:,2]-z)<max(.025,3*mad)
        xyz,a,b,fb,dy,agreement=xyz[keep],a[keep],b[keep],fb[keep],dy[keep],agreement[keep]
        if len(xyz)<8 or mad/z>.12:return self._reject('depth_scatter')
        coverage=np.ptp(a,axis=0)/np.array([max(1,x2-x1),max(1,y2-y1)])
        eigenvalues=np.linalg.eigvalsh(np.cov(a.T))
        if min(coverage)<.2 or eigenvalues[0]<.02*eigenvalues[1]:
            return self._reject('concentrated_features')
        def reprojection(p):
            q=p@np.vstack([xyz.T,np.ones(len(xyz))])
            return (q[:2]/q[2]).T
        residual=np.maximum(np.linalg.norm(reprojection(p_l)-a,axis=1),np.linalg.norm(reprojection(p_r)-b,axis=1))
        if np.percentile(residual,95)>2.:return self._reject('reprojection_error')
        median_disparity=float(np.median(np.abs(a[:,0]-b[:,0])))
        disparity_noise=max(.5,float(np.median(fb)),float(np.median(residual)))
        relative_noise=disparity_noise/max(median_disparity,1e-6)
        if relative_noise>.12:return self._reject('uncertain_small_disparity')
        midpoint=np.median(xyz,axis=0)-np.array([rectifier.center_right/2,0,0])
        x,y,z=map(float,midpoint)
        confidence=min(1.,len(xyz)/30)*.4+math.exp(-float(np.median(fb))/.5)*.3+math.exp(-float(mad/z)/.06)*.3
        if confidence<.55:return self._reject('low_confidence')
        return {"left_track":left.face_id,"right_track":right.face_id,"confidence":round(confidence,4),
                "position":{"x_m":x,"y_m":y,"z_m":z,"range_m":float(np.linalg.norm(midpoint)),
                            "yaw_deg":math.degrees(math.atan2(x,z)),
                            "pitch_deg":math.degrees(math.atan2(-y,math.hypot(x,z))),
                            "virtual_u_px":float(p_l[0,0]*x/z+width/2),
                            "virtual_v_px":float(p_l[1,1]*y/z+height/2),
                            "frame":"rectified_camera_midpoint","source":"calibrated_sparse_stereo",
                            "inliers":len(xyz),"epipolar_p95_px":float(np.percentile(dy,95)),
                            "reprojection_p95_px":float(np.percentile(residual,95)),
                            "depth_mad_m":float(mad),"known_distance_accuracy_verified":False,
                            "patch_correlation_median":float(np.median(agreement)),"feature_coverage_xy":coverage.tolist(),
                            "disparity_noise_floor_px":disparity_noise,"estimated_depth_noise_std_m":float(z*relative_noise),
                            "uncertainty_scope":"measurement_noise_estimate_excludes_calibration_and_scale_error",
                            "calibration_fingerprint":rectifier.fingerprint}}

    def match(self,images,targets,rectifier):
        self.rejections={}
        if not getattr(rectifier,"metric_ready",False):
            self.metrics={"status":"alignment_only","valid_pairs":0,"distance_output_enabled":False}
            return []
        gray={eye:cv2.cvtColor(image,cv2.COLOR_BGR2GRAY) for eye,image in images.items()}
        size=images["left"].shape[1::-1]
        projections=rectifier.projection_for_size(size)
        # All faces remain detected; bound the more expensive spatial estimates.
        observed={eye:sorted((t for t in targets.get(eye,[]) if is_observed(t)),
                             key=lambda t:t.score,reverse=True)[:4] for eye in ("left","right")}
        candidates=[]; attempted=0
        for left in observed["left"]:
            h=max(1,left.y2-left.y1)
            compatible=[r for r in observed["right"] if .5<(r.y2-r.y1)/h<2
                        and abs(left.center[1]-r.center[1])<.4*h]
            for right in compatible:
                attempted+=1
                result=self._pair(gray["left"],gray["right"],left,right,projections,rectifier)
                if result: candidates.append(result)
        unique=[]
        for item in candidates:
            others=[r["confidence"] for r in candidates if r is not item
                    and (r["left_track"]==item["left_track"] or r["right_track"]==item["right_track"])]
            if not others or item["confidence"]-max(others)>.10:
                unique.append(item)
        self.metrics={"status":"active","candidate_pairs":attempted,"valid_pairs":len(unique),
                      "rejections":dict(self.rejections),"distance_output_enabled":True,
                      "source":"calibrated sparse feature correspondences", "maximum_faces_per_eye":4,
                      "known_distance_accuracy_verified":False}
        return unique
