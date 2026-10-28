"""Measured ChArUco stereo calibration, validation and common valid rectification.

No geometry is inferred from a photograph of the room or nominal CAD spacing.
Board dimensions define metres; synthetic profiles cannot be used by the runtime.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

import cv2
import numpy as np

from .calibration_quality import validation_split,robust_stereo_refine,view_residuals


PROJECT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = PROJECT / "calibration/stereo_camera.json"
BOARD_SPEC = {"squares_x":7,"squares_y":10,"square_length_m":.020,
              "marker_length_m":.015,"dictionary":"DICT_4X4_100","legacy_pattern":False}


def make_board(spec=None):
    spec = spec or BOARD_SPEC
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco,spec["dictionary"]))
    board = cv2.aruco.CharucoBoard((int(spec["squares_x"]),int(spec["squares_y"])),
                                  float(spec["square_length_m"]),float(spec["marker_length_m"]),dictionary)
    board.setLegacyPattern(bool(spec.get("legacy_pattern",False)))
    return board


def orient_pair(image, rotations):
    half=image.shape[1]//2
    return {eye:cv2.rotate(part,cv2.ROTATE_180) if rotations[i] == 180 else part
            for i,(eye,part) in enumerate((("left",image[:,:half]),("right",image[:,half:])))}


def detect_board_pair(images, spec=None):
    board=make_board(spec)
    detector=cv2.aruco.CharucoDetector(board)
    found={}
    for eye,image in images.items():
        corners,ids,_,_=detector.detectBoard(image)
        found[eye]={"corners":np.empty((0,2),np.float32) if corners is None else corners.reshape(-1,2),
                    "ids":np.empty(0,np.int32) if ids is None else ids.ravel()}
    shared=np.intersect1d(found["left"]["ids"],found["right"]["ids"])
    if len(shared)<12:
        raise ValueError(f"两眼共同角点 {len(shared)} 个，至少需要 12 个；请让两只镜头都看到标定板")
    obj=board.getChessboardCorners()[shared].astype(np.float32)
    if np.linalg.matrix_rank(obj[:,:2]-obj[:,:2].mean(axis=0))<2:
        raise ValueError("共同角点接近共线，请调整标定板位置")
    actual_spec=spec or BOARD_SPEC
    sample={"object_points":obj.tolist(),"ids":shared.tolist(),
            "board_extent_m":[actual_spec["squares_x"]*actual_spec["square_length_m"],
                              actual_spec["squares_y"]*actual_spec["square_length_m"]]}
    for eye in ("left","right"):
        lookup={int(i):p for i,p in zip(found[eye]["ids"],found[eye]["corners"])}
        sample[eye]=np.array([lookup[int(i)] for i in shared],np.float32).tolist()
    return sample


def view_descriptor(sample, size):
    obj=np.array(sample["object_points"],np.float32)[:,:2]
    pts=np.array(sample["left"],np.float32)
    homography,_=cv2.findHomography(obj,pts)
    if homography is None:
        raise ValueError("标定板角点无法构成平面投影")
    bw,bh=sample.get("board_extent_m",[.14,.20])
    corners=np.array([[0,0],[bw,0],[bw,bh],[0,bh]],np.float32).reshape(-1,1,2)
    quad=cv2.perspectiveTransform(corners,homography).reshape(-1,2)
    horizontal=quad[1]-quad[0]; vertical=quad[3]-quad[0]
    area=abs(cv2.contourArea(quad)) / (size[0]*size[1])
    if area<.008 or area>.95:
        raise ValueError("标定板在图中太小或太近，请调整距离")
    return np.array([*list(quad.mean(axis=0)/np.array(size)),np.log(max(area,1e-8))*.25,
                     np.arctan2(horizontal[1],horizontal[0])/np.pi,
                     np.linalg.norm(quad[2]-quad[3])/max(1,np.linalg.norm(horizontal)),
                     np.linalg.norm(quad[2]-quad[1])/max(1,np.linalg.norm(vertical))])


def solve_calibration(samples,size,*,board_spec=None,rotations=(180,180),
                      scale_confirmed=False,origin="real_charuco",device=None,diagnostic_only=False,
                      robust_refinement=True):
    if not scale_confirmed and not diagnostic_only:
        raise ValueError("请先用直尺核对打印的格子和 100 mm 核对尺，再确认实际尺寸")
    if len(samples)<20:
        raise ValueError(f"当前仅 {len(samples)} 组，需要至少 20 组不同姿态的双目样本")
    spec=board_spec or BOARD_SPEC
    objects=[np.array(s["object_points"],np.float32) for s in samples]
    left=[np.array(s["left"],np.float32) for s in samples]
    right=[np.array(s["right"],np.float32) for s in samples]
    for s,o,a,b in zip(samples,objects,left,right):
        if o.ndim!=2 or o.shape[1]!=3 or len(o)<12 or a.shape!=(len(o),2) or b.shape!=a.shape:
            raise ValueError('标定样本至少需要 12 个成对角点，物点与两眼点数必须一致')
        if not all(np.isfinite(v).all() for v in (o,a,b)):
            raise ValueError('标定角点存在非有限数值')
        if np.linalg.matrix_rank(o[:,:2]-o[:,:2].mean(axis=0))<2:
            raise ValueError('标定角点共线，无法稳定求解平面姿态')
        if 'ids' in s and (len(s['ids'])!=len(o) or len(set(s['ids']))!=len(o)):
            raise ValueError('标定角点 ID 重复或数量不一致')
    split=validation_split(samples)
    train,holdout=split['train'],split['holdout']
    def subset(items): return [items[i] for i in train]
    rms_l,k_l,d_l,rvecs,_=cv2.calibrateCamera(subset(objects),subset(left),size,None,None)
    rms_r,k_r,d_r,_,_=cv2.calibrateCamera(subset(objects),subset(right),size,None,None)
    initial_rms=[float(rms_l),float(rms_r)]
    # Limited board tilts can leave separate intrinsics weakly constrained.
    # Refine both cameras against the shared observations instead of freezing
    # those seeds; fix k3 to zero to avoid an unsupported high-order radial fit.
    d_l.ravel()[4]=0.; d_r.ravel()[4]=0.
    rms,k_l,d_l,k_r,d_r,rotation,translation,_,_=cv2.stereoCalibrate(
        subset(objects),subset(left),subset(right),k_l,d_l,k_r,d_r,size,
        flags=cv2.CALIB_USE_INTRINSIC_GUESS|cv2.CALIB_FIX_K3,
        criteria=(cv2.TERM_CRITERIA_EPS+cv2.TERM_CRITERIA_MAX_ITER,200,1e-8))
    optimization=None
    if robust_refinement:
        rms,k_l,d_l,k_r,d_r,rotation,translation,optimization=robust_stereo_refine(
            subset(objects),subset(left),subset(right),k_l,d_l,k_r,d_r,rotation,translation,size)
    rvecs=[]; separate_errors=[[],[]]
    for i in train:
        for eye,(points,k,d) in enumerate(((left,k_l,d_l),(right,k_r,d_r))):
            ok,rv,tv=cv2.solvePnP(objects[i],points[i],k,d)
            if not ok: raise ValueError("训练样本姿态求解失败")
            predicted=cv2.projectPoints(objects[i],rv,tv,k,d)[0].reshape(-1,2)
            separate_errors[eye].extend(np.sum((predicted-points[i])**2,axis=1).tolist())
            if eye==0: rvecs.append(rv)
    rms_l,rms_r=[float(np.sqrt(np.mean(e))) for e in separate_errors]
    r_l,r_r,p_l,p_r,q,_,_=cv2.stereoRectify(k_l,d_l,k_r,d_r,size,rotation,translation,
                                           flags=cv2.CALIB_ZERO_DISPARITY,alpha=-1)
    reference_focal=float((k_l[1,1]+k_r[1,1])/2)
    epipolar,reprojection=[],[]
    for i in holdout:
        l=cv2.undistortPoints(left[i].reshape(-1,1,2),k_l,d_l,R=r_l).reshape(-1,2)
        r=cv2.undistortPoints(right[i].reshape(-1,1,2),k_r,d_r,R=r_r).reshape(-1,2)
        # Compare at a camera-sized focal scale, independent of preview zoom.
        epipolar.extend((np.abs(l[:,1]-r[:,1])*reference_focal).tolist())
        ok,rv,tv=cv2.solvePnP(objects[i],left[i],k_l,d_l)
        if not ok:
            raise ValueError("留出样本姿态求解失败")
        board_rotation=cv2.Rodrigues(rv)[0]
        predicted_r=cv2.projectPoints(objects[i],cv2.Rodrigues(rotation@board_rotation)[0],
                                     rotation@tv+translation,k_r,d_r)[0].reshape(-1,2)
        reprojection.extend(np.linalg.norm(predicted_r-right[i],axis=1).tolist())
    desc=np.array([view_descriptor(s,size) for s in samples])
    rotations_deg=np.array([cv2.Rodrigues(v)[0] for v in rvecs])
    # Board normals must vary; simply translating an upright board is insufficient.
    normals=rotations_deg[:,:,2]
    pose_span_deg=float(np.degrees(np.arccos(np.clip(normals@normals.T,-1,1))).max())
    baseline=float(np.linalg.norm(translation))
    checks={"left_rms":rms_l<1.5,"right_rms":rms_r<1.5,"stereo_rms":rms<1.8,
            "holdout_epipolar":float(np.percentile(epipolar,95))<2.,
            "holdout_reprojection":float(np.percentile(reprojection,95))<3.,
            "baseline_plausible":.01<baseline<.5,
            "horizontal_baseline":abs(float(translation[0,0]))>.6*baseline,
            "horizontal_coverage":float(np.ptp(desc[:,0]))>.20,
            "vertical_coverage":float(np.ptp(desc[:,1]))>.15,
            "scale_diversity":float(np.ptp(desc[:,2]))>.08,
            "pose_diversity":pose_span_deg>12.}
    if optimization:
        checks['optimizer_converged']=optimization['converged']
        checks['positive_training_depth']=optimization['positive_training_depth']
    geometry_ok=all(checks.values())
    status=("diagnostic_passed" if diagnostic_only else "accepted") if geometry_ok else "rejected"
    quality={"status":status, "checks":checks,"geometry_checks_passed":geometry_ok,
             "fit_method":"robust_soft_l1_joint_intrinsics_fixed_k3" if robust_refinement else "joint_intrinsics_fixed_k3",
             "initial_separate_rms_px":initial_rms,"validation_split":split,"optimization":optimization,
             "left_rms_px":float(rms_l),"right_rms_px":float(rms_r),"stereo_rms_px":float(rms),
             "holdout_epipolar_p95_px":float(np.percentile(epipolar,95)),
             "epipolar_reference_focal_px":reference_focal,
             "holdout_reprojection_p95_px":float(np.percentile(reprojection,95)),
             "baseline_m":baseline,"pose_span_deg":pose_span_deg,
             "training_pairs":len(train),"holdout_pairs":len(holdout),
             "metric_scale_confirmed":bool(scale_confirmed),"diagnostic_only":bool(diagnostic_only),
             "known_distance_accuracy_verified":False}
    quality['residual_audit']=view_residuals(objects,left,right,k_l,d_l,k_r,d_r,rotation,translation,split)
    return {"schema":1,"origin":"real_charuco_diagnostic" if diagnostic_only else origin,
            "created_at":datetime.now(timezone.utc).isoformat(),"metres_are_nominal":not scale_confirmed,
            "image_size":list(size),"rotations":list(rotations),"board":spec,"device":device or {},
            "K_left":k_l.tolist(),"D_left":d_l.tolist(),"K_right":k_r.tolist(),"D_right":d_r.tolist(),
            "R":rotation.tolist(),"T":translation.tolist(),"quality":quality}


def largest_valid_rectangle(mask):
    """Largest common axis-aligned rectangle of valid remapped pixels."""
    heights=np.zeros(mask.shape[1],np.int32)
    best=(0,0,0,0); best_area=0
    for y,row in enumerate(mask):
        heights=np.where(row,heights+1,0)
        stack=[]
        for x in range(len(heights)+1):
            value=int(heights[x]) if x<len(heights) else 0
            start=x
            while stack and stack[-1][1]>value:
                at,height=stack.pop(); area=height*(x-at)
                if area>best_area:
                    best_area=area; best=(at,y-height+1,x-at,height)
                start=at
            if not stack or stack[-1][1]<value:
                stack.append((start,value))
    return best


def alignment_profile_path(metric_path):
    path=Path(metric_path)
    return path.with_name(path.stem+"-alignment"+path.suffix)


def make_alignment_preview_profile(diagnostic):
    """Allow pixel alignment after relative checks, while preserving metric rejection."""
    if diagnostic.get("origin")!="real_charuco_diagnostic":
        raise ValueError("画面对齐预览只接受真实采集的诊断结果")
    if not all(diagnostic.get("device",{}).get(key) for key in ("usb_vid","usb_pid")):
        raise ValueError("画面对齐预览需要绑定实际摄像头身份")
    required=("left_rms","right_rms","stereo_rms","holdout_epipolar","baseline_plausible",
              "horizontal_baseline","horizontal_coverage","vertical_coverage","scale_diversity","pose_diversity")
    quality=diagnostic.get("quality",{})
    checks={key:bool(quality.get("checks",{}).get(key,False)) for key in required}
    if quality.get('optimization'):
        checks.update({key:bool(quality.get('checks',{}).get(key,False))
                       for key in ('optimizer_converged','positive_training_depth')})
    if not all(checks.values()):raise ValueError("图像对齐检查未通过，不能加载预览")
    result=deepcopy(diagnostic)
    result.pop("runtime_activation_requested",None)
    result.update(origin="real_charuco_alignment_preview",source_full_quality=deepcopy(quality),
                  metric_geometry_enabled=False,metres_are_nominal=True,
                  alignment_preview_activation_requested=True,metric_runtime_activation_requested=False)
    result["quality"]={"status":"alignment_preview","alignment_checks":checks,
        "metric_scale_confirmed":False,"known_distance_accuracy_verified":False,
        "full_calibration_passed":False,"holdout_epipolar_p95_px":quality["holdout_epipolar_p95_px"]}
    return result


class StereoRectifier:
    def __init__(self,profile,*,expected_size=None,rotations=None,allow_synthetic=False,allow_alignment_only=False):
        alignment_only=profile.get("origin")=="real_charuco_alignment_preview"
        if profile.get("schema")!=1 or (profile.get("origin")!="real_charuco" and not allow_synthetic and not alignment_only):
            raise ValueError("仅允许实物标定文件；示例或合成标定不能用于设备")
        quality=profile.get("quality",{})
        if alignment_only:
            if not allow_alignment_only or quality.get("status")!="alignment_preview" or not quality.get("alignment_checks") or not all(quality["alignment_checks"].values()):
                raise ValueError("仅画面对齐预览参数不能作为三维标定加载")
            self.metric_ready=False
        else:
            if quality.get("status")!="accepted" or not quality.get("metric_scale_confirmed") or not quality.get("checks") or not all(quality["checks"].values()):
                raise ValueError("标定质量或打印尺寸尚未通过验证")
            self.metric_ready=True
        self.size=tuple(map(int,profile["image_size"]))
        if expected_size is not None and tuple(expected_size)!=self.size:
            raise ValueError("标定分辨率与当前采集不匹配，需要重新标定")
        if rotations is not None and list(rotations)!=profile.get("rotations"):
            raise ValueError("镜头方向配置已改变，需要重新标定")
        def matrix(key,shape=None):
            a=np.array(profile[key],np.float64)
            if not np.isfinite(a).all() or shape is not None and a.shape!=shape:
                raise ValueError(f"Invalid calibration matrix: {key}")
            return a
        k_l,k_r=matrix("K_left",(3,3)),matrix("K_right",(3,3))
        d_l,d_r=matrix("D_left"),matrix("D_right")
        rotation,translation=matrix("R",(3,3)),matrix("T").reshape(3,1)
        if (min(k_l[0,0],k_l[1,1],k_r[0,0],k_r[1,1])<=0
                or any(not np.allclose(k[2],[0,0,1],atol=1e-9) or abs(k[1,0])>1e-9
                       or abs(np.linalg.det(k))<1e-9 for k in (k_l,k_r))
                or not np.allclose(rotation@rotation.T,np.eye(3),atol=1e-3)
                or not np.isclose(np.linalg.det(rotation),1.,atol=1e-3)):
            raise ValueError("Invalid camera intrinsics or rotation")
        if not .01<np.linalg.norm(translation)<.5:
            raise ValueError("Invalid metric baseline")
        r_l,r_r,p_l,p_r,self.q,_,_=cv2.stereoRectify(k_l,d_l,k_r,d_r,self.size,rotation,translation,
                                                  flags=cv2.CALIB_ZERO_DISPARITY,alpha=-1)
        # Bearing-only geometry needs rotations and intrinsics, never metric Q.
        half_rotation=cv2.Rodrigues(cv2.Rodrigues(rotation)[0]*.5)[0]
        self.rectified_to_rig=half_rotation@r_l.T
        if not self.metric_ready:self.q=None
        self.maps={eye:cv2.initUndistortRectifyMap(k,d,r,p,self.size,cv2.CV_32FC1)
                   for eye,k,d,r,p in (("left",k_l,d_l,r_l,p_l),("right",k_r,d_r,r_r,p_r))}
        white=np.full(self.size[::-1],255,np.uint8)
        masks=[cv2.remap(white,*maps,cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT)>254 for maps in self.maps.values()]
        self.crop=largest_valid_rectangle(masks[0]&masks[1])
        x,y,w,h=self.crop
        if w<self.size[0]*.35 or h<self.size[1]*.35:
            raise ValueError("共同有效视野太小，请检查安装和标定")
        shift=np.array([[1,0,-x],[0,1,-y],[0,0,1]],np.float64)
        self.projections={"left":shift@p_l,"right":shift@p_r}
        self.center_right=-float(p_r[0,3]/p_r[0,0])
        self.baseline=abs(self.center_right)
        self.fingerprint=hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest()
        self.profile=profile

    def apply(self,images):
        x,y,w,h=self.crop
        output={}
        for eye,image in images.items():
            if image.shape[1::-1]!=self.size:
                raise ValueError("Rectification input size mismatch")
            output[eye]=cv2.remap(image,*self.maps[eye],cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT)[y:y+h,x:x+w]
        return output

    def projection_for_size(self,size):
        if not self.metric_ready:
            raise ValueError("仅画面对齐预览，三维投影输出关闭")
        sx,sy=size[0]/self.crop[2],size[1]/self.crop[3]
        scale=np.diag([sx,sy,1.])
        return {eye:scale@p for eye,p in self.projections.items()}

    def raw_center(self,eye,point,size):
        x,y,w,h=self.crop
        center=(float(point[0]*w/size[0]+x),float(point[1]*h/size[1]+y))
        return tuple(float(cv2.getRectSubPix(m,(1,1),center)[0,0]) for m in self.maps[eye])

    def bearing_rays(self,eye,points,size):
        """Scale-free rays in the optical-axis bisector frame; no triangulation."""
        sx,sy=size[0]/self.crop[2],size[1]/self.crop[3]
        k=np.diag([sx,sy,1.])@self.projections[eye][:,:3]
        uv=np.asarray(points,np.float64).reshape(-1,2)
        rays=(np.linalg.inv(k)@np.c_[uv,np.ones(len(uv))].T).T
        return (self.rectified_to_rig@rays.T).T,k

    def direction_projection(self,size):
        scale=np.diag([size[0]/self.crop[2],size[1]/self.crop[3],1.])
        return {eye:(scale@p[:,:3]@self.rectified_to_rig.T).tolist() for eye,p in self.projections.items()}

    def metrics(self):
        return {"status":"active" if self.metric_ready else "alignment_preview",
                "calibrated":self.metric_ready,"rectification_enabled":True,"metric_ready":self.metric_ready,
                "message":"共同校正已加载" if self.metric_ready else "仅画面对齐预览；正式测距未通过，距离输出关闭",
                "crop_xywh":list(self.crop),
                "output_size":list(self.crop[2:]),"retained_pixel_area":float(self.crop[2]*self.crop[3]/np.prod(self.size)),
                "baseline_m":self.baseline if self.metric_ready else None,"fingerprint":self.fingerprint,
                "rectification_scaling":"native_focal_then_common_crop",
                "known_distance_accuracy_verified":False,
                "frame":"rectified_camera_midpoint" if self.metric_ready else "rectified_eye_pixels",
                "relative_pose_refinement":deepcopy(self.profile.get('alignment_refinement',{})),
                "horizontal_disparity_preserved":True}


class CalibrationStabilityGate:
    """Require a short stationary window before saving a calibration pose."""
    def __init__(self,minimum_span_s=1.4,max_median_speed_px_s=30.,max_p90_speed_px_s=50.):
        self.minimum_span_s=minimum_span_s
        self.max_median_speed_px_s=max_median_speed_px_s; self.max_p90_speed_px_s=max_p90_speed_px_s
        self.reset()

    def reset(self):
        self.history=[]

    def observe(self,sample,when):
        if self.history and (when<=self.history[-1][0] or when-self.history[-1][0]>1.5):self.reset()
        self.history=[entry for entry in self.history if when-entry[0]<=2.2]
        medians=[];percentiles=[]
        for before_time,old in self.history:
            shared=sorted(set(old["ids"])&set(sample["ids"]))
            # Four non-collinear tracks suffice to observe plane motion. The
            # current stereo sample still requires >=12 common calibration corners.
            obj_by_id=dict(zip(sample["ids"],sample["object_points"]))
            obj=np.array([obj_by_id[i][:2] for i in shared])
            if len(shared)<4 or np.linalg.matrix_rank(obj-obj.mean(axis=0))<2:
                self.reset();break
            for eye in ("left","right"):
                before=dict(zip(old["ids"],old[eye]));after=dict(zip(sample["ids"],sample[eye]))
                shifts=np.linalg.norm(np.array([before[i] for i in shared])-np.array([after[i] for i in shared]),axis=1)
                elapsed=when-before_time
                medians.append(float(np.median(shifts))/elapsed);percentiles.append(float(np.percentile(shifts,90))/elapsed)
            if max(medians)>self.max_median_speed_px_s or max(percentiles)>self.max_p90_speed_px_s:
                self.reset();break
        self.history.append((when,deepcopy(sample)))
        span=when-self.history[0][0]
        metrics={"span_s":float(span),"frames":len(self.history),
                 "maximum_median_speed_px_s":max(medians,default=0.),"maximum_p90_speed_px_s":max(percentiles,default=0.)}
        return len(self.history)>=3 and span+1e-6>=self.minimum_span_s,metrics


class StereoCalibrationSession:
    def __init__(self,capture,rotations,*,directory=None,profile_path=None):
        self.capture=capture; self.rotations=tuple(rotations)
        self.directory=Path(directory or PROJECT/"calibration/sessions")
        self.profile_path=Path(profile_path or DEFAULT_PROFILE)
        self.lock=threading.RLock(); self.stop_event=threading.Event(); self.worker=None
        self.samples=[]; self.sequences=set(); self.descriptors=[]; self.session_path=None
        self.spec=dict(BOARD_SPEC); self.scale_confirmed=False; self.size=None
        self.state={"status":"not_started","pairs":0,"minimum_pairs":20,"message":"先打印并核对尺寸，再开始采集",
                    "board":dict(self.spec),"automatic_motion":False,"profile_exists":self.profile_path.exists(),
                    "alignment_preview_exists":alignment_profile_path(self.profile_path).exists()}
        self.preview=""; self.last_sequence=0
        self.stability=CalibrationStabilityGate()

    def start(self,*,square_mm=20.,scale_confirmed=False):
        if not 10<=float(square_mm)<=50:
            raise ValueError("格子尺寸应在 10..50 mm")
        with self.lock:
            if self.state["status"]=="solving":
                raise ValueError("正在计算标定，请等待本次计算结束")
        self.stop()
        with self.lock:
            self.spec={**BOARD_SPEC,"square_length_m":square_mm/1000,"marker_length_m":square_mm*.75/1000}
            self.scale_confirmed=bool(scale_confirmed)
            self.samples=[]; self.sequences=set(); self.descriptors=[]
            self.preview=""; self.size=None; self.last_sequence=0
            self.stability.reset()
            self.state.pop("quality",None)
            self.session_path=self.directory/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")+"-"+uuid4().hex)
            self.session_path.mkdir(parents=True,exist_ok=False)
            self.state.update(status="collecting",pairs=0,message="移到新位置后保持纸面平展并停稳 4–5 秒；先定点确认，再做整组动作",board=dict(self.spec))
            self.stop_event.clear()
            self.worker=threading.Thread(target=self._run,daemon=True); self.worker.start()
        return self.snapshot()

    def resume(self):
        with self.lock:
            if self.session_path is None or self.state["status"] not in ("collected","rejected","error"):
                raise ValueError("没有可以继续采集的暂停分组")
            if len(self.samples)>=45:
                raise ValueError("本组已达 45 组，请重新开始并覆盖更多不同姿态")
            # Continue the same measured board and saved observations; never mix scales.
            self.state.pop("quality",None)
            self.state.update(status="collecting",message="继续同一组采集；保持原打印板尺寸、相机安装与焦点")
            self.stability.reset()
            self.stop_event.clear()
            self.worker=threading.Thread(target=self._run,daemon=True); self.worker.start()
        return self.snapshot()

    def _run(self):
        while not self.stop_event.wait(.7):
            try:
                self.capture_one()
            except Exception as exc:
                with self.lock: self.state["message"]=str(exc)

    def capture_one(self):
        import base64
        with self.lock:
            if self.state["status"]!="collecting":
                raise ValueError("请先开始标定采集")
            sample=self.capture.latest()
            if sample is None or time.perf_counter()-sample.captured_monotonic>.5:
                raise ValueError("没有新鲜相机帧")
            if sample.sequence==self.last_sequence: return False
            self.last_sequence=sample.sequence
            image=cv2.imdecode(np.frombuffer(sample.jpeg,np.uint8),cv2.IMREAD_COLOR)
            images=orient_pair(image,self.rotations); size=images["left"].shape[1::-1]
            self.size=size
            try:
                points=detect_board_pair(images,self.spec)
            except ValueError:
                self.stability.reset()
                self.state["stability"]=None
                raise
            ready,stability=self.stability.observe(points,sample.captured_monotonic)
            self.state["stability"]=stability
            if not ready:
                raise ValueError(f"请保持纸面平展并停稳 4–5 秒；稳定观察 {stability['span_s']:.1f}/1.4 秒")
            points["stability"]=stability
            # Geometry scales with the measured square size, independent of print DPI.
            desc=view_descriptor(points,size)
            if any(np.linalg.norm(desc-old)<.065 for old in self.descriptors):
                raise ValueError(f"已有 {len(self.samples)} 组：姿态与前面太相似，请移动到新位置或倾斜标定板")
            points.update(sequence=sample.sequence,captured_monotonic=sample.captured_monotonic)
            self.samples.append(points); self.descriptors.append(desc); self.sequences.add(sample.sequence)
            (self.session_path/f"pair-{len(self.samples):03d}.jpg").write_bytes(sample.jpeg)
            self.state.update(pairs=len(self.samples),message="样本已保存；继续覆盖边缘、改变距离并向不同方向倾斜")
            for eye in images:
                for p in points[eye]: cv2.circle(images[eye],tuple(np.rint(p).astype(int)),4,(0,220,100),2)
            preview=cv2.resize(np.hstack([images["left"],images["right"]]),(1280,360))
            self.preview=base64.b64encode(cv2.imencode(".jpg",preview)[1]).decode()
            self._persist()
            if len(self.samples)>=45:
                self.stop_event.set(); self.state["status"]="collected"
            return True

    def _persist(self):
        record={"board":self.spec,"image_size":list(self.size),"rotations":list(self.rotations),
                "metric_scale_confirmed":self.scale_confirmed,"samples":self.samples}
        path=self.session_path/"observations.json"
        temp=path.with_suffix(".tmp"); temp.write_text(json.dumps(record,indent=2),encoding="utf-8"); temp.replace(path)

    def solve(self):
        self.stop()
        with self.lock:
            self.state.update(status="solving",message="正在拟合内外参并检查留出样本")
            samples=deepcopy(self.samples); size=self.size; spec=dict(self.spec)
        try:
            profile=solve_calibration(samples,size,board_spec=spec,rotations=self.rotations,
                                      scale_confirmed=self.scale_confirmed,
                                      device={"usb_vid":self.capture.usb_vid,"usb_pid":self.capture.usb_pid})
            with self.lock:
                self.state["quality"]=profile["quality"]
                (self.session_path/"calibration-result.json").write_text(json.dumps(profile,indent=2),encoding="utf-8")
                if profile["quality"]["status"]!="accepted":
                    self.state.update(status="rejected",message="质量检查未通过，请查看失败项并补充不同姿态的样本")
                    return self.snapshot()
                # Constructing maps also verifies common valid FOV before activation.
                StereoRectifier(profile,expected_size=size,rotations=self.rotations)
                self.profile_path.parent.mkdir(parents=True,exist_ok=True)
                temp=self.profile_path.with_suffix(".tmp")
                temp.write_text(json.dumps(profile,indent=2),encoding="utf-8"); temp.replace(self.profile_path)
                self.state.update(status="accepted",message="标定通过；可加载两眼共同校正与空间估计",profile_exists=True)
                return self.snapshot()
        except Exception as exc:
            with self.lock: self.state.update(status="error",message=str(exc))
            raise

    def snapshot(self,include_preview=False):
        with self.lock:
            return {**deepcopy(self.state),"scale_confirmed":self.scale_confirmed,
                    "session":str(self.session_path) if self.session_path else None,
                    "preview":self.preview if include_preview else ""}

    def stop(self):
        self.stop_event.set()
        worker=self.worker
        if worker and worker is not threading.current_thread(): worker.join(timeout=5)
        with self.lock:
            if self.state["status"]=="collecting":
                self.state.update(status="collected",message="采集已暂停；已保存的本组样本保留，可继续补采")
