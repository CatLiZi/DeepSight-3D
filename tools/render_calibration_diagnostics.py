"""Plot recorded residuals without opening hardware or changing calibration."""
import argparse,json
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot as plt,font_manager


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--replay',type=Path,required=True);args=parser.parse_args()
    font_manager.fontManager.addfont('C:/Windows/Fonts/msyh.ttc');plt.rcParams['font.family']='Microsoft YaHei'
    plt.rcParams['axes.unicode_minus']=False
    standard=json.loads((args.replay/'grouped-standard.json').read_text(encoding='utf-8'))['quality']
    robust=json.loads((args.replay/'grouped-robust.json').read_text(encoding='utf-8'))['quality']
    rows=robust['residual_audit']['views'];held=[r for r in rows if r['role']=='holdout']
    before={r['index']:r for r in standard['residual_audit']['views']}
    fig,axes=plt.subplots(2,2,figsize=(13.2,8.2),dpi=145);fig.patch.set_facecolor('#f5f4ef')
    ax=axes[0,0];x=np.arange(len(held))
    ax.plot(x,[before[r['index']]['cross_projection_p95_px'] for r in held],'o--',color='#8b9494',ms=4,label='普通联合估计')
    ax.plot(x,[r['cross_projection_p95_px'] for r in held],'o-',color='#117a69',ms=4,label='Soft-L1 抗异常点估计')
    ax.axhline(3,color='#b85b29',ls=':',label='3 px 门槛');ax.set_xticks(x,[r['index']+1 for r in held],fontsize=8)
    ax.set(xlabel='留出姿态编号',ylabel='每姿态投影误差 P95 / px',title='同一 65 组参数拟合 / 18 组验证');ax.legend(fontsize=8)
    ax=axes[0,1]
    for q,label,color in ((standard,'普通估计','#8b9494'),(robust,'抗异常点估计','#117a69')):
        errors=np.sort([e for row in q['residual_audit']['views'] if row['role']=='holdout' for e in row['cross_projection_errors_px']])
        ax.plot(errors,np.arange(1,len(errors)+1)/len(errors),label=label,color=color)
    ax.axvline(3,color='#b85b29',ls=':');ax.axhline(.95,color='#8b9494',ls=':',lw=.8)
    ax.set(xlabel='投影误差 / px（包含所有验证角点）',ylabel='累计比例',title='所有验证角点保留，未按残差删点');ax.legend(fontsize=8)
    ax=axes[1,0]
    for role,label,color in (('train','拟合姿态','#8b9494'),('holdout','验证姿态','#117a69')):
        chosen=[r for r in rows if r['role']==role]
        ax.scatter([max(r['left_planar_p95_px'],r['right_planar_p95_px']) for r in chosen],
                   [r['cross_projection_p95_px'] for r in chosen],label=label,color=color,s=24,alpha=.8)
    ax.axvline(3,color='#b85b29',ls=':');ax.set(xlabel='两眼较大的平面拟合 P95 / px',ylabel='跨目投影 P95 / px',title='平面拟合仅作诊断，不确定物理原因');ax.legend(fontsize=8)
    ax=axes[1,1];ax.axis('off')
    lines=['本轮结果与使用边界',
           f'投影 P95：{standard["holdout_reprojection_p95_px"]:.2f} → {robust["holdout_reprojection_p95_px"]:.2f} px（仍需 <3）',
           f'行对齐 P95：{standard["holdout_epipolar_p95_px"]:.2f} → {robust["holdout_epipolar_p95_px"]:.2f} px',
           f'全部 {len(rows)} 姿态保留；平面拟合警告 {robust["residual_audit"]["warning_views"]} 组',
           '4 秒采集分组，拟合与验证边界隔离 0.7 秒',
           '本次输入含运动候选，纸面平整度未核验',
           '新结果仍为 rejected；未替换现有对齐预览',
           '未启用三维距离、自动转向或物理精度声明']
    for i,line in enumerate(lines):ax.text(.02,.96-i*.115,line,fontsize=12 if i==0 else 10.2,color='#18323d' if i<5 else '#955027',va='top')
    fig.suptitle('双目标定优化：时间分组验证 + 抗异常点联合估计 + 逐样本诊断',fontsize=17,y=.985)
    fig.text(.5,.018,'历史数据开发回放，尚未进行新的独立实物验收；测量噪声估计也不包含标定或打印尺度误差。',ha='center',fontsize=10,color='#955027')
    fig.tight_layout(rect=(.01,.045,.99,.95));fig.savefig(args.replay.parent/'calibration-diagnostics.png',facecolor=fig.get_facecolor());plt.close(fig)


if __name__=='__main__':main()
