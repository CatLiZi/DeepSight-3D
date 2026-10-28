"""Small relative servo checks using feedback and upright camera optical flow.

Never homes the head. Each tested axis returns to its measured initial position.
No torque/mode changes: movement uses the controller's current position mode.
"""
import argparse
import json
from pathlib import Path
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from locator_demo.capture import StereoCameraCapture
from locator_demo.servo_bus import ZPServoBus


def flow(before, after):
    a = cv2.cvtColor(cv2.resize(before, (960, 540)), cv2.COLOR_BGR2GRAY)
    b = cv2.cvtColor(cv2.resize(after, (960, 540)), cv2.COLOR_BGR2GRAY)
    points = cv2.goodFeaturesToTrack(a, 400, .015, 12)
    if points is None:
        return {'valid': False, 'reason': 'no corners'}
    q, status, _ = cv2.calcOpticalFlowPyrLK(a, b, points, None, winSize=(31, 31), maxLevel=3)
    back, good, _ = cv2.calcOpticalFlowPyrLK(b, a, q, None, winSize=(31, 31), maxLevel=3)
    mask = (status[:, 0] > 0) & (good[:, 0] > 0) & (np.linalg.norm(back[:, 0] - points[:, 0], axis=1) < 1.5)
    d = q[mask, 0] - points[mask, 0]
    if len(d) < 15:
        return {'valid': False, 'tracked': len(d)}
    median = np.median(d, axis=0)
    return {'valid': True, 'tracked': len(d), 'median_dx_px': float(median[0]), 'median_dy_px': float(median[1]),
            'mad_px': np.median(np.abs(d - median), axis=0).tolist(), 'image_size': [960, 540]}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--port', default='COM3')
    p.add_argument('--ids', type=int, nargs='+', default=[0, 2])
    p.add_argument('--step', type=int, default=12, choices=range(1, 21))
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    bus = ZPServoBus(args.port)
    camera = StereoCameraCapture()
    report = {'port': args.port, 'step': args.step, 'mode_or_torque_commands': False, 'tests': []}

    def snapshot():
        time.sleep(.15)
        sample = camera.latest()
        if sample is None or time.perf_counter() - sample.captured_monotonic > .3:
            raise RuntimeError('No fresh camera image')
        image = cv2.imdecode(np.frombuffer(sample.jpeg, np.uint8), cv2.IMREAD_COLOR)
        return cv2.rotate(image[:, :1920], cv2.ROTATE_180)

    try:
        camera.start()
        deadline = time.perf_counter() + 10
        while camera.latest() is None and time.perf_counter() < deadline:
            time.sleep(.05)
        time.sleep(1)
        for sid in args.ids:
            initial = bus.read_position(sid)
            if initial is None or not 1230 <= initial <= 1770:
                raise RuntimeError(f'ID {sid}: no usable initial position ({initial})')
            before = snapshot()
            row = {'id': sid, 'initial': initial, 'requested': initial + args.step}
            report['tests'].append(row)
            sent = False
            try:
                bus.move_position_only(sid, initial + args.step, 600)
                sent = True
                time.sleep(1.1)
                row['feedback_after'] = bus.read_position(sid)
                after = snapshot()
                row['flow'] = flow(before, after)
                cv2.imwrite(str(args.output / f'id{sid}-before.jpg'), before)
                cv2.imwrite(str(args.output / f'id{sid}-after.jpg'), after)
            finally:
                if sent:
                    bus.move_position_only(sid, initial, 600)
                    time.sleep(1)
                    row['feedback_returned'] = bus.read_position(sid)
            print(json.dumps(row), flush=True)
    except Exception as exc:
        report['error'] = str(exc)
        raise
    finally:
        camera.stop()
        bus.close()
        (args.output / 'validation.json').write_text(json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
