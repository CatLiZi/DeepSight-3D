/* The face reticle and the control goal are different marks. No shared virtual
   point is ever painted directly into either physical eye image. */
(function(root){
  const finitePoint=p=>Array.isArray(p)&&p.length===2&&p.every(Number.isFinite);
  function marks(visual, eye){
    const f=visual.fixation,t=visual.target;
    if(!t || !f || f.person_id!==t.face_id || !['single_eye','binocular'].includes(f.mode))return null;
    const view=f.eyes?.[eye];
    if(!view || !finitePoint(view.target_px) || !finitePoint(view.goal_px))return null;
    return {point:view.target_px,goal:view.goal_px,goalVisible:!!view.goal_visible,mode:f.mode};
  }
  function cross(ctx,x,y,r,color){
    ctx.beginPath();ctx.arc(x,y,r*.65,0,2*Math.PI);
    ctx.moveTo(x-r,y);ctx.lineTo(x+r,y);ctx.moveTo(x,y-r);ctx.lineTo(x,y+r);
    ctx.strokeStyle='rgba(15,23,42,.8)';ctx.lineWidth=4;ctx.stroke();
    ctx.strokeStyle=color;ctx.lineWidth=2;ctx.stroke();
  }
  function drawReference(ctx,width,height){
    ctx.save();ctx.strokeStyle='rgba(203,213,225,.7)';ctx.lineWidth=1.5;
    ctx.beginPath();ctx.moveTo(width/2-9,height/2);ctx.lineTo(width/2+9,height/2);
    ctx.moveTo(width/2,height/2-9);ctx.lineTo(width/2,height/2+9);ctx.stroke();ctx.restore();
  }
  function drawSelected(ctx,visual,eye){
    const m=marks(visual,eye);if(!m)return false;
    ctx.save();
    if(m.goalVisible){
      ctx.strokeStyle='rgba(110,231,183,.9)';ctx.lineWidth=2;ctx.setLineDash([6,6]);
      ctx.beginPath();ctx.moveTo(...m.point);ctx.lineTo(...m.goal);ctx.stroke();ctx.setLineDash([]);
      ctx.beginPath();ctx.arc(...m.goal,19,0,Math.PI*2);ctx.stroke();
    }
    cross(ctx,...m.point,16,'#facc15');ctx.restore();return true;
  }
  function description(f){
    if(f?.mode==='binocular')return '双眼共同跟随 · 黄色准星：人脸跟踪点 · 绿色圆环：各眼期望落点 · 灰色十字：画面中心';
    if(f?.mode==='single_eye')return `暂用${f.eyes?.left ? '左' : '右'}眼跟随 · 黄色准星：人脸跟踪点 · 绿色圆环：期望落点；另一眼匹配确认后共同参与`;
    if(f?.mode==='stale')return '等待新画面 · 过期跟踪准星已隐藏';
    return '黄色准星：已识别人脸的跟踪点 · 灰色十字：画面中心；双目期望落点等待有效匹配';
  }
  const api={marks,cross,drawReference,drawSelected,description};
  if(typeof module==='object'&&module.exports)module.exports=api;
  root.FixationView=api;
})(typeof globalThis==='object'?globalThis:this);
