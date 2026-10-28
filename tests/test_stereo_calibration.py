"""Synthetic geometry and print-target checks; never calibrate live hardware."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from fastapi.testclient import TestClient

from locator_demo.stereo_calibration import (make_board,detect_board_pair,solve_calibration,
    StereoRectifier,StereoCalibrationSession,CalibrationStabilityGate,largest_valid_rectangle,view_descriptor,
    alignment_profile_path,make_alignment_preview_profile)
from locator_demo.stereo_geometry import StereoTargetGeometry
from locator_demo.calibration_quality import validation_split
from locator_demo.stereo_vision import StereoVisionTracker
from locator_demo.binocular import BinocularFusion
from locator_demo.vision import FaceTarget,normalized_center_error
from locator_demo.settings import CameraConfig,SavedSettings,save_settings,load_settings
from locator_demo.web.app import create_app


def profile(size=(640,360),roll=0.,baseline=.082):
    w,h=size; k=np.array([[500.,0,w/2],[0,500.,h/2],[0,0,1]])
    r=cv2.Rodrigues(np.array([0.,0.,roll]))[0]
    t=r@np.array([[-baseline],[0.],[0.]])
    return {"schema":1,"origin":"synthetic_test_only","image_size":list(size),"rotations":[180,180],
            "K_left":k.tolist(),"K_right":k.tolist(),"D_left":[0.]*5,"D_right":[0.]*5,
            "R":r.tolist(),"T":t.tolist(),"quality":{"status":"accepted","checks":{"synthetic":True},
            "metric_scale_confirmed":True,"known_distance_accuracy_verified":False}}


def synthetic_samples(count=25):
    rng=np.random.default_rng(905)
    obj=make_board().getChessboardCorners()
    k=np.array([[550.,0,320.],[0,545.,180.],[0,0,1]])
    r=cv2.Rodrigues(np.array([.015,-.025,.07]))[0]
    t=r@np.array([[-.082],[0.],[0.]])
    samples=[]
    for i in range(count):
        rv=rng.uniform([-.4,-.4,-.15],[.4,.4,.15])
        rb=cv2.Rodrigues(rv)[0]
        z=rng.uniform(.42,.90)
        center=np.array([rng.uniform(-.20,.20)*z,rng.uniform(-.13,.13)*z,z])
        tv=(center-rb@np.array([.07,.10,0])).reshape(3,1)
        a=cv2.projectPoints(obj,rv,tv,k,np.zeros(5))[0].reshape(-1,2)
        b=cv2.projectPoints(obj,cv2.Rodrigues(r@rb)[0],r@tv+t,k,np.zeros(5))[0].reshape(-1,2)
        samples.append({"object_points":obj.tolist(),"left":(a+rng.normal(0,.025,a.shape)).tolist(),
                        "right":(b+rng.normal(0,.025,b.shape)).tolist()})
    return samples


def alignment_source():
    p=profile();p.update(origin="real_charuco_diagnostic",device={"usb_vid":"1BCF","usb_pid":"0B15"})
    checks={key:True for key in ("left_rms","right_rms","stereo_rms","holdout_epipolar","baseline_plausible",
        "horizontal_baseline","horizontal_coverage","vertical_coverage","scale_diversity","pose_diversity")}
    checks["holdout_reprojection"]=False
    p["quality"]={"status":"rejected","checks":checks,"metric_scale_confirmed":False,
                  "holdout_epipolar_p95_px":.4,"holdout_reprojection_p95_px":5.}
    return p


class CalibrationTargetTests(unittest.TestCase):
    def test_capture_waits_for_still_frames_and_resets_when_board_moves(self):
        sample=synthetic_samples(1)[0];sample["ids"]=list(range(len(sample["left"])))
        gate=CalibrationStabilityGate()
        self.assertFalse(gate.observe(sample,1.)[0])
        self.assertFalse(gate.observe(sample,1.7)[0])
        self.assertTrue(gate.observe(sample,2.4)[0])
        moved=deepcopy(sample)
        for eye in ("left","right"):
            moved[eye]=(np.array(moved[eye])+np.array([80.,0.])).tolist()
        self.assertFalse(gate.observe(moved,3.1)[0])
        self.assertFalse(gate.observe(moved,3.8)[0])
        self.assertTrue(gate.observe(moved,4.5)[0])

    def test_capture_allows_small_handheld_drift_instead_of_requiring_fixed_pixels(self):
        sample=synthetic_samples(1)[0];sample["ids"]=list(range(len(sample["left"])))
        gate=CalibrationStabilityGate()
        ready=False
        for n,t in enumerate((1.,1.7,2.4)):
            shifted=deepcopy(sample)
            for eye in ("left","right"):
                shifted[eye]=(np.array(shifted[eye])+np.array([n*10.,n*2.])).tolist()
            ready,metrics=gate.observe(shifted,t)
        self.assertTrue(ready,metrics)

    def test_motion_tracking_tolerates_marker_dropout_with_noncollinear_shared_tracks(self):
        sample=synthetic_samples(1)[0]
        def part(ids):
            return {"ids":ids,**{key:[sample[key][i] for i in ids] for key in ("object_points","left","right")}}
        gate=CalibrationStabilityGate()
        self.assertFalse(gate.observe(part(list(range(12))),1.)[0])
        self.assertFalse(gate.observe(part([4,5,10,11]+list(range(12,24))),1.7)[0])
        self.assertTrue(gate.observe(part([4,5,10,11]+list(range(24,36))),2.4)[0])

    def test_centered_twenty_degree_board_tilt_is_a_distinct_calibration_view(self):
        obj=make_board().getChessboardCorners();k=np.array([[500.,0.,320],[0.,500.,180],[0.,0.,1.]])
        def pose(angle):
            rv=np.array([0.,np.radians(angle),0.]);r=cv2.Rodrigues(rv)[0]
            t=np.array([0.,0.,.55])-r@np.array([.07,.10,0.])
            uv=cv2.projectPoints(obj,rv,t,k,np.zeros(5))[0].reshape(-1,2)
            return {"object_points":obj.tolist(),"left":uv.tolist()}
        a=view_descriptor(pose(0),(640,360));b=view_descriptor(pose(20),(640,360))
        self.assertGreater(np.linalg.norm(a-b),.065)

    def test_stillness_does_not_bridge_a_missing_detection_or_large_frame_gap(self):
        sample=synthetic_samples(1)[0];sample["ids"]=list(range(len(sample["left"])))
        gate=CalibrationStabilityGate()
        for t in (1.,1.7,2.4):gate.observe(sample,t)
        self.assertFalse(gate.observe(sample,5.)[0])
        gate.reset()
        self.assertFalse(gate.observe(sample,5.7)[0])
    def test_generated_charuco_target_has_all_54_common_corners(self):
        board=make_board(); image=board.generateImage((700,1000))
        image=cv2.copyMakeBorder(image,20,20,20,20,cv2.BORDER_CONSTANT,value=255)
        image=cv2.cvtColor(image,cv2.COLOR_GRAY2BGR)
        detected=detect_board_pair({"left":image,"right":image.copy()})
        self.assertEqual(len(detected["ids"]),54)
        self.assertEqual(len(detected["object_points"]),54)
        self.assertAlmostEqual(max(p[0] for p in detected["object_points"]),.12,places=5)

    def test_empty_room_is_rejected_instead_of_creating_fake_calibration_points(self):
        blank=np.full((360,640,3),146,np.uint8)
        with self.assertRaisesRegex(ValueError,"共同角点"):
            detect_board_pair({"left":blank,"right":blank.copy()})

    def test_board_must_be_visible_in_both_eyes(self):
        image=cv2.cvtColor(make_board().generateImage((700,1000)),cv2.COLOR_GRAY2BGR)
        with self.assertRaises(ValueError):
            detect_board_pair({"left":image,"right":np.full_like(image,255)})

    def test_measured_square_size_controls_metric_object_coordinates(self):
        board=make_board({"squares_x":7,"squares_y":10,"square_length_m":.025,
                          "marker_length_m":.01875,"dictionary":"DICT_4X4_100"})
        self.assertAlmostEqual(float(board.getChessboardCorners()[:,0].max()),.15,places=5)

    def test_solver_requires_confirmed_print_size_and_enough_views(self):
        with self.assertRaisesRegex(ValueError,"直尺"):
            solve_calibration([], (640,360))
        with self.assertRaisesRegex(ValueError,"20"):
            solve_calibration([], (640,360),scale_confirmed=True)

    def test_view_descriptor_detects_repeated_pose(self):
        sample=synthetic_samples(1)[0]
        first=view_descriptor(sample,(640,360))
        second=view_descriptor(deepcopy(sample),(640,360))
        self.assertLess(np.linalg.norm(first-second),1e-6)

    def test_synthetic_solver_recovers_known_baseline_and_passes_holdout(self):
        solved=solve_calibration(synthetic_samples(),(640,360),scale_confirmed=True,origin="synthetic_test_only")
        self.assertEqual(solved["quality"]["status"],"accepted",solved["quality"])
        self.assertAlmostEqual(solved["quality"]["baseline_m"],.082,delta=.002)
        self.assertLess(solved["quality"]["holdout_epipolar_p95_px"],.2)
        self.assertFalse(solved["quality"]["known_distance_accuracy_verified"])

    def test_diagnostic_fit_keeps_unverified_scale_and_cannot_be_activated(self):
        solved=solve_calibration(synthetic_samples(),(640,360),diagnostic_only=True)
        self.assertEqual(solved["quality"]["status"],"diagnostic_passed",solved["quality"])
        self.assertFalse(solved["quality"]["metric_scale_confirmed"])
        self.assertTrue(solved["metres_are_nominal"])
        self.assertAlmostEqual(solved["quality"]["baseline_m"],.082,delta=.002)
        with self.assertRaises(ValueError):
            StereoRectifier(solved)

    def test_joint_fit_recovers_geometry_from_biased_separate_intrinsic_seeds(self):
        original=cv2.calibrateCamera; calls=[]
        def biased(*args,**kwargs):
            result=list(original(*args,**kwargs))
            k=result[1].copy(); factor=.82 if not calls else 1.15
            k[0,0]*=factor; k[1,1]*=factor; result[1]=k; calls.append(factor)
            return tuple(result)
        with patch("cv2.calibrateCamera",side_effect=biased):
            solved=solve_calibration(synthetic_samples(),(640,360),scale_confirmed=True,origin="synthetic_test_only")
        self.assertEqual(solved["quality"]["status"],"accepted",solved["quality"])
        self.assertAlmostEqual(solved["quality"]["baseline_m"],.082,delta=.002)
        self.assertLess(solved["quality"]["holdout_epipolar_p95_px"],.2)


class CalibrationRefinementTests(unittest.TestCase):
    def test_temporal_neighbours_stay_together_and_validation_boundary_is_purged(self):
        samples=[{'captured_monotonic':i*.5} for i in range(80)]
        split=validation_split(samples)
        self.assertTrue(split['temporal_separation_verified'])
        self.assertTrue(split['purged'])
        for group in split['groups']:
            self.assertFalse(set(group)&set(split['train']) and set(group)&set(split['holdout']))
        self.assertGreater(min(abs(samples[i]['captured_monotonic']-samples[j]['captured_monotonic'])
            for i in split['train'] for j in split['holdout']),.7)

    def test_one_short_capture_burst_does_not_pass_as_independent_validation(self):
        with self.assertRaisesRegex(ValueError,'独立验证'):
            validation_split([{'captured_monotonic':i*.1} for i in range(25)])

    def test_undated_photos_do_not_claim_verified_temporal_separation(self):
        split=validation_split(synthetic_samples())
        self.assertFalse(split['temporal_separation_verified'])
        self.assertFalse(set(split['train'])&set(split['holdout']))

    def test_outlier_resistant_fit_improves_geometry_without_hiding_bad_training_corners(self):
        samples=synthetic_samples(30);rng=np.random.default_rng(882)
        for i,sample in enumerate(samples):
            if i%5==4:continue
            for eye in ('left','right'):
                pts=np.array(sample[eye]);pts[[2,23,49]]+=rng.choice([-1,1],size=(3,2))*12
                sample[eye]=pts.tolist()
        ordinary=solve_calibration(samples,(640,360),diagnostic_only=True,robust_refinement=False)
        robust=solve_calibration(samples,(640,360),diagnostic_only=True)
        self.assertLess(robust['quality']['holdout_reprojection_p95_px'],ordinary['quality']['holdout_reprojection_p95_px']*.6)
        self.assertAlmostEqual(robust['quality']['baseline_m'],.082,delta=.001)
        self.assertEqual(robust['quality']['optimization']['training_corners'],24*54)
        self.assertEqual(robust['quality']['status'],'rejected')
        self.assertGreater(robust['quality']['stereo_rms_px'],1.8)

    def test_modifying_only_validation_pixels_cannot_change_fitted_cameras(self):
        from locator_demo.calibration_quality import robust_stereo_refine
        captured=[]
        def inspect_training(*args,**kwargs):
            captured.append(deepcopy(args[:3]));return robust_stereo_refine(*args,**kwargs)
        samples=synthetic_samples()
        changed=deepcopy(samples)
        for i,sample in enumerate(changed):
            if i%5==4:sample['right']=(np.array(sample['right'])+np.array([20.,0.])).tolist()
        with patch('locator_demo.stereo_calibration.robust_stereo_refine',side_effect=inspect_training):
            first=solve_calibration(samples,(640,360),diagnostic_only=True)
            second=solve_calibration(changed,(640,360),diagnostic_only=True)
        for old,new in zip(captured[0],captured[1]):
            for a,b in zip(old,new):np.testing.assert_array_equal(a,b)
        for key in ('K_left','K_right','R','T'):
            np.testing.assert_allclose(first[key],second[key],atol=.002 if key.startswith('K') else 2e-5)
        # Individual radial coefficients are weakly identifiable and can vary
        # at sparse-solver termination. Check their observable projection jointly
        # with K instead of a coefficient tolerance unrelated to pixel error.
        grid=np.array([[x,y,1.] for x in np.linspace(-.6,.6,7) for y in np.linspace(-.4,.4,5)])
        for eye in ('left','right'):
            projected=[cv2.projectPoints(grid,np.zeros(3),np.zeros(3),np.array(v['K_'+eye]),np.array(v['D_'+eye]))[0]
                       for v in (first,second)]
            self.assertLess(float(np.max(np.linalg.norm(projected[0]-projected[1],axis=-1))),.01)
        self.assertGreater(second['quality']['holdout_reprojection_p95_px'],15.)

    def test_bad_validation_corner_is_reported_and_never_trimmed(self):
        samples=synthetic_samples();samples[4]['right'][0][0]+=50
        solved=solve_calibration(samples,(640,360),diagnostic_only=True)
        audit=solved['quality']['residual_audit'];row=audit['views'][4]
        self.assertEqual(len(row['cross_projection_errors_px']),54)
        self.assertGreater(max(row['cross_projection_errors_px']),45.)
        self.assertTrue(audit['all_validation_points_retained'])
        self.assertFalse(audit['paper_deformation_confirmed'])

    def test_unconverged_optimizer_cannot_be_promoted_to_alignment_preview(self):
        from locator_demo.calibration_quality import robust_stereo_refine
        def unfinished(*args,**kwargs):
            result=list(robust_stereo_refine(*args,**kwargs));result[-1]['converged']=False;return result
        with patch('locator_demo.stereo_calibration.robust_stereo_refine',side_effect=unfinished):
            solved=solve_calibration(synthetic_samples(),(640,360),diagnostic_only=True,device={'usb_vid':'1BCF','usb_pid':'0B15'})
        self.assertEqual(solved['quality']['status'],'rejected')
        with self.assertRaises(ValueError):make_alignment_preview_profile(solved)

    def test_nonfinite_calibration_corner_is_rejected_before_optimization(self):
        samples=synthetic_samples();samples[0]['left'][0][0]=float('nan')
        with self.assertRaisesRegex(ValueError,'非有限'):solve_calibration(samples,(640,360),diagnostic_only=True)

    def test_duplicate_corner_ids_cannot_count_as_independent_evidence(self):
        samples=synthetic_samples()
        samples[0]['ids']=list(range(54));samples[0]['ids'][-1]=0
        with self.assertRaisesRegex(ValueError,'ID 重复'):solve_calibration(samples,(640,360),diagnostic_only=True)


class RectificationTests(unittest.TestCase):
    def test_alignment_preview_preserves_metric_rejection_and_requires_explicit_opt_in(self):
        source=alignment_source();before=deepcopy(source);p=make_alignment_preview_profile(source)
        self.assertEqual(source,before)
        self.assertFalse(p["source_full_quality"]["checks"]["holdout_reprojection"])
        with self.assertRaises(ValueError):StereoRectifier(p)
        rect=StereoRectifier(p,allow_alignment_only=True)
        self.assertFalse(rect.metric_ready)
        self.assertFalse(rect.metrics()["calibrated"])
        self.assertTrue(rect.metrics()["rectification_enabled"])
        self.assertIsNone(rect.metrics()["baseline_m"])
        self.assertIsNone(rect.q)
        with self.assertRaises(ValueError):rect.projection_for_size((640,360))

    def test_alignment_preview_rejects_bad_relative_geometry_and_synthetic_source(self):
        source=alignment_source();source["quality"]["checks"]["holdout_epipolar"]=False
        with self.assertRaises(ValueError):make_alignment_preview_profile(source)
        with self.assertRaises(ValueError):make_alignment_preview_profile(profile())

    def test_alignment_images_share_one_fully_valid_crop(self):
        rect=StereoRectifier(make_alignment_preview_profile(alignment_source()),allow_alignment_only=True)
        white=np.full((360,640,3),255,np.uint8);frames=rect.apply({"left":white,"right":white.copy()})
        self.assertEqual(frames["left"].shape,frames["right"].shape)
        self.assertTrue(all(im.min()==255 for im in frames.values()))
    def test_common_canvas_does_not_collapse_from_extrapolated_edge_distortion(self):
        p=profile(size=(1920,1080),baseline=.089)
        p.update(K_left=[[875.,0.,901.],[0.,876.,504.],[0.,0.,1.]],
                 K_right=[[881.,0.,909.],[0.,881.,496.],[0.,0.,1.]],
                 D_left=[[-.047,-.011,-.0055,.0064,0.]],
                 D_right=[[-.033,-.044,-.0015,-.0023,0.]],
                 R=cv2.Rodrigues(np.array([-.022,-.13,.07]))[0].tolist(),
                 T=[[-.089],[-.0047],[-.0052]])
        rect=StereoRectifier(p,allow_synthetic=True)
        self.assertGreater(rect.crop[2],1400)
        self.assertGreater(rect.crop[3],800)

    def test_common_rectangle_contains_only_valid_pixels(self):
        mask=np.zeros((8,12),bool); mask[2:7,3:10]=True; mask[1,5:8]=True
        x,y,w,h=largest_valid_rectangle(mask)
        self.assertEqual(w*h,35)
        self.assertTrue(mask[y:y+h,x:x+w].all())

    def test_empty_valid_region_is_not_fabricated(self):
        self.assertEqual(largest_valid_rectangle(np.zeros((4,8),bool)),(0,0,0,0))

    def test_real_runtime_rejects_synthetic_profile(self):
        with self.assertRaisesRegex(ValueError,"合成"):
            StereoRectifier(profile())

    def test_bad_quality_or_unconfirmed_scale_cannot_load(self):
        for key,value in (("metric_scale_confirmed",False),("status","rejected"),("checks",{})):
            data=profile(); data["quality"][key]=value
            with self.assertRaises(ValueError): StereoRectifier(data,allow_synthetic=True)

    def test_resolution_and_orientation_are_bound_to_profile(self):
        with self.assertRaisesRegex(ValueError,"分辨率"):
            StereoRectifier(profile(),expected_size=(1920,1080),allow_synthetic=True)
        with self.assertRaisesRegex(ValueError,"方向"):
            StereoRectifier(profile(),rotations=(0,0),allow_synthetic=True)

    def test_nonfinite_matrices_are_rejected(self):
        data=profile(); data["K_left"][0][0]=float("nan")
        with self.assertRaises(ValueError): StereoRectifier(data,allow_synthetic=True)

    def test_rolled_camera_pair_rectifies_to_common_horizontal_epipolar_lines(self):
        data=profile(roll=.09); rect=StereoRectifier(data,allow_synthetic=True)
        k=np.array(data["K_left"]); r=np.array(data["R"]); t=np.array(data["T"])
        points=np.array([[-.15,-.1,1.3],[.15,.1,2.],[0,0,.7]],float)
        raw_l=cv2.projectPoints(points,np.zeros(3),np.zeros(3),k,np.zeros(5))[0]
        raw_r=cv2.projectPoints(points,cv2.Rodrigues(r)[0],t,k,np.zeros(5))[0]
        r1,r2,p1,p2,*_=cv2.stereoRectify(k,np.zeros(5),k,np.zeros(5),(640,360),r,t,flags=cv2.CALIB_ZERO_DISPARITY,alpha=-1)
        a=cv2.undistortPoints(raw_l,k,np.zeros(5),R=r1,P=p1).reshape(-1,2)
        b=cv2.undistortPoints(raw_r,k,np.zeros(5),R=r2,P=p2).reshape(-1,2)
        x,y,_,_=rect.crop; a-=[x,y]; b-=[x,y]
        self.assertLess(float(np.max(np.abs(a[:,1]-b[:,1]))),1e-5)
        p=rect.projection_for_size(rect.crop[2:])
        reconstructed=cv2.triangulatePoints(p["left"],p["right"],a.T,b.T)
        reconstructed=(reconstructed[:3]/reconstructed[3]).T
        np.testing.assert_allclose(reconstructed,(r1@points.T).T,atol=1e-6)

    def test_common_crop_projections_remain_correct_after_processing_resize(self):
        rect=StereoRectifier(profile(roll=.08),allow_synthetic=True)
        p=rect.projection_for_size((320,180)); point=np.array([.12,.03,1.8,1.])
        pixels=[]
        for eye in ("left","right"):
            uv=p[eye]@point; pixels.append((uv[:2]/uv[2]).reshape(2,1))
        result=cv2.triangulatePoints(p["left"],p["right"],*pixels)
        np.testing.assert_allclose(result[:3,0]/result[3,0],point[:3],atol=1e-6)

    def test_both_rectified_outputs_have_identical_fully_valid_dimensions(self):
        rect=StereoRectifier(profile(roll=.09),allow_synthetic=True)
        white=np.full((360,640,3),255,np.uint8)
        outputs=rect.apply({"left":white,"right":white.copy()})
        self.assertEqual(outputs["left"].shape,outputs["right"].shape)
        self.assertTrue(all(image.min()==255 for image in outputs.values()))
        self.assertLessEqual(rect.metrics()["retained_pixel_area"],1.)


class StereoMetricTargetTests(unittest.TestCase):
    def test_alignment_only_never_returns_distance_even_with_good_texture(self):
        _,images,targets=self.scene()
        rect=StereoRectifier(make_alignment_preview_profile(alignment_source()),allow_alignment_only=True)
        geometry=StereoTargetGeometry()
        self.assertEqual(geometry.match(images,targets,rect),[])
        self.assertFalse(geometry.metrics["distance_output_enabled"])
    def scene(self,baseline=.082,distance=2.):
        rect=StereoRectifier(profile(baseline=baseline),allow_synthetic=True)
        rng=np.random.default_rng(928)
        gray=rng.integers(10,245,(360,640),dtype=np.uint8)
        gray=cv2.GaussianBlur(gray,(3,3),.6)
        disparity=500*baseline/distance
        right=cv2.warpAffine(gray,np.float32([[1,0,-disparity],[0,1,0]]),(640,360))
        images=rect.apply({"left":cv2.cvtColor(gray,cv2.COLOR_GRAY2BGR),"right":cv2.cvtColor(right,cv2.COLOR_GRAY2BGR)})
        x,y,_,_=rect.crop
        a=FaceTarget(180-x,70-y,310-x,240-y,face_id=1,score=.9,tracking_source="scrfd")
        b=replace(a,x1=a.x1-disparity,x2=a.x2-disparity,face_id=2)
        return rect,images,{"left":[a],"right":[b]}

    def test_real_correspondence_algorithm_recovers_two_metre_plane(self):
        rect,images,targets=self.scene()
        matches=StereoTargetGeometry().match(images,targets,rect)
        self.assertEqual(len(matches),1)
        self.assertAlmostEqual(matches[0]["position"]["z_m"],2.,delta=.06)
        self.assertGreaterEqual(matches[0]["position"]["inliers"],8)
        self.assertEqual(matches[0]["position"]["frame"],"rectified_camera_midpoint")

    def test_patch_check_tolerates_different_linear_brightness_between_eyes(self):
        rect,images,targets=self.scene()
        images['right']=np.clip(images['right'].astype(float)*.8+20,0,255).astype(np.uint8)
        matches=StereoTargetGeometry().match(images,targets,rect)
        self.assertEqual(len(matches),1)
        self.assertAlmostEqual(matches[0]['position']['z_m'],2.,delta=.08)

    def test_one_small_textured_patch_is_not_enough_spatial_support_for_a_face(self):
        rect,images,targets=self.scene();rng=np.random.default_rng(920)
        image=np.full((360,640),146,np.uint8);image[140:164,230:254]=rng.integers(20,240,(24,24),dtype=np.uint8)
        other=cv2.warpAffine(image,np.float32([[1,0,-20.5],[0,1,0]]),(640,360))
        images={eye:cv2.cvtColor(im,cv2.COLOR_GRAY2BGR) for eye,im in (('left',image),('right',other))}
        geometry=StereoTargetGeometry();self.assertEqual(geometry.match(images,targets,rect),[])

    def test_depth_noise_estimate_grows_with_distance_and_excludes_scale_accuracy(self):
        a,ia,ta=self.scene(distance=2.);b,ib,tb=self.scene(distance=4.)
        near=StereoTargetGeometry().match(ia,ta,a)[0]['position']
        far=StereoTargetGeometry().match(ib,tb,b)[0]['position']
        self.assertGreater(far['estimated_depth_noise_std_m'],near['estimated_depth_noise_std_m']*3)
        self.assertFalse(far['known_distance_accuracy_verified'])
        self.assertIn('excludes_calibration_and_scale',far['uncertainty_scope'])

    def test_tiny_disparity_is_rejected_even_when_texture_matches_well(self):
        rect,images,targets=self.scene(baseline=.012)
        geometry=StereoTargetGeometry();self.assertEqual(geometry.match(images,targets,rect),[])
        self.assertGreater(geometry.metrics['rejections'].get('uncertain_small_disparity',0),0)

    def test_reversed_usb_eye_order_supports_negative_disparity(self):
        rect,images,targets=self.scene(baseline=-.082)
        matches=StereoTargetGeometry().match(images,targets,rect)
        self.assertEqual(len(matches),1)
        self.assertAlmostEqual(matches[0]["position"]["z_m"],2.,delta=.06)

    def test_textureless_face_does_not_produce_a_distance(self):
        rect,images,targets=self.scene()
        for image in images.values(): image[:]=146
        self.assertEqual(StereoTargetGeometry().match(images,targets,rect),[])

    def test_missing_second_observation_cannot_produce_three_dimensional_position(self):
        rect,images,targets=self.scene(); targets["right"]=[]
        self.assertEqual(StereoTargetGeometry().match(images,targets,rect),[])

    def test_ambiguous_pairing_rejects_duplicate_right_candidates(self):
        rect,images,targets=self.scene()
        targets["right"].append(replace(targets["right"][0],face_id=3))
        self.assertEqual(StereoTargetGeometry().match(images,targets,rect),[])

    def test_wrong_disparity_sign_is_rejected(self):
        rect,images,targets=self.scene()
        images["left"],images["right"]=images["right"],images["left"]
        targets["left"],targets["right"]=targets["right"],targets["left"]
        self.assertEqual(StereoTargetGeometry().match(images,targets,rect),[])

    def test_fusion_uses_calibrated_correspondences_and_retains_one_space_position(self):
        rect,images,targets=self.scene()
        matches=StereoTargetGeometry().match(images,targets,rect)
        fusion=BinocularFusion()
        health={eye:{"valid":True} for eye in images}
        for now in (1,1.1,1.2,1.3):
            result=fusion.update(images,targets,health,now=now,geometric_matches=matches)
        self.assertEqual(len(result["summary"]["people"]),1)
        self.assertIsNotNone(result["target"].stereo_position)
        self.assertEqual(result["summary"]["people"][0]["association"],"calibrated_correspondence")

    def test_common_space_control_error_does_not_depend_on_selected_eye_bbox(self):
        p={"frame":"rectified_camera_midpoint","virtual_u_px":370.,"virtual_v_px":150.}
        a=FaceTarget(100,90,200,240,stereo_position=p)
        b=replace(a,x1=400,x2=500)
        self.assertEqual(normalized_center_error(a,frame_width=640,frame_height=360),
                         normalized_center_error(b,frame_width=640,frame_height=360))

    def test_switch_from_three_dimensional_to_two_dimensional_control_is_reconfirmed(self):
        rect,images,targets=self.scene()
        matches=StereoTargetGeometry().match(images,targets,rect)
        fusion=BinocularFusion(); health={eye:{"valid":True} for eye in images}
        for now in (1,1.1,1.2,1.3):
            result=fusion.update(images,targets,health,now=now,geometric_matches=matches)
        targets["right"]=[]
        fallback=fusion.update(images,targets,health,now=1.4)
        self.assertIsNone(fallback["target"].stereo_position)
        self.assertFalse(fallback["control_ready"])


class CalibrationIntegrationTests(unittest.TestCase):
    def test_tracker_loads_alignment_fallback_without_enabling_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            metric=Path(tmp)/"rig.json"
            alignment_profile_path(metric).write_text(json.dumps(make_alignment_preview_profile(alignment_source())),encoding="utf-8")
            tracker=StereoVisionTracker(video_size="1280x360",left_rotation=180,right_rotation=180,
                                        stereo_calibration_path=str(metric))
            try:
                tracker._load_rectification()
                self.assertIsNotNone(tracker.rectifier)
                self.assertFalse(tracker.rectifier.metric_ready)
                self.assertEqual(tracker.capture_metrics()["rectification"]["status"],"alignment_preview")
                self.assertFalse(tracker.capture_metrics()["stereo_depth_enabled"])
            finally:tracker.stop()
    def test_resuming_paused_group_preserves_samples_and_measured_scale(self):
        with tempfile.TemporaryDirectory() as tmp:
            session=StereoCalibrationSession(SimpleNamespace(latest=lambda:None),(180,180),directory=Path(tmp)/"sessions")
            try:
                session.start(square_mm=19.8,scale_confirmed=True); session.stop()
                folder=session.session_path
                session.samples.append({"sequence":1}); session.state["pairs"]=1
                session.state["quality"]={"status":"rejected"}
                resumed=session.resume()
                self.assertEqual(session.samples,[{"sequence":1}])
                self.assertEqual(session.session_path,folder)
                self.assertAlmostEqual(session.spec["square_length_m"],.0198)
                self.assertTrue(resumed["scale_confirmed"])
                self.assertNotIn("quality",resumed)
            finally: session.stop()

    def test_new_group_discards_stale_quality_and_preview_but_keeps_old_folder(self):
        with tempfile.TemporaryDirectory() as tmp,patch("locator_demo.stereo_calibration.datetime") as stamp:
            stamp.now.return_value.strftime.return_value="same-clock-tick"
            session=StereoCalibrationSession(SimpleNamespace(latest=lambda:None),(180,180),directory=Path(tmp)/"sessions")
            try:
                session.start(); session.stop(); folder=session.session_path
                session.preview="old"; session.state["quality"]={"status":"accepted"}
                current=session.start()
                self.assertTrue(folder.exists())
                self.assertNotEqual(folder,session.session_path)
                self.assertEqual(session.preview,"")
                self.assertNotIn("quality",current)
            finally: session.stop()

    def test_cannot_resume_unstarted_or_change_group_during_solve(self):
        session=StereoCalibrationSession(SimpleNamespace(latest=lambda:None),(180,180))
        with self.assertRaises(ValueError): session.resume()
        session.state["status"]="solving"
        with self.assertRaises(ValueError): session.start()

    def test_settings_preserve_calibration_path_and_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"settings.json"
            camera=CameraConfig(stereo_calibration_path="calibration/rig.json",stereo_geometry_enabled=False)
            save_settings(path,SavedSettings(camera=camera))
            self.assertEqual(load_settings(path).camera,camera)

    def test_no_profile_keeps_raw_detection_and_disables_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker=StereoVisionTracker(video_size="640x180",stereo_calibration_path=str(Path(tmp)/"none.json"))
            tracker._load_rectification()
            self.assertIsNone(tracker.rectifier)
            self.assertFalse(tracker.capture_metrics()["stereo_depth_enabled"])
            tracker.stop()

    def test_synthetic_profile_cannot_be_activated_through_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"synthetic.json"; path.write_text(json.dumps(profile()),encoding="utf-8")
            tracker=StereoVisionTracker(video_size="1280x360",stereo_calibration_path=str(path))
            tracker._load_rectification()
            self.assertIsNone(tracker.rectifier)
            self.assertEqual(tracker._rectification_status["status"],"invalid")
            tracker.stop()

    def test_calibration_does_not_start_with_a_fabricated_sample_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture=SimpleNamespace(latest=lambda:None,usb_vid="1BCF",usb_pid="0B15")
            session=StereoCalibrationSession(capture,(180,180),directory=Path(tmp)/"sessions",profile_path=Path(tmp)/"rig.json")
            self.assertEqual(session.snapshot()["pairs"],0)
            with self.assertRaises(ValueError): session.solve()
            self.assertFalse((Path(tmp)/"rig.json").exists())

    def test_print_pdf_is_served_without_starting_capture_or_motion(self):
        app=create_app(simulated=True)
        with TestClient(app) as client:
            response=client.get("/api/stereo-calibration/board.pdf")
            self.assertEqual(response.status_code,200)
            self.assertTrue(response.content.startswith(b"%PDF"))
            self.assertFalse(app.state.runtime.running)

    def test_calibration_start_always_stops_automatic_motion_and_cancel_pending(self):
        app=create_app(simulated=True)
        service=SimpleNamespace(start=lambda **kw:{"status":"collecting","pairs":0},snapshot=lambda:{"status":"collecting"})
        app.state.runtime.vision.calibration=service
        app.state.runtime.running=True
        with TestClient(app) as client:
            response=client.post("/api/stereo-calibration/start",json={"square_mm":20,"scale_confirmed":False})
            self.assertEqual(response.status_code,200)
            self.assertFalse(app.state.runtime.running)
            self.assertEqual(client.post("/api/demo/start").status_code,409)


if __name__=="__main__": unittest.main()
