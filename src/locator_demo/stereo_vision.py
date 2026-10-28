"""Same-frame binocular detection, isolated tracking and conservative arbitration."""
from __future__ import annotations

import base64
from copy import deepcopy
from collections import deque
from dataclasses import replace
import threading
import time
import json
from pathlib import Path

from .camera import FfmpegCameraSnapshot, VisionFrameResult, VisionStatus
from .capture import StereoCameraCapture
from .vision import choose_primary_target
from .multiscale_faces import AdaptiveFaceSearch
from .visual_quality import CameraCapabilityProbe, FocusAssist
from .binocular import BinocularFusion
from .stereo_calibration import StereoRectifier, StereoCalibrationSession, PROJECT, alignment_profile_path
from .stereo_geometry import StereoTargetGeometry
from .bearing import AlignedBearingMatcher,annotate_eye_bearings,ScreenCenteredDirectionFrame


def prepare_stereo_eyes(image, left_rotation=0, right_rotation=0):
    """Orient each eye independently; never rotate the SBS frame as one image."""
    import cv2
    import numpy as np
    if image.shape[1] % 2 or left_rotation not in (0, 180) or right_rotation not in (0, 180):
        raise ValueError("Invalid stereo dimensions/orientation")
    images, quality = {}, {}
    half = image.shape[1] // 2
    for name, frame, rotation in (("left", image[:, :half], left_rotation),
                                   ("right", image[:, half:], right_rotation)):
        frame = cv2.rotate(frame, cv2.ROTATE_180) if rotation else frame
        gray = cv2.cvtColor(cv2.resize(frame, (320, 180)), cv2.COLOR_BGR2GRAY)
        white, black = float(np.mean(gray > 250)), float(np.mean(gray < 5))
        reason = "overexposed" if white > .995 else "dark" if black > .995 else "ok"
        smooth = cv2.GaussianBlur(gray, (3, 3), .6)
        contrast = float(smooth.std())
        detail = float(cv2.Laplacian(smooth, cv2.CV_32F).var())
        if reason == "ok" and contrast < 2.5 and detail < 2:
            reason = "low_texture"
        quality[name] = {"valid": reason == "ok", "reason": reason, "rotation": rotation,
                         "mean_luma": round(float(gray.mean()), 2), "white_fraction": white,
                         "black_fraction": black, "contrast": round(contrast, 2),
                         "sharpness_proxy": round(detail, 2),
                         "quality_score": round(min(1., contrast / 30) * .5 + min(1., detail / 120) * .5, 3),
                         "quality_note": "Content-dependent diagnostics, not a definitive occlusion classifier"}
        images[name] = frame
    return images, quality


class EyeTrackState:
    """Only temporal state/model instances; the SCRFD GPU session is shared."""
    def __init__(self):
        self.values = dict(_previous_gray=None, _previous_mouth_open={}, _tracked_targets=[],
                           _next_tracked_face_id=1, _latest_body_target=None,
                           _face_mesh=None, _pose=None, _face_landmarker=None, _pose_landmarker=None)
        self.search = AdaptiveFaceSearch(interval=.36)
        self.times = deque(maxlen=90)
        self.processed = 0
        self.sequence = 0
        self.detector = "none"
        self.last_body_pass = -1e9

    def invalidate(self):
        self.values.update(_previous_gray=None, _previous_mouth_open={}, _tracked_targets=[], _latest_body_target=None)
        self.search = AdaptiveFaceSearch(interval=.36)


class StereoVisionTracker(FfmpegCameraSnapshot):
    def __init__(self, *, input_fps=60, usb_vid="1BCF", usb_pid="0B15", tracking_eye="auto",
                 left_rotation=0, right_rotation=0, multi_scale_search=True,
                 focus_assist=True, preview_jpeg_quality=95,
                 stereo_calibration_path="calibration/stereo_camera.json",stereo_geometry_enabled=True,
                 audio_visual_guided=False,visual_priority=False,audio_sector_half_width_deg=15.,**kwargs):
        if tracking_eye not in ("auto", "left", "right"):
            raise ValueError("Invalid tracking eye")
        self.tracking_eye = tracking_eye
        self.audio_visual_guided=audio_visual_guided
        self.visual_priority=bool(visual_priority)
        self.audio_sector_half_width_deg=float(audio_sector_half_width_deg)
        self._guidance=None
        self._reset_fusion_pending=False
        self.left_rotation, self.right_rotation = left_rotation, right_rotation
        self._active_eye = None
        self._eye_health = {}
        self._image_valid = False
        self.multi_scale_search = bool(multi_scale_search)
        self.focus_assist_enabled = bool(focus_assist)
        self.preview_jpeg_quality = max(70, min(100, int(preview_jpeg_quality)))
        self.face_search = AdaptiveFaceSearch()
        self.focus_assist = FocusAssist()
        self.capabilities = CameraCapabilityProbe()
        super().__init__(**kwargs)
        width, height = map(int, (self.video_size or "3840x1080").lower().split("x"))
        if width % 2:
            raise ValueError("Stereo width must be even")
        self.capture = StereoCameraCapture(device_name=self.device_name or "USB Video Device", usb_vid=usb_vid,
                                           usb_pid=usb_pid, width=width, height=height, fps=input_fps, ffmpeg=self.ffmpeg)
        self._frame_width = self.output_width
        self._frame_height = round(height * self.output_width / (width // 2))
        self.processing_fps = max(1, min(240, kwargs.get("fps") or input_fps))
        self._recent_frames = deque(maxlen=max(90, round(3.5 * self.processing_fps)))
        self._processed_frames = 0
        self._skipped_for_inference = 0
        self._inference_stop = threading.Event()
        self._inference_worker = None
        self._inference_times = deque(maxlen=120)
        self._detection_sequence = 0
        self._detection_monotonic = 0.0
        self._detection_captured_monotonic = 0.0
        self._preview_b64 = ""
        self._stereo_b64 = ""
        self._stereo_b64_sequence = 0
        self._eye_states = {eye: EyeTrackState() for eye in ("left", "right")}
        self.fusion = self._make_fusion()
        self._eye_frames = {}
        self._eye_targets = {}
        self._binocular = {}
        self._eye_metrics = {}
        self._control_ready = False
        self._aux_eye = "left"
        self._processing_eye_state = None
        calibration_path=Path(stereo_calibration_path)
        self._calibration_path=calibration_path if calibration_path.is_absolute() else PROJECT/calibration_path
        self.stereo_geometry_enabled=bool(stereo_geometry_enabled)
        self.calibration=StereoCalibrationSession(self.capture,(left_rotation,right_rotation),profile_path=self._calibration_path)
        self.rectifier=None
        self.geometry=StereoTargetGeometry()
        self.bearing_matcher=AlignedBearingMatcher()
        self._rectification_status={"status":"not_calibrated","calibrated":False,"message":"请先采集标定板并通过质量检查"}
        self._reload_rectification=True
        self._native_eye_size=(width//2,height)
        self._published_size=(self._frame_width,self._frame_height)

    def request_calibration_reload(self):
        self._reload_rectification=True

    def set_audio_guidance(self,estimate):
        with self._lock:self._guidance=estimate

    def _make_fusion(self):
        return BinocularFusion(self.tracking_eye,guided=self.audio_visual_guided,visual_priority=self.visual_priority,
                               sector_half_width_deg=self.audio_sector_half_width_deg)

    def _direction_frame(self):
        if self.rectifier is None:return None
        return ScreenCenteredDirectionFrame(self.rectifier) if self.visual_priority else self.rectifier

    def set_guided_mode(self,enabled,*,visual_priority=False,sector_half_width_deg=15.):
        with self._lock:
            options=(bool(enabled),bool(visual_priority),float(sector_half_width_deg))
            if (self.audio_visual_guided,self.visual_priority,self.audio_sector_half_width_deg)!=options:
                self.audio_visual_guided,self.visual_priority,self.audio_sector_half_width_deg=options
                self._reset_fusion_pending=True

    def _load_rectification(self):
        self._reload_rectification=False
        try:
            path=self._calibration_path if self._calibration_path.exists() else alignment_profile_path(self._calibration_path)
            if not self.stereo_geometry_enabled or not path.exists():
                self.rectifier=None
                self._rectification_status={"status":"not_calibrated","calibrated":False,"message":"尚未加载实物标定，保留原始双目二维识别"}
                return
            profile=json.loads(path.read_text(encoding="utf-8"))
            identity=profile.get("device",{})
            if identity and (identity.get("usb_vid","").upper(),identity.get("usb_pid","").upper())!=(self.capture.usb_vid.upper(),self.capture.usb_pid.upper()):
                raise ValueError("标定设备与当前 USB 摄像头不匹配")
            self.rectifier=StereoRectifier(profile,expected_size=self._native_eye_size,
                                           rotations=(self.left_rotation,self.right_rotation),allow_alignment_only=True)
            self._rectification_status=self.rectifier.metrics()
        except Exception as exc:
            self.rectifier=None
            self._rectification_status={"status":"invalid","calibrated":False,"message":str(exc)}
        finally:
            for state in self._eye_states.values(): state.invalidate()
            self.fusion=self._make_fusion()
            self.geometry.metrics={"status":"not_calibrated","valid_pairs":0}
            with self._lock: self._recent_frames.clear()

    def _mediapipe_body_target(self, image, gray):
        # Body fallback is useful when a face disappears, but running a second
        # pose model on every face-detection frame doubles unnecessary work.
        state = self._processing_eye_state
        now = time.perf_counter()
        if state is not None:
            if now - state.last_body_pass < .35:
                return None
            state.last_body_pass = now
        return super()._mediapipe_body_target(image, gray)

    def _process_eye(self, image, eye, *, now, allow_extra=True):
        import cv2
        state = self._eye_states[eye]
        self._processing_eye_state = state
        saved = {key: getattr(self, key) for key in state.values}
        for key, value in state.values.items():
            setattr(self, key, value)
        try:
            previous_gray = self._previous_gray
            targets = self._detect_image_targets(image)
            if (allow_extra and self.multi_scale_search and self.face_detector_backend == "scrfd"
                    and self._scrfd_detector is not None):
                def extra_pass(crop):
                    old_gray = self._previous_gray
                    try:
                        self._previous_gray = None
                        return self._scrfd_targets(crop, cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY))
                    finally:
                        self._previous_gray = old_gray
                targets = state.search.search(image, targets, extra_pass, now=now)
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                corrected = []
                for target in targets:
                    if target.tracking_source.endswith("_native_roi"):
                        mouth = self._mouth_motion_from_roi(gray, previous_gray, int(target.x1), int(target.y1),
                                                          int(target.x2-target.x1), int(target.y2-target.y1))
                        target = replace(target, mouth_motion_score=mouth, mouth_audio_sync_score=mouth,
                                         active_speaker_score=min(1., .15*target.frontal_score+.65*mouth+.12*target.score))
                    corrected.append(target)
                targets = corrected
            targets = self._stabilize_targets(targets, unique_new_ids=True)
            state.processed += 1
            state.times.append(time.perf_counter())
            state.detector = self._latest_detector
            return [replace(t, source_eye=eye) for t in targets]
        finally:
            self._processing_eye_state = None
            for key in state.values:
                state.values[key] = getattr(self, key)
                setattr(self, key, saved[key])

    def _start(self):
        self.capture.start()
        if self.capture.actual_device is not None:
            self.capabilities.start()
        with self._lock:
            if self._inference_worker is None or not self._inference_worker.is_alive():
                self._inference_stop.clear()
                self._inference_worker = threading.Thread(target=self._inference_loop, daemon=True)
                self._inference_worker.start()

    def _inference_loop(self):
        import cv2
        import numpy as np
        previous = None
        next_inference = 0.0
        while not self._inference_stop.is_set():
            sample = self.capture.latest()
            if sample is None or sample is previous or time.perf_counter() < next_inference:
                self._inference_stop.wait(0.004)
                continue
            previous = sample
            next_inference = time.perf_counter() + 1.0 / self.processing_fps
            try:
                if self._reset_fusion_pending:
                    old_next=self.fusion.next_id
                    self.fusion=self._make_fusion()
                    self.fusion.next_id=old_next;self._reset_fusion_pending=False
                if self._reload_rectification:
                    self._load_rectification()
                image = cv2.imdecode(np.frombuffer(sample.jpeg, np.uint8), cv2.IMREAD_COLOR)
                if image is None or image.shape[:2] != (sample.height, sample.width):
                    raise ValueError("Stereo JPEG decode failed")
                eyes, quality = prepare_stereo_eyes(image, self.left_rotation, self.right_rotation)
                raw_eyes=eyes
                if self.rectifier is not None:
                    eyes=self.rectifier.apply(eyes)
                frame_width=min(self.output_width,eyes["left"].shape[1]) if self.rectifier else self.output_width
                frame_height=round(eyes["left"].shape[0]*frame_width/eyes["left"].shape[1])
                self._frame_width,self._frame_height=frame_width,frame_height
                started = time.perf_counter()
                detector_images, per_eye_targets, eye_errors = {}, {}, {}
                # Both detectors consume this exact USB sample. Only auxiliary
                # tiles alternate; full-view detection always runs for both eyes.
                for eye in ("left", "right"):
                    pixels = eyes[eye]
                    if pixels.shape[1] != self._frame_width:
                        pixels = cv2.resize(pixels, (self._frame_width, self._frame_height))
                    detector_images[eye] = pixels
                    if not quality[eye]["valid"]:
                        self._eye_states[eye].invalidate()
                        per_eye_targets[eye] = []
                        continue
                    try:
                        per_eye_targets[eye] = self._process_eye(pixels, eye, now=started, allow_extra=eye == self._aux_eye)
                        self._eye_states[eye].sequence = sample.sequence
                    except Exception as exc:
                        self._eye_states[eye].invalidate()
                        per_eye_targets[eye] = []
                        quality[eye].update(valid=False, reason="detector_error")
                        eye_errors[eye] = str(exc)
                self._aux_eye = "right" if self._aux_eye == "left" else "left"
                matches=self.geometry.match(detector_images,per_eye_targets,self.rectifier) if self.rectifier else []
                if self.rectifier:
                    direction_frame=self._direction_frame()
                    per_eye_targets=annotate_eye_bearings(per_eye_targets,direction_frame,(self._frame_width,self._frame_height))
                    aligned=self.bearing_matcher.match(detector_images,per_eye_targets,direction_frame)
                    metric_keys={(m['left_track'],m['right_track']) for m in matches}
                    for m in matches:
                        pair=next((b for b in aligned if (b['left_track'],b['right_track'])==(m['left_track'],m['right_track'])),None)
                        if pair:
                            m['bearing']=pair['bearing']
                            m['fixation']=pair.get('fixation')
                    occupied_l={k[0] for k in metric_keys};occupied_r={k[1] for k in metric_keys}
                    matches+= [b for b in aligned if b['left_track'] not in occupied_l and b['right_track'] not in occupied_r]
                with self._lock:guidance=self._guidance
                result=self.fusion.update(detector_images,per_eye_targets,quality,now=time.perf_counter(),geometric_matches=matches,
                    audio=guidance,frame_time=sample.captured_monotonic)
                if self.rectifier:
                    has_position=result["target"] is not None and result["target"].stereo_position is not None
                    metric_ready=self.rectifier.metric_ready
                    has_bearing=result['target'] is not None and result['target'].bearing is not None
                    result["summary"].update(calibrated=metric_ready,depth_enabled=metric_ready,rectification_enabled=True,
                        coordinate_frame="rectified_camera_midpoint" if has_position else result['target'].bearing['frame'] if has_bearing else "selected_rectified_eye_pixels",
                        uses_two_eye_geometry=has_position,
                        note="Validated rectification; sparse metric estimates require correspondences; known-distance accuracy not yet verified" if metric_ready else "Image alignment preview only; metric validation failed and distance output is disabled")
                selected = result["eye"]
                target = result["target"]
                targets = result["targets"].get(selected, [])
                if self.focus_assist_enabled:
                    focus_targets = {}
                    for eye in ("left", "right"):
                        face = choose_primary_target(result["targets"][eye])
                        if face is not None and self.rectifier:
                            x,y=self.rectifier.raw_center(eye,face.center,(self._frame_width,self._frame_height))
                            face=replace(face,x1=x-1,x2=x+1,y1=y-1,y2=y+1)
                        elif face is not None:
                            ratio = eyes[eye].shape[1] / self._frame_width
                            face = replace(face, x1=face.x1*ratio, y1=face.y1*ratio, x2=face.x2*ratio, y2=face.y2*ratio)
                        focus_targets[eye] = face
                    self.focus_assist.update(raw_eyes, targets=focus_targets)
                eye_frames = {}
                for eye, pixels in detector_images.items():
                    preview = cv2.resize(pixels, (min(960, self._frame_width), round(self._frame_height * min(960, self._frame_width) / self._frame_width)))
                    ok, encoded = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 90])
                    if ok:
                        eye_frames[eye] = base64.b64encode(encoded).decode("ascii")
                jpeg = b""
                if selected is not None:
                    ok, encoded = cv2.imencode(".jpg", detector_images[selected], [cv2.IMWRITE_JPEG_QUALITY, self.preview_jpeg_quality])
                    if ok:
                        jpeg = encoded.tobytes()
                now = time.perf_counter()
                eye_metrics = {}
                for eye, state in self._eye_states.items():
                    stamps = list(state.times)
                    rate = (len(stamps)-1)/(stamps[-1]-stamps[0]) if len(stamps)>1 and stamps[-1]>stamps[0] else 0.
                    eye_metrics[eye] = {"inference_fps": round(rate, 3) if quality[eye]["valid"] else 0.,
                                        "processed_frames": state.processed, "detection_sequence": state.sequence,
                                        "observed_faces": len(result["targets"][eye]), "detector": state.detector,
                                        "extra_passes": state.search.calls, "last_region": state.search.last_region,
                                        "error": eye_errors.get(eye)}
                result["summary"].update(sample_sequence=sample.sequence, sample_captured_monotonic=sample.captured_monotonic,
                                         processing_ms=round((now-started)*1000, 2))
                with self._lock:
                    if result["eye_changed"]:
                        # TalkNet clips must never contain alternate camera views.
                        self._recent_frames.clear()
                    self._active_eye = selected
                    self._published_size=(self._frame_width,self._frame_height)
                    self._eye_health = quality
                    self._image_valid = selected is not None
                    self._eye_metrics = eye_metrics
                    self._eye_frames = eye_frames
                    self._eye_targets = result["targets"]
                    self._binocular = result["summary"]
                    self._control_ready = result["control_ready"]
                    self._latest_jpeg = jpeg
                    self._preview_b64 = base64.b64encode(jpeg).decode("ascii")
                    self._latest_targets = targets
                    self._latest_target = target
                    self._latest_body_target = self._eye_states[selected].values["_latest_body_target"] if selected else None
                    self._skipped_for_inference += max(0, sample.sequence - self._detection_sequence - 1) if sample.sequence >= self._detection_sequence else 0
                    self._processed_frames += 1
                    self._detection_sequence = sample.sequence
                    self._detection_monotonic = now
                    self._detection_captured_monotonic = sample.captured_monotonic
                    self._latest_frame_time = sample.captured_unix
                    self._inference_times.append(now)
                    if jpeg:
                        self._recent_frames.append((sample.captured_unix, jpeg, self._frame_width, self._frame_height))
                    self._latest_error = "binocular streaming" if selected else "no usable camera eye"
            except Exception as exc:
                self._latest_error = f"inference: {exc}"
                self._inference_stop.wait(0.05)

    def capture_metrics(self):
        metrics = self.capture.metrics()
        with self._lock:
            stamps = list(self._inference_times)
            rate = (len(stamps) - 1) / (stamps[-1] - stamps[0]) if len(stamps) > 1 and stamps[-1] > stamps[0] else 0
            metrics.update(inference_fps=round(rate, 3), detection_sequence=self._detection_sequence,
                           detection_age_s=round(time.perf_counter() - self._detection_captured_monotonic, 4) if self._detection_captured_monotonic else None,
                           detector_input_width=self._published_size[0], detector_input_height=self._published_size[1],
                           processed_frames=self._processed_frames, skipped_for_inference=self._skipped_for_inference,
                           detector_eye=self._active_eye, tracking_eye=self.tracking_eye,
                           eye_health=dict(self._eye_health), left_rotation=self.left_rotation,
                           right_rotation=self.right_rotation, stereo_depth_enabled=bool(self.rectifier and self.rectifier.metric_ready),
                           rectification=deepcopy(self._rectification_status),
                           spatial_geometry=deepcopy(self.geometry.metrics),
                           bearing_geometry=deepcopy(self.bearing_matcher.metrics),
                           direction_projection=self._direction_frame().direction_projection(self._published_size) if self.rectifier else {},
                           direction_view=self._direction_frame().direction_view(self._published_size) if self.rectifier and self.visual_priority else {},
                           dual_eye_detection=True, per_eye=deepcopy(self._eye_metrics),
                           binocular=deepcopy(self._binocular),
                           detector_input_source="decoded pixels before preview recompression",
                           preview_jpeg_quality=self.preview_jpeg_quality,
                           multi_scale_search={"enabled": self.multi_scale_search and self.face_detector_backend == "scrfd",
                                               "extra_passes": sum(m["extra_passes"] for m in self._eye_metrics.values()),
                                               "policy": "both full views; one auxiliary eye per pair, >=0.36s per eye"},
                           focus_assist=dict(self.focus_assist.metrics) if self.focus_assist_enabled else {},
                           focus_capability=self.capabilities.snapshot())
        return metrics

    def startup_health_check(self,timeout_s=1.2):
        """Inspect three fresh USB samples while automatic motion is paused."""
        import cv2
        import numpy as np
        from .stereo_health import check_pair
        rows=[];seen=set();deadline=time.perf_counter()+timeout_s
        while time.perf_counter()<deadline and len(rows)<3:
            sample=self.capture.latest()
            if sample is None or sample.sequence in seen or time.perf_counter()-sample.captured_monotonic>.6:
                time.sleep(.03);continue
            seen.add(sample.sequence)
            image=cv2.imdecode(np.frombuffer(sample.jpeg,np.uint8),cv2.IMREAD_COLOR)
            if image is None:continue
            eyes,_=prepare_stereo_eyes(image,self.left_rotation,self.right_rotation)
            rectifier=self.rectifier
            if rectifier is not None:eyes=rectifier.apply(eyes)
            result=check_pair(eyes['left'],eyes['right'],alignment_loaded=rectifier is not None)
            result['usb_sample_sequence']=sample.sequence;rows.append(result)
            time.sleep(.04)
        statuses=[r['status'] for r in rows]
        status=('misaligned' if statuses.count('misaligned')>=2 else
                'degraded' if statuses.count('degraded')>=2 else
                'consistent' if statuses.count('consistent')>=2 else 'unverified')
        return dict(status=status,samples=rows,source='single_SBS_USB_frame',
            formal_recalibration_performed=False,calibration_changed=False,
            message='双目校正可能已偏移，请检查镜头固定并重新标定' if status=='misaligned' else
                '存在可见垂直残差；可继续二维跟踪，建议检查或更新对齐参数' if status=='degraded' else
                '共同特征垂直对齐正常；左右水平视差保留，不代表正式测距或曝光同步验收' if status=='consistent' else
                '共同纹理或新鲜画面不足，暂不能确认双目校正；未改写参数')

    def read(self):
        self._start()
        sample = self.capture.latest()
        metrics = self.capture_metrics()
        with self._lock:
            if sample is not None and sample.sequence != self._stereo_b64_sequence:
                self._stereo_b64 = base64.b64encode(sample.jpeg).decode("ascii")
                self._stereo_b64_sequence = sample.sequence
            data_fresh = bool(metrics["ok"] and self._detection_captured_monotonic
                              and time.perf_counter() - self._detection_captured_monotonic < .75)
            fresh = data_fresh and self._image_valid
            binocular = deepcopy(self._binocular)
            if not data_fresh:
                binocular.update(control_ready=False, selection_reason="stale_frame", people=[], associations=[])
                binocular['target_selection']={**binocular.get('target_selection',{}),'audio_usable':False,'audio_reason':'stale_frame'}
            return VisionFrameResult(
                target=self._latest_target if fresh else None,
                frame_width=self._published_size[0], frame_height=self._published_size[1], jpeg_base64=self._preview_b64 if data_fresh else '',
                status=VisionStatus(fresh, self.capture.device_name, self._latest_error, fps=metrics["measured_fps"],
                                    detector=self._latest_detector, inference_fps=metrics["inference_fps"]),
                targets=list(self._latest_targets) if fresh else [],
                body_target=self._latest_body_target if fresh else None,
                stereo_jpeg_base64=self._stereo_b64 if sample is not None and time.perf_counter()-sample.captured_monotonic < 2 else '',
                capture_sequence=sample.sequence if sample else 0,
                captured_monotonic=sample.captured_monotonic if sample else 0,
                detection_sequence=self._detection_sequence,
                focus_previews=dict(self.focus_assist.previews) if self.focus_assist_enabled and data_fresh else {},
                eye_frames=dict(self._eye_frames) if data_fresh else {},
                eye_targets={eye: list(targets) if data_fresh else [] for eye, targets in self._eye_targets.items()},
                binocular=binocular, control_eye=self._active_eye or "",
                control_ready=fresh and self._control_ready)

    def stop(self):
        self._inference_stop.set()
        self.calibration.stop()
        if getattr(self, "capture", None):
            self.capture.stop()
        worker = getattr(self, "_inference_worker", None)
        if worker and worker is not threading.current_thread():
            worker.join(timeout=10)
        if not worker or not worker.is_alive():
            for state in self._eye_states.values():
                for name in ("_face_mesh", "_pose", "_face_landmarker", "_pose_landmarker"):
                    detector = state.values[name]
                    if detector is not None and callable(getattr(detector, "close", None)):
                        detector.close()
            self._close_mediapipe_detectors()
        with self._lock:
            self._latest_error = "stopped"
            self._latest_targets = []
            self._latest_target = None
            self._preview_b64 = self._stereo_b64 = ""
            self._stereo_b64_sequence = 0
            self._detection_sequence = 0
            self._detection_monotonic = 0
            self._detection_captured_monotonic = 0
            self._inference_times.clear()
            self._processed_frames = self._skipped_for_inference = 0
            self._eye_health = {}
            self._image_valid = False
            self._active_eye = None
            self.focus_assist = FocusAssist()
            self.face_search = AdaptiveFaceSearch()
            self._eye_states = {eye: EyeTrackState() for eye in ("left", "right")}
            self.fusion = self._make_fusion()
            self._eye_frames, self._eye_targets, self._binocular, self._eye_metrics = {}, {}, {}, {}
            self._control_ready = False
