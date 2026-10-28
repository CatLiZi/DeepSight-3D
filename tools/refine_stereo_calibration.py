"""Compare saved calibration fits with fixed, time-separated validation groups.

Writes diagnostics only. Does not open devices or activate either candidate.
"""
import argparse,hashlib,json,sys,time
from pathlib import Path

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'src'))
from locator_demo.stereo_calibration import solve_calibration


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--observations',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path);args=parser.parse_args()
    data=json.loads(args.observations.read_text(encoding='utf-8'));args.output.mkdir(parents=True,exist_ok=True)
    records={}
    for name,robust in (('grouped-standard',False),('grouped-robust',True)):
        start=time.perf_counter()
        fit=solve_calibration(data['samples'],tuple(data['image_size']),board_spec=data['board'],
            rotations=data['rotations'],diagnostic_only=True,robust_refinement=robust,device={'usb_vid':'1BCF','usb_pid':'0B15'})
        fit.update(paper_flatness_verified=False,motion_stability_required=data.get('motion_stability_required',False),
                   runtime_activation_requested=False,source_observations_sha256=hashlib.sha256(args.observations.read_bytes()).hexdigest())
        (args.output/(name+'.json')).write_text(json.dumps(fit,ensure_ascii=False,indent=2),encoding='utf-8')
        q=fit['quality'];records[name]={key:q[key] for key in ('status','left_rms_px','right_rms_px','stereo_rms_px',
            'holdout_epipolar_p95_px','holdout_reprojection_p95_px','training_pairs','holdout_pairs','validation_split','optimization')}
        records[name]['duration_s']=time.perf_counter()-start
        print(json.dumps({'finished':name,**{k:v for k,v in records[name].items() if k not in ('validation_split','optimization')}}),flush=True)
    assert records['grouped-standard']['validation_split']==records['grouped-robust']['validation_split']
    summary={'input':str(args.observations.resolve()),'poses':len(data['samples']),'comparisons':records,
        'same_train_and_validation_sets':True,'all_validation_corners_retained':True,'runtime_profile_changed':False,
        'independent_new_capture_test':False,'note':'Historical captures are development replay, not a new physical accuracy acceptance test.'}
    (args.output/'comparison.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
