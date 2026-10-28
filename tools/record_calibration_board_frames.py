"""Archive native board frames during operator-paced calibration; skip empty setup frames."""
import argparse
import base64
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys,time,urllib.request

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'src'))
import cv2
import numpy as np
from locator_demo.stereo_calibration import make_board,orient_pair


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',required=True)
    parser.add_argument('--timeout',type=float,default=600)
    parser.add_argument('--visible-seconds',type=float,default=120)
    args=parser.parse_args()
    folder=Path(args.output).resolve()
    if not folder.is_relative_to(PROJECT.parent/'hardware_inventory'):
        raise ValueError('Output must stay under this workspace hardware_inventory')
    folder.mkdir(parents=True,exist_ok=False);(folder/'frames').mkdir()
    cv2.setNumThreads(2)
    detector=cv2.aruco.CharucoDetector(make_board())
    rows=[];errors=[];seen=set();started=time.monotonic();visible=0.;attempts=0
    print('Recorder waiting for a visible board; empty preparation frames are skipped.',flush=True)
    while time.monotonic()-started<args.timeout and visible<args.visible_seconds and not (folder/'stop').exists():
        tick=time.monotonic();attempts+=1
        try:
            state=json.load(urllib.request.urlopen('http://127.0.0.1:8010/api/state',timeout=8))
            raw=base64.b64decode(state['visual']['stereo_frame']);digest=hashlib.sha256(raw).hexdigest()
            if digest in seen:time.sleep(.2);continue
            seen.add(digest)
            image=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_COLOR)
            ids={};markers={}
            for eye,pixels in orient_pair(image,(180,180)).items():
                _,found,_,marked=detector.detectBoard(pixels)
                ids[eye]=set([] if found is None else map(int,found.ravel()))
                markers[eye]=0 if marked is None else len(marked)
            common=len(ids['left']&ids['right'])
            if common>=4 or min(markers.values())>=2:
                name=f'board-{len(rows)+1:04d}.jpg';(folder/'frames'/name).write_bytes(raw)
                rows.append({'file':name,'sha256':digest,'recorded_at':datetime.now(timezone.utc).isoformat(),
                             'observed_monotonic':time.perf_counter(),'common_corners':common,'markers':markers,
                             'capture_frame_counter':state['visual']['capture']['frames']})
                visible+=.5
                if len(rows)==1:print('First board frame detected; recording board frames now.',flush=True)
        except Exception as exc:errors.append(str(exc))
        (folder/'manifest.json').write_text(json.dumps({'frames':rows,'errors':errors,'attempts':attempts,
            'board_visible_seconds_approx':visible,'empty_preparation_frames_skipped':True,
            'automatic_motion_started':False},indent=2),encoding='utf-8')
        time.sleep(max(0,.5-(time.monotonic()-tick)))
    print(json.dumps({'saved_board_frames':len(rows),'errors':errors,'manual_stop':(folder/'stop').exists()}),flush=True)


if __name__=='__main__':main()
