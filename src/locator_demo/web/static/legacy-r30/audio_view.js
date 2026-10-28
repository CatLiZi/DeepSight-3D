/* Display-only audio memory and angular projection. Never sends control data. */
(function(root) {
  "use strict";
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const clamp = (value, lo, hi) => Math.max(lo, Math.min(hi, value));

  class AudioPresentation {
    constructor(holdSeconds = 1.0) { this.holdSeconds = holdSeconds; this.reset(); }
    reset() { this.last = null; this.session = null; this.watermark = null; this.endedAt=null; this.inputUsable=false; this.selectorAccepted=false; this.reason='unavailable'; }
    update(audio, selection, nowMs, session = "") {
      if (session !== this.session) { this.reset(); this.session = session; }
      const age = audio?.age_s;
      const observation = audio?.raw_observation;
      const identityValid = !observation || (finite(observation.observed_monotonic) && observation.observed_monotonic>0 &&
        Number.isInteger(observation.sample_sequence) && observation.sample_sequence>=0);
      const doa=audio?.doa_confidence ?? audio?.confidence;
      const spread=audio?.azimuth_std_deg ?? 12;
      const fault=audio?.capture?.sample_clock_valid===false || audio?.capture?.ok===false;
      if(fault || !finite(nowMs)) { this.reset(); this.session=session; return {active:false,historical:false,age_s:null,reason:'unhealthy_capture'}; }
      // Acoustic display does not require a face, a video timestamp, or permission
      // from the target selector. All steering continues to use backend evidence.
      let usable = ['center','left','right','left_front','right_front'].includes(audio?.direction) &&
        [audio?.azimuth_deg, audio?.confidence, audio?.speech_confidence, doa, audio?.temporal_stability, spread, age].every(finite) &&
        Math.abs(audio.azimuth_deg) <= 90 && audio.confidence >= .2 && audio.confidence <= 1 && doa >= .2 && doa <= 1 && audio.speech_confidence >= .5 && audio.speech_confidence <= 1 &&
        audio.temporal_stability >= .5 && audio.temporal_stability <= 1 && spread>=0 && spread<=25 && age >= 0 && age <= .35 && !audio.motor_suppressed && identityValid;
      const key = observation && finite(observation.observed_monotonic) && Number.isInteger(observation.sample_sequence)
        ? `${observation.sample_sequence}/${observation.observed_monotonic}` : null;
      if(usable && observation && this.watermark && key!==this.watermark.key && observation.observed_monotonic<=this.watermark.time) usable=false;
      if (usable && (!this.last || key === null || key !== this.last.key)) {
        const width = finite(selection?.sector_half_width_deg) ? selection.sector_half_width_deg : 15;
        this.last = {key, at: nowMs-age*1000, azimuth_deg: audio.azimuth_deg,
          spread_deg: clamp(width, 5, 30), confidence: audio.confidence};
        if(observation)this.watermark={key,time:observation.observed_monotonic};
        this.endedAt=null;
      }
      this.inputUsable=usable;this.selectorAccepted=Boolean(selection?.audio_usable);
      this.reason=audio?.noise_state || 'unavailable';
      return this.read(nowMs);
    }
    read(nowMs) {
      const elapsed = this.last ? Math.max(0, (nowMs-this.last.at)/1000) : Infinity;
      if (!this.last || !finite(nowMs)) {
        return {active:false, historical:false, age_s:null, reason:this.reason};
      }
      const current = this.inputUsable && elapsed <= .35;
      if(current)this.endedAt=null;
      // Explicit loss of a direction starts one full second of visible hold.
      // With no new messages, expiry starts at the last evidence freshness limit;
      // a cached sample or a dead WebSocket can never prolong the range forever.
      else if(this.endedAt===null)this.endedAt=Math.min(nowMs,this.last.at+350);
      const heldSeconds=this.endedAt===null ? 0 : Math.max(0,(nowMs-this.endedAt)/1000);
      if(!current && heldSeconds>=this.holdSeconds)
        return {active:false,historical:false,age_s:elapsed,reason:this.reason};
      return {...this.last, active:true, historical:!current, age_s:elapsed,
        alpha:.34,remaining_hold_s:current ? null : Math.max(0,this.holdSeconds-heldSeconds),
        selector_accepted:current && this.selectorAccepted,
        label:current ? "声源方向范围" : "声音结束后保留1秒（仅显示）"};
    }
  }

  function projectSector(project, degrees, halfWidth, height) {
    if (!Array.isArray(project) || project.length!==3 || !project.every(row => Array.isArray(row) && row.length===3 && row.every(finite)) ||
        ![degrees,halfWidth,height].every(finite) || Math.abs(degrees)>90 || halfWidth<=0 || halfWidth>30 || height<=0) return null;
    function line(angle) {
      const x = -Math.tan(clamp(angle,-89.5,89.5)*Math.PI/180);
      const points=[];
      for (const v of [0,height]) {
        const denominator=project[1][1]-v*project[2][1];
        if (Math.abs(denominator)<1e-9) return null;
        const y=(v*(project[2][0]*x+project[2][2])-(project[1][0]*x+project[1][2]))/denominator;
        const z=project[2][0]*x+project[2][1]*y+project[2][2];
        const u=(project[0][0]*x+project[0][1]*y+project[0][2])/z;
        if (z<=0 || !finite(u)) return null;
        points.push([u,v]);
      }
      return points;
    }
    const a=line(degrees-halfWidth),b=line(degrees+halfWidth),center=line(degrees);
    return a && b && center ? {polygon:[a[0],b[0],b[1],a[1]],center} : null;
  }

  function reasonText(reason) {
    return ({no_voice_activity:"有声音，但尚未通过人声检测",quiet:"耳部信号较轻",motor_guard:"转头噪声保护：方向暂不参与控制",
      ambiguous_delay:"已收到声音，时差存在多个候选峰",low_coherence:"两耳信号一致性不足",direction_transition:"方向变化确认中",
      inconsistent_frequency_bands:"不同频段给出的方向不一致",stale_audio:"音频已过期",audio_gap:"音频不连续",
      sample_clock_mismatch:"音频时钟异常，等待恢复",speech_like:"声学方向已产生",ear_pair:"声学方向已产生",
      channels_nearly_identical:"两耳波形近乎相同，暂不输出方向",replayed_audio_observation:"重复或乱序音频，等待新观察",
      unhealthy_capture:"采集或采样时钟异常，暂不显示方向范围"})[reason] || reason || "等待音频";
  }

  // Smooth angular closeness, not a calibrated probability of speaker identity.
  function angularWeight(offset,halfWidth) {
    if(!finite(offset) || !finite(halfWidth) || halfWidth<=0 || Math.abs(offset)>=halfWidth)return 0;
    return Math.cos(Math.PI*offset/(2*halfWidth))**2;
  }

  function angularSlices(view,count=64) {
    if(!view?.active || ![view.azimuth_deg,view.spread_deg,view.alpha].every(finite) || Math.abs(view.azimuth_deg)>90 ||
       view.spread_deg<=0 || view.spread_deg>30 || view.alpha<=0 || view.alpha>1 || !Number.isInteger(count) || count<4 || count>128)return [];
    const result=[],step=2*view.spread_deg/count;
    for(let i=0;i<count;i++){
      const offset=-view.spread_deg+(i+.5)*step;
      result.push({angle:view.azimuth_deg+offset,halfWidth:step/2,
        alpha:view.alpha*angularWeight(offset,view.spread_deg)});
    }
    return result;
  }

  function drawSectorOverlay(ctx,projection,view,width,height) {
    if(!finite(width) || !finite(height) || width<=0 || height<=0)return false;
    const sector=projectSector(projection,view?.azimuth_deg,view?.spread_deg,height);
    const slices=angularSlices(view);
    if(!sector || !slices.length)return false;
    ctx.save();
    // Project each angular strip rather than a horizontal CSS rectangle. This
    // preserves the same bearing/roll reference used by each camera image.
    for(const part of slices){
      if(Math.abs(part.angle)>89.5)continue;
      const geometry=projectSector(projection,part.angle,part.halfWidth,height);
      if(!geometry)continue;
      ctx.fillStyle=`rgba(96,165,250,${part.alpha})`;ctx.beginPath();
      geometry.polygon.forEach(([x,y],i)=>i ? ctx.lineTo(x,y) : ctx.moveTo(x,y));ctx.closePath();ctx.fill();
    }
    ctx.strokeStyle=view.historical ? '#93a9c3' : '#60a5fa';ctx.lineWidth=2;ctx.setLineDash([10,8]);
    ctx.beginPath();ctx.moveTo(...sector.center[0]);ctx.lineTo(...sector.center[1]);ctx.stroke();
    ctx.restore();return true;
  }

  function drawCompass(ctx,view,width,height) {
    const cx=width/2,cy=height*.82,radius=Math.min(width*.45,height*.75);
    const radians=angle=>(-90-angle)*Math.PI/180;
    ctx.save();ctx.clearRect(0,0,width,height);ctx.fillStyle='#f8fafc';ctx.fillRect(0,0,width,height);
    const wedge=(low,high)=>{ctx.beginPath();ctx.moveTo(cx,cy);ctx.arc(cx,cy,radius,radians(high),radians(low));ctx.closePath();};
    const sectors=[['左侧',60,90],['左前',15,60],['正前方',-15,15],['右前',-60,-15],['右侧',-90,-60]];
    for(const [,low,high] of sectors){wedge(low,high);ctx.fillStyle='#e8eef5';ctx.fill();ctx.strokeStyle='#fff';ctx.stroke();}
    const slices=angularSlices(view);
    for(const part of slices){
      const low=clamp(part.angle-part.halfWidth,-90,90),high=clamp(part.angle+part.halfWidth,-90,90);
      if(high<=low)continue;
      wedge(low,high);ctx.fillStyle=`rgba(96,165,250,${Math.min(.65,part.alpha*1.7)})`;ctx.fill();
    }
    for(const [name,low,high] of sectors){
      const angle=radians((low+high)/2);
      ctx.fillStyle='#52606d';ctx.font='13px Segoe UI';ctx.textAlign='center';
      ctx.fillText(name,cx+Math.cos(angle)*radius*.67,cy+Math.sin(angle)*radius*.67);
    }
    if(slices.length){
      const angle=radians(view.azimuth_deg);
      ctx.strokeStyle=view.historical ? '#93a9c3' : '#3b82f6';ctx.lineWidth=2;
      ctx.beginPath();ctx.moveTo(cx,cy);ctx.lineTo(cx+Math.cos(angle)*radius,cy+Math.sin(angle)*radius);ctx.stroke();
    }
    ctx.beginPath();ctx.arc(cx,cy,14,0,Math.PI*2);ctx.fillStyle='#17202a';ctx.fill();
    ctx.fillStyle='#52606d';ctx.font='12px Segoe UI';ctx.textAlign='center';
    ctx.fillText(view?.active ? `${view.historical ? '最近方向 · ' : ''}${Math.abs(view.azimuth_deg)<.5 ? '正前方 0°' : (view.azimuth_deg>0 ? '左 ' : '右 ')+Math.abs(view.azimuth_deg).toFixed(0)+'°'} ±${view.spread_deg}°` : '等待可靠声音方向',cx,height-9);
    ctx.restore();return slices.length>0;
  }

  const api={AudioPresentation,projectSector,reasonText,angularWeight,angularSlices,drawSectorOverlay,drawCompass};
  root.AudioView=api;
  if (typeof module!=="undefined" && module.exports) module.exports=api;
})(globalThis);
