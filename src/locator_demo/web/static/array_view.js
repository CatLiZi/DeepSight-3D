/* Read-only presentation of each pair's actual role and coordinate frame. */
(function(root){
  'use strict';
  const finite=v=>typeof v==='number'&&Number.isFinite(v);
  function channel(capture,ch){
    const roles={ear_left:'左耳',ear_right:'右耳',body_left:'身体左',body_right:'身体右'};
    const role=capture?.channel_roles?.[String(ch)] ||
      (ch===capture?.ear_left_channel?'ear_left':ch===capture?.ear_right_channel?'ear_right':null);
    const active=Array.isArray(capture?.active_channels)?capture.active_channels.includes(ch):Boolean(role);
    return {active,label:role?`${roles[role] || '麦克风'} CH${ch}`:`CH${ch}${active?' 参与':' 未参与'}`};
  }
  const angle=value=>!finite(value)?'—':Math.abs(value)<.5?'正前方':`${value>0?'左':'右'}${Math.abs(value).toFixed(1)}°`;
  const reasons={pair_consensus:'两对方向一致，已融合',body_fallback:'身体方向补充，已转换到头部坐标',
    pair_conflict:'两对存在冲突，保留双耳方向',ear_fallback:'双耳方向有效',
    no_head_direction:'等待头部坐标中的可靠方向'};
  function summary(capture){
    if(capture?.localization_mode!=='four_mic')return '';
    const f=capture.fusion || {},body=capture.pairs?.body;
    const uses=[];
    if(f.body_spectral_used)uses.push('身体频段辅助');
    if(f.body_voice_used)uses.push('身体人声辅助');
    if(f.body_direction_used)uses.push('身体角度融合');
    return `四麦协同：左耳CH${capture.ear_left_channel} / 右耳CH${capture.ear_right_channel}，身体左CH${capture.body_left_channel} / 右CH${capture.body_right_channel}。`+
      (body?.usable?`身体方向 ${angle(body.azimuth_deg)}（身体坐标）；`:'身体方向待可靠人声；')+
      `${reasons[f.status] || '等待四路新音频'}${uses.length?' · '+uses.join('、'):''}。`+
      (finite(f.head_yaw_deg)?`头部相对身体 ${angle(f.head_yaw_deg)}；`:'')+
      (finite(f.body_head_azimuth_deg)?`身体声源换到头部坐标 ${angle(f.body_head_azimuth_deg)}；`:'')+
      (f.pose_time_aligned?'姿态已对齐音频时刻（反馈约束的水平角估计）。':'')+
      (!f.body_direction_used&&f.reason==='head_body_pose_unavailable'?'当前姿态未完成角度换算，身体继续提供声学辅助。':'')+
      '不输出声音距离。';
  }
  const api={channel,summary,angle};
  if(typeof module==='object'&&module.exports)module.exports=api;
  root.ArrayView=api;
})(typeof globalThis!=='undefined'?globalThis:this);
