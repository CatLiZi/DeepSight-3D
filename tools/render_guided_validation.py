"""Build an inspectable decision replay and plots from saved validation evidence."""
from dataclasses import replace
import json
import math
from pathlib import Path
import sys
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from locator_demo.audio import AudioEstimate,AudioDirection
from locator_demo.av_selector import AudioVisualSelector
from locator_demo.vision import FaceTarget


def target(identity,angle,width,score):
    x=320-500*math.tan(math.radians(angle))
    bearing=dict(frame='rig_optical_bisector',azimuth_left_deg=angle,azimuth_interval_deg=[angle-3,angle+3],
                 sigma_deg=6.,confidence=.9,source='binocular_feature_rays',virtual_u_px=x,virtual_v_px=160.)
    return FaceTarget(x-width/2,80,x+width/2,240,face_id=identity,score=score,source_eye='left',
                      tracking_source='scrfd',bearing=bearing)


def main():
    out=ROOT.parent/'hardware_inventory/av-guided-upgrade-20261025'
    big=target(1,-25,160,.95);small=target(2,25,50,.8)
    selector=AudioVisualSelector();rows=[];phases=[];n=0
    definitions=[('安静时先选视觉目标',.6,None,None,1),('声音来自较小人脸',1.2,25,None,2),
        ('停止说话仍保留目标',1.,None,None,2),('声音方向模糊时不跳人',.8,0,None,2),
        ('声音过期时不参与选人',.6,-25,'stale',2),('锁定目标短暂遮挡',.3,None,'occluded',None),
        ('目标重新出现',.6,None,None,2),('另一个方向持续说话',1.2,-25,None,1)]
    for label,duration,angle,condition,expected in definitions:
        begin=len(rows)
        for _ in range(round(duration/.1)):
            now=1+n*.1;audio=None
            if angle is not None:
                audio=AudioEstimate(AudioDirection.LEFT if angle>8 else AudioDirection.RIGHT if angle< -8 else AudioDirection.CENTER,
                    .9,0.,.2,float(angle),.95,.9,2.5,'speech_like',observed_monotonic=now-(1 if condition=='stale' else 0),
                    sample_sequence=n,temporal_stability=1.,azimuth_std_deg=6.,speech_backend='simulation')
            people={1:[big]} if condition=='occluded' else {1:[big],2:[small]}
            winner=selector.update(people,now=now,size=(640,360),audio=audio)
            rows.append(dict(t=round(n*.1,3),phase=label,selected=winner,audio_angle=angle,condition=condition,
                             diagnostics=selector.metrics.copy()))
            n+=1
        phases.append(dict(label=label,start=rows[begin]['t'],end=rows[-1]['t'],selected=winner,expected=expected,
                           passed=winner==expected,mode=selector.metrics['mode'],audio_reason=selector.metrics['audio_reason']))
    result=dict(scope='Synthetic decision-sequence replay, not physical speaker recognition or room-angle accuracy',
                hardware_io=False,servo_io=False,faces=[dict(id=1,angle=-25,size='large'),dict(id=2,angle=25,size='small')],
                phases=phases,samples=rows,checks={phase['label']:phase['passed'] for phase in phases})
    (out/'selector-replay.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    assert all(result['checks'].values())
    audio=json.loads((out/'audio-replay/validation.json').read_text(encoding='utf-8'))
    plt.rcParams.update({'font.family':'Microsoft YaHei','axes.unicode_minus':False,'font.size':10,
        'figure.facecolor':'#fffefa','axes.facecolor':'#fffefa','axes.spines.top':False,'axes.spines.right':False})
    fig,axes=plt.subplots(3,1,figsize=(12,8),gridspec_kw={'height_ratios':[1,1,1.1]},layout='constrained')
    for key,color in [('speech_delay_9','#117a69'),('speech_delay_-9','#3c6c9d')]:
        case=audio['cases'][key];timeline=case['timeline'];valid=[r for r in timeline if r['accepted']]
        axes[0].plot([r['t'] for r in valid],[r['angle'] for r in valid],'.',markersize=1.7,color=color,
                     label=f"注入时差 {9 if 'delay_9' in key else -9} 个采样点，参考角 {case['expected_angle_deg']:.2f}°")
    axes[0].set(ylabel='估计水平角 / °',title='语音回放：已知时差、独立轻噪声；只显示通过人声与时差检查的窗口',xlim=(0,60),ylim=(-38,38))
    axes[0].legend(loc='upper right',fontsize=8,ncol=2)
    keys=['silence','independent_white_noise','narrowband_tone','recorded_ear_rubbing']
    names=['静音 5 s','独立白噪声 5 s','纯音 5 s','真实摩擦录音 3 s']
    fractions=[audio['cases'][key]['direction_accepted_windows']/audio['cases'][key]['windows']*100 for key in keys]
    axes[1].barh(names,fractions,color=['#82baac','#82baac','#82baac','#b85b29'],height=.5)
    for i,value in enumerate(fractions):axes[1].text(value+.018,i,f'{value:.2f}%',va='center',fontsize=9)
    axes[1].set(xlim=(0,1.1),xlabel='通过单窗口方向门控的比例 / %',title='噪声检查：摩擦录音仍有 1 个短暂误报，选人另需多次连续确认')
    t=[r['t'] for r in rows];selected=[r['selected'] if r['selected'] is not None else 0 for r in rows]
    axes[2].step(t,selected,where='post',color='#117a69',linewidth=2.3)
    for i,phase in enumerate(phases):
        axes[2].axvspan(phase['start'],phase['end']+.1,color=('#dff0e8' if i%2==0 else '#edf1f3'),zorder=-1)
        axes[2].text((phase['start']+phase['end']+.1)/2,2.37,str(i+1),ha='center',fontsize=9)
    axes[2].set(yticks=[0,1,2],yticklabels=['无新目标','目标 1：大脸','目标 2：小脸'],ylim=(-.2,2.65),
                xlabel='回放时间 / s',title='合成多人决策回放：1 安静 → 2 小脸方向声音 → 3 停声 → 4 模糊 → 5 过期 → 6 遮挡 → 7 恢复 → 8 新方向声音')
    for ax in axes:ax.grid(axis='x',alpha=.15)
    fig.suptitle('R11 · 声音辅助选人和视觉保持的软件验证',fontsize=16,fontweight='bold')
    fig.savefig(out/'guided-validation.png',dpi=150)
    print(json.dumps(dict(phases=len(phases),all_checks=all(result['checks'].values()),plot=str(out/'guided-validation.png'))))


if __name__=='__main__':main()
