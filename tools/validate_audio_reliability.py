"""Replay saved raw PCM using actual streaming VAD, without hardware or motion.

Positions label reporting intervals only; no labels enter either estimator.
The recorded operator positions have no measured angles: reported region
agreement is a development check, not physical angular accuracy.
"""
import argparse
from collections import Counter
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
import wave

import numpy as np
from scipy.signal import resample_poly

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parent
sys.path.insert(0, str(PROJECT/'src'))
from locator_demo.audio import AudioDirection
from locator_demo.ear_localization import EarPairConfig, EarPairLocalizer
from locator_demo.speech_vad import StreamingSpeechVAD


def read_wav(path):
    with wave.open(str(path), 'rb') as source:
        assert source.getsampwidth() == 2
        return source.getframerate(), np.frombuffer(source.readframes(source.getnframes()), '<i2').reshape(-1, source.getnchannels())


def summarize(rows, begin, end, region=None):
    subset = [r for r in rows if begin <= r['t'] <= end]
    voice = sum(r['p'] >= .5 for r in subset)
    angles = np.array([r['angle'] for r in subset if r['accepted']])
    result = dict(windows=len(subset), voice_windows=voice, accepted=len(angles),
                  accepted_when_voice=len(angles)/max(1, voice),
                  median_deg=float(np.median(angles)) if len(angles) else None,
                  p10_p90_deg=np.percentile(angles, [10, 90]).tolist() if len(angles) else None,
                  reasons=dict(Counter(r['reason'] for r in subset)))
    if region and len(angles):
        correct = abs(angles) <= 15 if region == 'front' else (angles > 8 if region == 'left' else angles < -8)
        result['qualitative_region_agreement'] = float(np.mean(correct))
        result['opposite_side_fraction'] = float(np.mean(angles < -8 if region == 'left' else angles > 8)) if region != 'front' else None
    return result


def replay(pcm, variants):
    vad = StreamingSpeechVAD()
    assert vad.session is not None, vad.error
    rows = {name: [] for name in variants}
    for end in range(960, len(pcm)+1, 960):
        context = pcm[max(0, end-9216):end]
        probability = vad.update(context[:, [1, 2]], sample_start=end-len(context))
        for name, (localizer, length, old) in variants.items():
            block = pcm[max(0, end-length):end]
            if old:
                estimate = localizer.estimate(block)
                if probability < .45:
                    localizer._unknown('no_voice_activity')
                    estimate = replace(estimate, direction=AudioDirection.UNKNOWN, confidence=0,
                                       doa_confidence=0, noise_state='no_voice_activity')
            else:
                estimate = localizer.estimate(block, speech_probability=probability)
            accepted = (probability >= .5 and estimate.direction != AudioDirection.UNKNOWN
                        and estimate.confidence >= .2 and estimate.doa_confidence >= .05
                        and estimate.temporal_stability >= .5 and estimate.azimuth_std_deg <= 25)
            rows[name].append(dict(t=end/48000, p=probability, accepted=bool(accepted),
                angle=estimate.azimuth_deg if accepted else None, reason=estimate.noise_state,
                correlation=localizer.diagnostics.get('normalized_correlation'),
                coherence=localizer.diagnostics.get('spectral_coherence')))
    return rows, vad.metrics()


def variants(baseline):
    old = EarPairConfig('ear_pair', 2, 3, True, .13176241517)
    return dict(r16_96ms=(baseline.EarPairLocalizer(baseline.EarPairConfig('ear_pair',2,3,True,old.spacing_m)),4608,True),
                r16_192ms=(baseline.EarPairLocalizer(baseline.EarPairConfig('ear_pair',2,3,True,old.spacing_m)),9216,True),
                weighted_old_spacing=(EarPairLocalizer(old),9216,False),
                weighted_1355mm=(EarPairLocalizer(replace(old,spacing_m=.1355)),9216,False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(); args.output.mkdir(parents=True, exist_ok=True)
    baseline_path = args.output/'ear_localization-r16.py'
    assert baseline_path.is_file(), 'Preserve the R16 baseline before changing production sources'
    spec = importlib.util.spec_from_file_location('locator_demo._r16_baseline', baseline_path)
    baseline = importlib.util.module_from_spec(spec); sys.modules[spec.name]=baseline; spec.loader.exec_module(baseline)
    result = dict(scope=__doc__, hardware_io=False, servo_io=False, network_training=False,
                  spacing_m=.1355, spacing_source='operator approximate 13.5–13.6 cm; not precision metrology',
                  delay_offset_us=0, baseline_sha256=hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
                  recordings={}, synthetic={})
    recorded = ROOT/'hardware_inventory/microphone-replug-20261026'
    cases = dict(directional=(recorded/'directional/operator-full-4ch.wav',
                              dict(right=(131.5,136,'right'), front=(139,144,'front'), left=(145.5,149,'left'), quiet=(5,10,None))),
                 operator=(recorded/'operator/operator-full-4ch.wav',
                           dict(front=(171.5,176.6,'front'), rubbing=(154,165,None), quiet=(5,10,None))))
    for name,(path, intervals) in cases.items():
        rate,pcm=read_wav(path); assert rate==48000 and pcm.shape[1]==4
        start=time.perf_counter(); rows,vad=replay(pcm, variants(baseline))
        (args.output/f'{name}-replay.json').write_text(json.dumps(rows,separators=(',',':')),encoding='utf-8')
        summaries={version:{label:summarize(trace,*interval) for label,interval in intervals.items()} for version,trace in rows.items()}
        result['recordings'][name]=dict(path=str(path.relative_to(ROOT)).replace('\\','/'),sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            seconds=len(pcm)/48000, processing_s=time.perf_counter()-start, vad=vad, cases=summaries)
        print(json.dumps(dict(recording=name,cases=summaries)),flush=True)
    reference=ROOT/'hardware_inventory/av-guided-upgrade-20261025/silero-upstream-test.wav'
    rate,speech=read_wav(reference);assert rate==16000
    signal=resample_poly(speech[:16000*15,0].astype(float),3,1)
    signal*=.3*32767/max(1.,np.max(np.abs(signal)))
    rng=np.random.default_rng(8812)
    for delay in (-9,0,9):
        right=np.interp(np.arange(len(signal))-delay,np.arange(len(signal)),signal,left=0,right=0)
        pair=np.stack((signal,right),axis=1)+rng.normal(0,9,(len(signal),2))
        pcm=np.zeros((len(signal),4),'<i2');pcm[:,[1,2]]=np.clip(pair,-32768,32767)
        rows,_=replay(pcm,{'new':variants(baseline)['weighted_1355mm']})
        angles=np.array([r['angle'] for r in rows['new'] if r['accepted']])
        expected=float(np.degrees(np.arcsin(delay/48000/(.1355/343))))
        result['synthetic'][f'delay_{delay}']=dict(**summarize(rows['new'],0,len(pcm)/48000),expected_angle_deg=expected,
            p95_error_deg=float(np.percentile(abs(angles-expected),95)) if len(angles) else None)
    n=48000*5;t=np.arange(n)/48000
    for name,pair in dict(silence=np.zeros((n,2)), independent_noise=rng.normal(0,550,(n,2)),
                          tone=np.stack((1500*np.sin(2*np.pi*440*t),1500*np.sin(2*np.pi*440*(t-.0001))),axis=1)).items():
        pcm=np.zeros((n,4),'<i2');pcm[:,[1,2]]=pair.astype('<i2')
        rows,_=replay(pcm,{'new':variants(baseline)['weighted_1355mm']})
        result['synthetic'][name]=summarize(rows['new'],0,5)
    current=result['recordings']['directional']['cases']['weighted_1355mm']
    old=result['recordings']['directional']['cases']['r16_96ms']
    result['development_targets']=dict(
        strict_region_agreement_75pct_all_three=all(current[k]['accepted']>=20 and current[k]['qualitative_region_agreement']>=.75 for k in ('right','front','left')))
    result['limitations']=['Only approximate physical positions were recorded; no true angles were measured.',
        'Some left-front evidence is close to the centre deadband. This remains unresolved physical directional uncertainty.',
        'The strict 75% side-region development target is reported separately; it is not a calibration certificate.',
        'The same recordings informed development and evaluation; this is not an independent room or speaker acceptance test.']
    result['checks']=dict(
        three_medians_follow_reported_side_order=(current['right']['median_deg']<-8 and abs(current['front']['median_deg'])<=15 and current['left']['median_deg']>8),
        no_opposite_side_in_recorded_side_cases=all(current[k]['opposite_side_fraction']==0 for k in ('right','left')),
        more_useful_evidence_than_r16=all(current[k]['accepted']>old[k]['accepted'] for k in ('right','front','left')),
        synthetic_delay_sign_and_error=all(result['synthetic'][f'delay_{d}']['accepted']>20 and result['synthetic'][f'delay_{d}']['p95_error_deg']<3 for d in (-9,0,9)),
        synthetic_noise_no_directions=all(result['synthetic'][k]['accepted']==0 for k in ('silence','independent_noise','tone')),
        recorded_quiet_no_directions=all(x['cases']['weighted_1355mm']['quiet']['accepted']==0 for x in result['recordings'].values()))
    result['angular_accuracy_verified']=False
    result['source_sha256']={str(p.relative_to(PROJECT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in sorted((PROJECT/'src/locator_demo').rglob('*.py'))}
    (args.output/'replay-validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(checks=result['checks'],synthetic=result['synthetic'])),flush=True)
    return not all(result['checks'].values())


if __name__=='__main__':raise SystemExit(main())
