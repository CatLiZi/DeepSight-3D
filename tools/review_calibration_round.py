"""Review all archived board frames, newest first, then replay motion gating in time order."""
import argparse,json,sys
from pathlib import Path
import cv2
import numpy as np

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'src'))
from locator_demo.stereo_calibration import (detect_board_pair,orient_pair,view_descriptor,
    CalibrationStabilityGate,solve_calibration)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--round',required=True);args=parser.parse_args()
    folder=Path(args.round).resolve()
    if not folder.is_relative_to(PROJECT.parent/'hardware_inventory'):raise ValueError('Unexpected round folder')
    state=json.loads((folder/'completed-round.json').read_text(encoding='utf-8'))
    session=Path(state['session'])
    if not session.is_relative_to(PROJECT/'calibration/sessions'):raise ValueError('Unexpected session folder')
    recorded=json.loads((session/'observations.json').read_text(encoding='utf-8'))
    manifest=json.loads((folder/'near-pass/manifest.json').read_text(encoding='utf-8'))
    cv2.setNumThreads(2);rows=[]
    for n,frame in enumerate(reversed(manifest['frames']),1):
        path=folder/'near-pass/frames'/frame['file']
        row={**frame,'source_path':str(path.relative_to(PROJECT.parent)).replace('\\','/'),'sample':None}
        try:
            image=cv2.imread(str(path));sample=detect_board_pair(orient_pair(image,(180,180)))
            row['descriptor']=view_descriptor(sample,tuple(recorded['image_size'])).tolist()
            row['sample']=sample
        except ValueError as exc:row['rejection']=str(exc)
        rows.append(row)
        if n%50==0:print(json.dumps({'reviewed_newest_first':n,'total':len(manifest['frames'])}),flush=True)
    rows.reverse();gate=CalibrationStabilityGate();stable=[]
    for row in rows:
        if row['sample'] is None:gate.reset();continue
        ready,metrics=gate.observe(row['sample'],row['observed_monotonic'])
        row['motion_ready']=ready;row['stability']=metrics
        if ready:
            sample={**row['sample'],'stability':metrics,'captured_monotonic':row['observed_monotonic']}
            stable.append({'sample':sample,'descriptor':row['descriptor'],'source_path':row['source_path'],'source_kind':'archived_board_frame'})
    candidates=list(stable)
    for n,sample in enumerate(recorded['samples'],1):
        candidates.append({'sample':sample,'descriptor':view_descriptor(sample,tuple(recorded['image_size'])).tolist(),
            'source_path':str((session/f'pair-{n:03d}.jpg').relative_to(PROJECT.parent)).replace('\\','/'),'source_kind':'live_stable_capture'})
    selected=[]
    # Choose by corner count before any model fit, never by calibration residuals.
    for item in sorted(candidates,key=lambda a:len(a['sample']['ids']),reverse=True):
        desc=np.array(item['descriptor']);duplicate=False
        for old in selected:
            delta=desc-np.array(old['descriptor']);delta[3]=(delta[3]+1)%2-1
            if np.linalg.norm(delta)<.065:duplicate=True;break
        if not duplicate:selected.append(item)
    selected.sort(key=lambda a:a['sample']['captured_monotonic'])
    descriptors=np.array([a['descriptor'] for a in selected])
    summary={'live_stable_pairs':len(recorded['samples']),'archived_board_frames':len(rows),
        'all_archived_frames_reviewed':True,'review_order':'newest first; motion replay chronological',
        'archived_frames_with_12_shared_corners':sum(r['sample'] is not None for r in rows),
        'archived_motion_ready_frames':len(stable),'distinct_selected_views':len(selected),
        'selected_common_corner_range':[min(len(a['sample']['ids']) for a in selected),max(len(a['sample']['ids']) for a in selected)],
        'center_span_xy':np.ptp(descriptors[:,:2],axis=0).tolist(),'distance_proxy_span':float(np.ptp(descriptors[:,2])),
        'selection_uses_model_residuals':False,'runtime_profile_changed':False}
    (folder/'all-frame-review.json').write_text(json.dumps({'summary':summary,'frames':rows},ensure_ascii=False,indent=2),encoding='utf-8')
    observations={'board':recorded['board'],'image_size':recorded['image_size'],'rotations':recorded['rotations'],
        'metric_scale_confirmed':False,'samples':[a['sample'] for a in selected],'selected_sources':selected,'selection':summary}
    (folder/'reviewed-observations.json').write_text(json.dumps(observations,indent=2),encoding='utf-8')
    if len(selected)>=20:
        fit=solve_calibration(observations['samples'],tuple(recorded['image_size']),board_spec=recorded['board'],
            rotations=recorded['rotations'],diagnostic_only=True,device={'usb_vid':'1BCF','usb_pid':'0B15'})
        fit.update(selection=summary,paper_flatness_verified=False,runtime_activation_requested=False)
        (folder/'reviewed-diagnostic-fit.json').write_text(json.dumps(fit,indent=2),encoding='utf-8')
        summary['fit_quality']=fit['quality']
    else:summary['fit_not_run']='Fewer than 20 distinct motion-qualified views'
    (folder/'round-summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps(summary),flush=True)


if __name__=='__main__':main()
