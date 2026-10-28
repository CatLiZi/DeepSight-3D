"""Reorganize the backed-up UI without changing its functional DOM contracts."""
from pathlib import Path
from collections import Counter
import json,re
from bs4 import BeautifulSoup

PROJECT=Path(__file__).resolve().parents[1]
BACKUP=PROJECT/'ui_backups/r30-before-redesign-20261027/static'
original=BeautifulSoup((BACKUP/'index.html').read_text(encoding='utf-8'),'html.parser')
shell=(PROJECT/'tools/console_shell.html').read_text(encoding='utf-8')
def take(selector):
    node=original.select_one(selector)
    assert node is not None,selector
    return node.extract()
def slot(name,content):
    global shell
    marker='<!-- '+name+' -->';assert shell.count(marker)==1,name
    shell=shell.replace(marker,str(content))
def combined(nodes):return '\n'.join(str(n) for n in nodes)

actions=[]
for id_,label,icon in [('start-btn','开始运行','i-play'),('stop-btn','停止','i-stop'),('center-btn','回中',None)]:
    button=take('#'+id_);button.clear();button['type']='button'
    if icon:button.append(BeautifulSoup(f'<svg class="icon" aria-hidden="true"><use href="#{icon}"/></svg>','html.parser'))
    button.append(label);actions.append(button)
actions[1]['class']=['stop-button']
slot('GLOBAL_ACTIONS',combined(actions))
slot('FEATURE_SWITCHES',combined([n.extract() for n in original.select('.actions .toggle')]))

# Audio: keep live inputs visible; place numerical diagnostics and configuration
# in distinct areas. No field, ID, value, or input limit is discarded.
slot('DIRECTION_CANVAS',take('#direction-canvas'))
slot('DIRECTION_VALUE',take('#audio-direction'))
slot('EAR_LEVELS',take('#ear-levels'))
audio_metrics=take('.audio-panel .metrics')
for empty in list(audio_metrics.select('label')):
    if not empty.find('strong'):empty.decompose()
slot('AUDIO_DIAGNOSTICS',combined([audio_metrics,take('#array-audio-note'),take('#speech-vad-note'),take('#audio-observation-status')]))
slot('EAR_CONFIG',take('.audio-panel .device-config:not(.audio-config)'))
slot('AUDIO_CONFIG',take('.audio-config'))

slot('MAIN_PREVIEW',take('#video-wrap'))
binocular=take('#binocular-panel')
face=binocular.select_one('#face-score-panel').extract();face['class']=['panel','face-score-panel']
face.h3.string='候选匹配';slot('FACE_SCORE',face)
note_ids=['binocular-coordinate-note','visual-approach-status']
notes=[binocular.select_one('#'+name).extract() for name in note_ids]
binocular_status=binocular.select_one('#binocular-status').extract()
guidance_status=binocular.select_one('#av-guidance-status').extract()
people=binocular.select_one('#binocular-people').extract()
slot('BINOCULAR_CONTENT',binocular)
slot('VISION_STATUS',combined([guidance_status,binocular_status,take('#capture-metrics'),people]))
slot('VISION_NOTES',combined(notes+[take('#stereo-space-status')]))

rectification=original.select_one('#rectification-enabled').find_parent('label').extract()
slot('RECTIFICATION_CONTROL',rectification)
slot('CALIBRATION',take('#calibration-panel'))
focus=take('#focus-panel');focus.attrs.pop('open',None);focus.attrs.pop('style',None);slot('FOCUS',focus)
slot('STEREO',take('#stereo-panel'))
slot('CAMERA_CONFIG',take('.camera-config'))
runtime=[take('#microphone-capture-metrics'),take('#motion-metrics'),take('.video-panel .metrics')]
slot('RUNTIME_DIAGNOSTICS',combined(runtime))

servo=original.select_one('#servo-config').find_parent('section')
# The heading ID is provided by the new shell; all motion controls keep their
# existing event handlers and attributes, and no new motion shortcut is added.
servo.select_one('.panel-head').decompose()
manual=[];settings=[]
for node in list(servo.find_all(recursive=False)):
    classes=node.get('class',[])
    if any(c in classes for c in ('axis-config','direction-config','control-config')) or node.get('id') in ('save-axis-limits-btn','direction-config-status','visual-calibration-btn','visual-calibration-result','scan-result'):
        settings.append(node.extract())
    else:manual.append(node.extract())
settings.append(take('#scan-btn'))
slot('MANUAL_CONTROL',combined(manual));slot('MOTION_CONFIG',combined(settings))
slot('CLEAR_LOG',take('#clear-log-btn'));slot('EVENT_LOG',take('#event-log'))

new=BeautifulSoup(shell,'html.parser')
assert not re.search(r'<!-- [A-Z_]+ -->',str(new))
for node in new.find_all(style=True):
    # Styling is owned by the design system. Geometry and display updates remain
    # in the original runtime code, including the native 1:1 focus preview.
    del node['style']
translations={'Face detector':'人脸检测器','SCRFD model':'模型路径','SCRFD th':'检测阈值','SCRFD size':'模型输入尺寸',
 'Strategy':'跟踪策略','ASD':'说话人判别','Lock policy':'目标保持策略','Visual yaw':'水平跟随','Visual pitch':'俯仰跟随',
 'Mirror X':'水平映射','Yaw dead':'水平死区','Yaw step':'水平最小步进','Yaw max':'水平最大步进',
 'Pitch dead':'俯仰死区','Pitch step':'俯仰最小步进','Pitch max':'俯仰最大步进','Target X':'水平瞄准偏移',
 'Target Y':'俯仰瞄准偏移','Speaker th':'视觉候选阈值','Mouth th':'嘴部活动阈值','Near face':'初选人脸比例',
 'Keep face':'保持人脸比例','TalkNet th':'TalkNet 阈值','Hold s':'保持时间 / s','Lost s':'丢失等待 / s',
 'Audio interrupt':'声音打断','Silent visual search':'无声搜索','Silent hold s':'无声保持 / s',
 'Audio th':'声音置信度','Speech th':'人声阈值','DoA th':'方向置信度','Audio hits':'连续确认次数'}
for label in new.select('label'):
    for child in list(label.children):
        if isinstance(child,str) and child.strip() in translations:child.replace_with(translations[child.strip()]+' ')
for id_,caption in {'yaw-min-input':'水平下限 / 刻度','yaw-center-input':'水平中位 / 刻度','yaw-max-input':'水平上限 / 刻度',
                    'pitch-min-input':'俯仰下限 / 刻度','pitch-center-input':'俯仰中位 / 刻度','pitch-max-input':'俯仰上限 / 刻度'}.items():
    label=new.find(id=id_).find_parent('label')
    for child in list(label.children):
        if isinstance(child,str) and child.strip():child.replace_with(caption+' ');break
for link in new.select('.nav-item'):
    link['aria-label']=link.get_text(' ',strip=True)
for icon in new.select('svg.icon'):icon['aria-hidden']='true'
for select in new.select('select'):
    values={'small':'小步跟随','off':'关闭','full':'完整范围','rules':'规则融合','turn hold':'持续保持',
        'until lost':'丢失后释放','interruptible':'允许打断','classic audio first':'经典声音优先','crowded visual first':'多人视觉优先'}
    for option in select.find_all('option'):
        if option.get_text(strip=True) in values:option.string=values[option.get_text(strip=True)]
new.select_one('#visual-calibration-btn').string='记录视觉样本'
new.select_one('#visual-calibration-result').string='尚未记录视觉样本'
new.select_one('#preview')['alt']='当前跟踪视角画面'
new.select_one('#clear-log-btn').string='清空当前列表'
for id_ in ('yaw-value','pitch-value'):new.select_one('#'+id_).string='—'

# Compare the untouched backup, not the extracted tree.
baseline=BeautifulSoup((BACKUP/'index.html').read_text(encoding='utf-8'),'html.parser')
old_ids={n['id'] for n in baseline.select('[id]')};new_ids=[n['id'] for n in new.select('[id]')]
assert old_ids<=set(new_ids),sorted(old_ids-set(new_ids))
assert not [id_ for id_,count in Counter(new_ids).items() if count>1]
for old in baseline.select('input,select'):
    if not old.get('id'):continue
    current=new.find(id=old['id'])
    for attr in ('type','min','max','step','value','checked'):
        assert old.get(attr)==current.get(attr),(old['id'],attr,old.get(attr),current.get(attr))
(PROJECT/'src/locator_demo/web/static/index.html').write_text(str(new),encoding='utf-8')
print(json.dumps({'preserved_original_ids':len(old_ids),'unique_current_ids':len(new_ids),'hardware_input_contracts_preserved':True},ensure_ascii=False))
