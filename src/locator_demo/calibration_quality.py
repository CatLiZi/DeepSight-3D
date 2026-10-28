"""Independent validation groups, robust stereo fitting and inspectable residuals."""
from __future__ import annotations

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix


def validation_split(samples,group_span_s=4.,purge_s=.7):
    """Keep adjacent captures together and purge train neighbours of validation."""
    times=[s.get('captured_monotonic') for s in samples]
    if not all(t is not None and np.isfinite(t) for t in times):
        hold=[i for i in range(len(samples)) if i%5==4]
        return {'method':'every_fifth_view_without_timestamps','train':[i for i in range(len(samples)) if i not in hold],
                'holdout':hold,'purged':[],'temporal_separation_verified':False}
    groups=[]
    for i in sorted(range(len(times)),key=lambda n:times[n]):
        if not groups or times[i]-times[groups[-1][0]]>=group_span_s:groups.append([])
        groups[-1].append(i)
    hold=sorted(i for g,items in enumerate(groups) if g%5==4 for i in items)
    train=[];purged=[]
    for i in range(len(samples)):
        if i in hold:continue
        (purged if any(abs(times[i]-times[j])<=purge_s for j in hold) else train).append(i)
    if len(hold)<4 or len(train)<16:
        raise ValueError('独立验证需要更多分开的采集姿态与时间段，当前有效训练或验证组不足')
    return {'method':'four_second_capture_groups_with_neighbour_purge','train':train,'holdout':hold,
            'purged':purged,'groups':groups,'group_span_s':group_span_s,'purge_s':purge_s,
            'temporal_separation_verified':True}


def robust_stereo_refine(objects,left,right,k_l,d_l,k_r,d_r,rotation,translation,size):
    """Soft-L1 joint bundle adjustment; no corner or held-out view is removed."""
    objects=[np.asarray(o,np.float64) for o in objects]
    left=[np.asarray(a,np.float64) for a in left];right=[np.asarray(b,np.float64) for b in right]
    def pack_camera(k,d):return np.r_[k[0,0],k[1,1],k[0,2],k[1,2],np.asarray(d).ravel()[:4]]
    def camera(x):
        return np.array([[x[0],0.,x[2]],[0.,x[1],x[3]],[0.,0.,1.]]),np.r_[x[4:8],0.]
    poses=[]
    for obj,pts in zip(objects,left):
        ok,rv,tv=cv2.solvePnP(obj,pts,k_l,d_l)
        if not ok:raise ValueError('训练样本初始位姿求解失败')
        poses.extend(np.r_[rv.ravel(),tv.ravel()])
    initial=np.r_[pack_camera(k_l,d_l),pack_camera(k_r,d_r),cv2.Rodrigues(rotation)[0].ravel(),translation.ravel(),poses]
    lengths=[len(o)*4 for o in objects];offsets=np.r_[0,np.cumsum(lengths)]
    def evaluate(x,want_jac=False):
        kl,dl=camera(x[:8]);kr,dr=camera(x[8:16]);rv=x[16:19];tv=x[19:22]
        residual=np.empty(offsets[-1]);jac=lil_matrix((offsets[-1],len(x))) if want_jac else None
        for i,(obj,a,b) in enumerate(zip(objects,left,right)):
            pose=x[22+i*6:28+i*6];n=len(obj)*2;at=offsets[i]
            composition=cv2.composeRT(pose[:3],pose[3:],rv,tv)
            pa,ja=cv2.projectPoints(obj,pose[:3],pose[3:],kl,dl)
            pb,jb=cv2.projectPoints(obj,composition[0],composition[1],kr,dr)
            residual[at:at+n]=(pa.reshape(-1,2)-a).ravel()
            residual[at+n:at+2*n]=(pb.reshape(-1,2)-b).ravel()
            if want_jac:
                jac[at:at+n,:8]=ja[:,6:14]
                jac[at:at+n,22+i*6:28+i*6]=ja[:,:6]
                jac[at+n:at+2*n,8:16]=jb[:,6:14]
                for column,drot,dtrans in ((16,composition[4],composition[8]),(19,composition[5],composition[9]),
                                           (22+i*6,composition[2],composition[6]),(25+i*6,composition[3],composition[7])):
                    jac[at+n:at+2*n,column:column+3]=jb[:,:3]@drot+jb[:,3:6]@dtrans
        return jac.tocsr() if want_jac else residual
    lower=np.full(len(initial),-np.inf);upper=-lower
    for start in (0,8):
        lower[start:start+4]=[size[0]*.1,size[1]*.1,-size[0]*.5,-size[1]*.5]
        upper[start:start+4]=[size[0]*5,size[1]*5,size[0]*1.5,size[1]*1.5]
    # The original solve must already be physically plausible; never clip it to fit bounds.
    if np.any(initial<=lower) or np.any(initial>=upper):raise ValueError('初始相机参数不在合理范围')
    result=least_squares(evaluate,initial,jac=lambda x:evaluate(x,True),bounds=(lower,upper),
                         loss='soft_l1',f_scale=1.5,x_scale='jac',max_nfev=150,
                         ftol=1e-7,xtol=1e-7,gtol=1e-7)
    kl,dl=camera(result.x[:8]);kr,dr=camera(result.x[8:16])
    residual=evaluate(result.x)
    rms=float(np.sqrt(np.sum(residual**2)/(sum(len(o) for o in objects)*2)))
    metadata={'method':'soft_l1_bundle_adjustment','scale_px':1.5,'converged':bool(result.success),
              'evaluations':result.nfev,'termination':str(result.message),'all_training_corners_retained':True,
              'training_corners':sum(len(o) for o in objects),'heldout_used_for_fit':False,
              'initial_unweighted_rms_px':float(np.sqrt(np.sum(evaluate(initial)**2)/(sum(len(o) for o in objects)*2))),
              'final_unweighted_rms_px':rms}
    rig_rotation=cv2.Rodrigues(result.x[16:19])[0]
    positive=True
    for i,obj in enumerate(objects):
        pose=result.x[22+i*6:28+i*6]
        xyz=(cv2.Rodrigues(pose[:3])[0]@obj.T).T+pose[3:]
        other=(rig_rotation@xyz.T).T+result.x[19:22]
        positive=positive and bool((xyz[:,2]>0).all() and (other[:,2]>0).all())
    metadata['positive_training_depth']=positive
    return rms,kl,dl,kr,dr,rig_rotation,result.x[19:22].reshape(3,1),metadata


def view_residuals(objects,left,right,k_l,d_l,k_r,d_r,rotation,translation,split):
    """Report every point/view; validation errors are never silently trimmed."""
    rows=[]
    for i,(obj,a,b) in enumerate(zip(objects,left,right)):
        ok,rv,tv=cv2.solvePnP(obj,a,k_l,d_l)
        if not ok:raise ValueError('样本位姿求解失败')
        rb=cv2.Rodrigues(rv)[0]
        prediction=cv2.projectPoints(obj,cv2.Rodrigues(rotation@rb)[0],rotation@tv+translation,k_r,d_r)[0].reshape(-1,2)
        cross=np.linalg.norm(prediction-b,axis=1);row={'index':i,'role':'holdout' if i in split['holdout'] else ('train' if i in split['train'] else 'purged'),
            'corners':len(obj),'cross_projection_p95_px':float(np.percentile(cross,95)),
            'cross_projection_median_px':float(np.median(cross)), 'cross_projection_errors_px':cross.tolist()}
        for eye,pts,k,d in (('left',a,k_l,d_l),('right',b,k_r,d_r)):
            uv=cv2.undistortPoints(pts.reshape(-1,1,2),k,d,P=k).reshape(-1,2)
            h,_=cv2.findHomography(obj[:,:2],uv)
            if h is None:raise ValueError('样本角点无法构成平面投影')
            plane=cv2.perspectiveTransform(obj[:,:2].reshape(-1,1,2),h).reshape(-1,2)
            err=np.linalg.norm(plane-uv,axis=1)
            row[eye+'_planar_p95_px']=float(np.percentile(err,95))
        row['planar_fit_warning']=max(row['left_planar_p95_px'],row['right_planar_p95_px'])>3.
        rows.append(row)
    return {'views':rows,'warning_views':sum(row['planar_fit_warning'] for row in rows),
            'all_validation_points_retained':True,'paper_deformation_confirmed':False,
            'note':'Large plane residuals may reflect detection, motion, lens-model or board-planarity errors; they do not establish a physical cause.'}
