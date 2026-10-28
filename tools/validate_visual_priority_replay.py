"""Synthetic sector choices and delayed image feedback, with no device I/O."""
from dataclasses import asdict
import json
from pathlib import Path
import sys
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from locator_demo.visual_priority import VisualPrioritySelector,progressive_visual_step
from locator_demo.head import HeadControllerLogic,HeadPose,DirectionConfig
from locator_demo.motion import SmoothMotionPlanner
from test_visual_priority import face
from test_av_guidance import voice


def main():
    out=ROOT.parent/'hardware_inventory/visual-priority-20261026';out.mkdir(exist_ok=True)
    selector=VisualPrioritySelector();rows=[];phases=[];sequence=0
    definitions=[('无声：视觉选大脸',None,{1:[face(1,0,width=140)],2:[face(2,-35,width=70)]},1),
        ('同一声音扇区：选较大人脸',-35,{1:[face(1,0,width=140)],2:[face(2,-30,width=95)],3:[face(3,-39,width=40)]},2),
        ('停止说话：视觉保持',None,{1:[face(1,0,width=100)],2:[face(2,-30,width=95)]},2),
        ('声音无视觉对应：保持视觉',67,{1:[face(1,0,width=100)],2:[face(2,-30,width=95)]},2),
        ('视觉目标短暂丢失',None,{1:[face(1,0,width=100)]},None),
        ('目标恢复：使用新观察',None,{1:[face(1,0,width=100)],2:[face(2,-30,width=95)]},2)]
    for label,angle,people,expected in definitions:
        start=len(rows)
        count=3 if '短暂' in label else 15
        for _ in range(count):
            now=1+sequence*.1;audio=voice(angle,now,sequence) if angle is not None else None
            selected=selector.update(people,now=now,size=(640,360),audio=audio)
            rows.append(dict(t=sequence*.1,selected=selected,phase=label,diagnostics=selector.metrics.copy()))
            sequence+=1
        phases.append(dict(label=label,selected=selected,expected=expected,passed=selected==expected,
                           start=rows[start]['t'],end=rows[-1]['t']))
    decision=dict(scope='Synthetic visual/audio candidate fixture; not physical speaker selection or camera field-of-view accuracy',
                  phases=phases,samples=rows,checks={p['label']:p['passed'] for p in phases},hardware_io=False,servo_io=False)
    (out/'sector-decision-replay.json').write_text(json.dumps(decision,ensure_ascii=False,indent=2),encoding='utf-8')
    logic=HeadControllerLogic(direction=DirectionConfig(pitch_up_sign=1))
    planner=SmoothMotionPlanner(HeadPose(1500,1500));rng=np.random.default_rng(26026)
    limits=dict(yaw_deadband=.03,pitch_deadband=.03,max_yaw_delta=30,max_pitch_delta=30,min_yaw_delta=2,min_pitch_delta=0)
    samples=[];moves=[];last_frame=-1;filtered=None;last_identity=None;continuity=[]
    for step in range(901):
        now=step/50;goal=HeadPose(1350,1550) if now<7 else HeadPose(1600,1450)
        states=planner.sample(now);current=HeadPose(*(round(axis[0]) for axis in states))
        frame=int(now*15)
        if frame!=last_frame:
            last_frame=frame;delay_steps=6
            earlier=samples[max(0,len(samples)-delay_steps)]['position'] if samples else [1500,1500]
            measured=np.array([(goal.yaw-earlier[0])/200,(earlier[1]-goal.pitch)/200])+rng.uniform(-.002,.002,2)
            identity=1 if now<7 else 2
            filtered=measured if filtered is None or identity!=last_identity else .55*measured+.45*filtered
            last_identity=identity
            target,metrics=progressive_visual_step(logic,current,*filtered,limits,measured=measured)
            before=np.array(planner.sample(now));accepted=planner.retarget(target,now)
            after=np.array(planner.sample(now));continuity.append(float(np.abs(before-after).max()))
            moves.append(dict(t=now,target=asdict(target),accepted=accepted,**metrics))
        samples.append(dict(t=now,position=[states[0][0],states[1][0]],velocity=[states[0][1],states[1][1]],
                            acceleration=[states[0][2],states[1][2]],goal=[goal.yaw,goal.pitch]))
    positions=np.array([s['position'] for s in samples]);velocities=np.array([s['velocity'] for s in samples])
    accelerations=np.array([s['acceleration'] for s in samples]);error=np.abs(positions[-1]-samples[-1]['goal'])/200
    checks=dict(position_limits=bool(np.all((positions>=1200)&(positions<=1800))),
                velocity_limit=bool(np.max(np.abs(velocities))<=180+1e-6),
                acceleration_limit=bool(np.max(np.abs(accelerations))<=600+1e-6),
                retarget_continuity=max(continuity)<1e-6,final_image_error_under_005=bool(np.max(error)<.05))
    control=dict(scope='Virtual camera/actuator: 15 Hz image observations, 120 ms simulated delay, small image noise, 50 Hz quintic planner. No physical convergence claim.',
                 hardware_io=False,servo_io=False,metric_distance_used=False,checks=checks,
                 final_error_normalized=error.tolist(),max_continuity_error=max(continuity),samples=samples,moves=moves)
    (out/'progressive-feedback-replay.json').write_text(json.dumps(control,indent=2),encoding='utf-8')
    plt.rcParams.update({'font.family':'Microsoft YaHei','axes.unicode_minus':False,'figure.facecolor':'#fffefa',
                        'axes.facecolor':'#fffefa','axes.spines.top':False,'axes.spines.right':False})
    fig,axes=plt.subplots(3,1,figsize=(11,8),layout='constrained')
    t=np.array([s['t'] for s in samples]);goals=np.array([s['goal'] for s in samples])
    for axis,color,label in ((0,'#117a69','左右'),(1,'#3c6c9d','上下')):
        axes[0].plot(t,positions[:,axis],color=color,label=label+'规划位置')
        axes[0].plot(t,goals[:,axis],color=color,ls='--',alpha=.5)
        axes[1].plot(t,(positions[:,axis]-goals[:,axis])/200*(-1 if axis==0 else 1),color=color,label=label+'图像误差')
    axes[0].set(title='虚拟执行器：实线为规划位置，虚线为目标；第 7 秒换目标',ylabel='舵机位置刻度')
    axes[1].axhspan(-.03,.03,color='#dff0e8');axes[1].set(title='每次新画面重新纠偏，不使用目标距离；绿色区域为死区',ylabel='归一化图像误差')
    axes[2].step([m['t'] for m in moves],[m['yaw_step_cap'] if m['yaw_stage']!='hold' else 0 for m in moves],where='post',color='#117a69')
    axes[2].set(title='左右单次调整上限：较大偏差 → 中等偏差 → 精细接近 → 保持',ylabel='单次位置刻度上限',xlabel='模拟时间 / s')
    for ax in axes:ax.grid(alpha=.15)
    axes[0].legend(loc='upper right');fig.suptitle('R12 · 分段逼近与连续轨迹的软件模拟',fontsize=15,fontweight='bold')
    fig.savefig(out/'progressive-feedback.png',dpi=150)
    summary={'decision':decision['checks'],'control':checks,'final_error':control['final_error_normalized']}
    print(json.dumps(summary,ensure_ascii=False))
    return not (all(decision['checks'].values()) and all(checks.values()))


if __name__=='__main__':raise SystemExit(main())
