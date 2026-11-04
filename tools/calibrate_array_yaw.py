"""Fit measured yaw angles without moving hardware or changing control gains.

Input JSON: [[actual_feedback_ticks, measured_left_positive_degrees], ...].
Use at least three stationary measurements, including zero and both sides.
"""
import argparse,json,sys,os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from locator_demo.array_pose import fit_yaw_calibration
from locator_demo.settings import AudioCaptureConfig


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('measurements',type=Path);p.add_argument('--output',required=True,type=Path)
    p.add_argument('--apply-config',type=Path)
    args=p.parse_args();points=json.loads(args.measurements.read_text(encoding='utf-8-sig'))
    if args.output.exists() or args.output.resolve()==args.measurements.resolve():
        raise ValueError('Use a new output file; never overwrite measurements')
    if args.apply_config and args.output.resolve()==args.apply_config.resolve():
        raise ValueError('Calibration report and application config must be different files')
    result=fit_yaw_calibration(points);result['measurements']=points
    if args.apply_config:
        config=json.loads(args.apply_config.read_text(encoding='utf-8-sig'))
        if result['measured_left_sign']!=config['direction']['yaw_left_sign']:
            raise ValueError('Measured direction conflicts with current configuration; do not silently reverse motors')
        limits=config['axis_limits']
        if any(not limits['yaw_min']<=row[0]<=limits['yaw_max'] for row in points):
            raise ValueError('Measurements must stay inside current software travel')
        capture=dict(config['audio_capture'])
        for name in ('body_yaw_units_per_degree','body_yaw_zero_position'):capture[name]=result[name]
        AudioCaptureConfig(**capture)
        config['audio_capture']=capture
        backup=args.apply_config.with_suffix(args.apply_config.suffix+'.before-array-yaw')
        if backup.exists():raise FileExistsError('Preserve existing backup before applying a second calibration')
        backup.write_bytes(args.apply_config.read_bytes())
        temp=args.apply_config.with_suffix(args.apply_config.suffix+'.array-yaw-tmp')
        temp.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
        os.replace(temp,args.apply_config)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
