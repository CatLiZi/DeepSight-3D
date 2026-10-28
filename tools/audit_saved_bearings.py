"""Explore saved capture frames with the current two-eye pipeline; no hardware."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time
import cv2
import numpy as np

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'src'))
from locator_demo.stereo_vision import StereoVisionTracker
from locator_demo.settings import load_settings


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--count',type=int,default=18)
    args=parser.parse_args()
    if not args.directory.is_dir():parser.error('Archive directory does not exist')
    paths=sorted(args.directory.glob('*.jpg'))
    if not paths:parser.error('Archive directory has no JPEG frames')
    args.output.mkdir(parents=True,exist_ok=False)
    paths=[paths[i] for i in np.unique(np.linspace(0,len(paths)-1,min(args.count,len(paths)),dtype=int))]
    config=load_settings(PROJECT/'locator_demo_settings.json').camera
    tracker=StereoVisionTracker(video_size=config.video_size,output_width=1920,
        left_rotation=config.left_rotation,right_rotation=config.right_rotation,
        face_detector_backend=config.face_detector_backend,scrfd_model_path=config.scrfd_model_path,
        scrfd_threshold=config.scrfd_threshold,multi_scale_search=config.multi_scale_search,
        focus_assist=False,audio_visual_guided=True)
    tracker.capture.start=lambda:None;tracker.capabilities.start=lambda:None
    original=tracker.capture.metrics;tracker.capture.metrics=lambda:{**original(),'ok':True}
    rows=[]
    try:
        for path in paths:
            tracker.capture._accept(path.read_bytes());seq=tracker.capture.latest().sequence
            tracker._start();deadline=time.perf_counter()+30
            while tracker.capture_metrics()['detection_sequence']!=seq and time.perf_counter()<deadline:time.sleep(.01)
            if tracker.capture_metrics()['detection_sequence']!=seq:raise RuntimeError(tracker._latest_error)
            frame=tracker.read();metrics=tracker.capture_metrics()
            row={'source':str(path.resolve()),'faces':{eye:len(ts) for eye,ts in frame.eye_targets.items()},
                 'matches':metrics.get('bearing_geometry'),'binocular':frame.binocular}
            rows.append(row)
        result={'scope':'Exploratory evenly spaced archived-frame audit, not an independent accuracy benchmark',
                'hardware_io':False,'servo_io':False,'rows':rows,
                'frames':len(rows),'frames_with_feature_match':sum(r['matches'].get('valid_pairs',0)>0 for r in rows)}
        (args.output/'validation.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in result.items() if k!='rows'}))
    finally:tracker.stop()


if __name__=='__main__':main()
