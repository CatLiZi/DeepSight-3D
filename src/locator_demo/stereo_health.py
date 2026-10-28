"""Read-only rectified-pair diagnostics, never a replacement for calibration."""
import cv2
import numpy as np


def check_pair(left,right,*,alignment_loaded=True):
    if left is None or right is None or left.shape[:2]!=right.shape[:2]:
        return dict(status='unavailable',reason='missing_or_different_eye_sizes',calibration_changed=False)
    height,width=left.shape[:2];scale=min(1.,960/width)
    images=[cv2.cvtColor(cv2.resize(im,None,fx=scale,fy=scale),cv2.COLOR_BGR2GRAY)
            if im.ndim==3 else cv2.resize(im,None,fx=scale,fy=scale) for im in (left,right)]
    if any(float(np.mean(im<5))>.995 or float(np.mean(im>250))>.995 for im in images):
        return dict(status='unavailable',reason='covered_or_overexposed_eye',calibration_changed=False)
    if not alignment_loaded:
        return dict(status='unverified',reason='no_alignment_profile',calibration_changed=False)
    # Subpixel feature locations avoid multiplying ORB's small-image rounding
    # error back into an apparent native-resolution alignment error.
    detector=cv2.SIFT_create(nfeatures=1800,contrastThreshold=.025)
    a,da=detector.detectAndCompute(images[0],None);b,db=detector.detectAndCompute(images[1],None)
    if da is None or db is None or min(len(a),len(b))<20:
        return dict(status='unverified',reason='insufficient_texture',calibration_changed=False)
    matcher=cv2.BFMatcher(cv2.NORM_L2)
    forward={m.queryIdx:m for pair in matcher.knnMatch(da,db,k=2) if len(pair)==2
             for m,n in [pair] if m.distance < .72*n.distance}
    reverse={m.queryIdx:m.trainIdx for pair in matcher.knnMatch(db,da,k=2) if len(pair)==2
             for m,n in [pair] if m.distance < .72*n.distance}
    pairs=[m for m in forward.values() if reverse.get(m.trainIdx)==m.queryIdx]
    if len(pairs)<20:
        return dict(status='unverified',reason='insufficient_common_features',matches=len(pairs),calibration_changed=False)
    p=np.float32([a[m.queryIdx].pt for m in pairs]);q=np.float32([b[m.trainIdx].pt for m in pairs])
    _,mask=cv2.findFundamentalMat(p,q,cv2.FM_RANSAC,1.,.99)
    if mask is None or int(mask.sum())<20:
        return dict(status='unverified',reason='ambiguous_feature_geometry',calibration_changed=False)
    keep=mask.ravel().astype(bool);p,q=p[keep]/scale,q[keep]/scale
    # A well-aligned little patch cannot certify the rest of either image.
    spans=[np.ptp(points,axis=0)/[width,height] for points in (p,q)]
    if any(span[0]<.22 or span[1]<.18 for span in spans):
        return dict(status='unverified',reason='insufficient_feature_coverage',inliers=len(p),
                    calibration_changed=False)
    dy=np.abs(p[:,1]-q[:,1])
    median=float(np.median(dy));p90=float(np.percentile(dy,90))
    factor=width/1920.
    aligned=median<=max(1.,2.5*factor) and p90<=max(2.5,6.*factor)
    suspicious=median>max(5.,10.*factor) or p90>max(8.,20.*factor)
    return dict(status='misaligned' if suspicious else 'consistent' if aligned else 'degraded',
        reason='rectified_vertical_residual',
        matches=len(pairs),inliers=len(p),median_vertical_px=round(median,2),
        p90_vertical_px=round(p90,2),p95_vertical_px=round(float(np.percentile(dy,95)),2),
        horizontal_disparity_median_px=round(float(np.median(p[:,0]-q[:,0])),2),
        diagnostic_method='SIFT_mutual_matches_fundamental_RANSAC',height=height,width=width,
        mild_misalignment_blocks_tracking=False,
        calibration_changed=False,metric_accuracy_verified=False,sensor_exposure_sync_verified=False)
