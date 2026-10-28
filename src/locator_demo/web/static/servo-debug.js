"use strict";
const $=id=>document.getElementById(id);
const labels={yaw:"轴 A / ID 0",pitch:"轴 B / ID 2",moving:"转动中",completed:"已到达",stopped:"已停止",left:"向左",right:"向右",up:"抬头",down:"低头",none:"未转动",unknown:"待确认",smooth:"顺滑",sticky:"卡顿",shaking:"抖动 / 异响"};
let current=null,busy=false,polling=false;
function message(text,error=false){$("message").textContent=text;$("message").className=error?"error":"";}
async function api(path,body){const r=await fetch("/api/debug/"+path,body===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw new Error(data.detail||"操作失败");return data;}
async function act(path,body={},success="操作已完成"){
  if(busy&&path!=="stop")return;
  busy=true;if(current)render(current);
  try{const result=await api(path,body);render(result);message(success);}catch(e){message(e.message,true);}finally{busy=false;if(current)render(current);}
}
function render(data){
  current=data;
  const all=Object.values(data.axes),online=all.filter(a=>a.fresh&&!a.fault).length;
  $("connection").textContent=`${data.port} · ${online} / 2 个舵机反馈正常`;
  $("connection-note").textContent=data.active_move?"正在手动测试，完成后可记录实物观察。":data.enabled?"自动转头关闭。确认实物周围无遮挡，再逐轴操作。":"已停止或尚未连接，请点击「读取位置 / 重新连接」。";
  $("connect").disabled=busy||!!data.active_move;
  for(const name of ["yaw","pitch"]){
    const a=data.axes[name],can=a.can_move&&!busy;
    $(name+"-id").textContent=a.servo_id;
    $(name+"-position").textContent=a.fresh?a.position:"—";
    $(name+"-origin").textContent=a.origin??"—";
    $(name+"-range").textContent=`配置范围 ${a.minimum}–${a.maximum}`;
    const badge=$(name+"-status");badge.textContent=a.fault?"反馈异常":a.fresh?data.active_axis===name?"转动中":"反馈正常":"未读取 / 已过期";
    badge.className="badge "+(a.fault?"bad":a.fresh?"good":"");
    $(name+"-note").textContent=a.fault||(a.fresh?`实际回包 ${a.raw} · 更新于 ${Math.max(0,a.age_s).toFixed(1)} 秒前`:a.last_position!==null?`上次有效位置 ${a.last_position}，当前不能据此发令。`:"尚未获得位置回包；数字是位置刻度，尚未标定为角度。");
    document.querySelectorAll(`[data-axis="${name}"].move,[data-return="${name}"],[data-target="${name}"]`).forEach(b=>b.disabled=!can);
    const target=$(name+"-target");target.min=a.minimum;target.max=a.maximum;
  }
  const last=data.moves.at(-1);$("save-observation").disabled=busy||!last||last.status==="moving";
  $("last-move").textContent=last?`动作 ${last.id} · ${labels[last.axis]} · ${last.from} → ${last.target} · ${labels[last.status]}`:"完成一次动作后，在这里记录。";
  const tbody=$("moves");tbody.replaceChildren();
  if(!data.moves.length){const tr=document.createElement("tr"),td=document.createElement("td");td.colSpan=6;td.textContent="尚未发出运动指令";td.className="muted";tr.append(td);tbody.append(tr);}
  for(const row of data.moves.slice(-12).reverse()){
    const tr=document.createElement("tr"),o=row.observation;
    for(const text of [row.id,`ID ${row.servo_id}`,`${row.from} → ${row.target}`,row.actual??"—",labels[row.status]+(row.reason?`：${row.reason}`:""),o?`${labels[o.direction]} · ${labels[o.smoothness]}`:"待记录"]){const td=document.createElement("td");td.textContent=String(text);tr.append(td);}tbody.append(tr);
  }
  $("record-path").textContent="记录自动保存在本机："+data.record_path;
  draw(data.trace,data.axes);
}
function chartBounds(axes){
  const values=Object.values(axes||{});
  return values.length ? {minimum:Math.min(...values.map(a=>a.minimum)),maximum:Math.max(...values.map(a=>a.maximum))} : {minimum:800,maximum:2200};
}
function draw(trace,axes){
  const canvas=$("trace"),width=canvas.clientWidth||480,height=170,scale=window.devicePixelRatio||1;canvas.width=width*scale;canvas.height=height*scale;
  const c=canvas.getContext("2d");c.scale(scale,scale);c.clearRect(0,0,width,height);c.font="11px Segoe UI";c.fillStyle="#9eafb3";c.strokeStyle="#31424a";
  const bounds=chartBounds(axes),span=Math.max(1,bounds.maximum-bounds.minimum);
  for(let i=0;i<=4;i++){const value=bounds.minimum+span*i/4,y=145-(value-bounds.minimum)/span*125;c.beginPath();c.moveTo(35,y);c.lineTo(width-5,y);c.stroke();c.fillText(String(Math.round(value)),0,y+4);}
  if(!trace.length){c.fillText("读取位置后显示实际反馈",50,85);return;}
  const end=trace.at(-1).t,start=end-25;
  for(const [name,color]of[["yaw","#96e5be"],["pitch","#efba85"]])for(const field of["position","planned"]){c.strokeStyle=color;c.lineWidth=field==="position"?2:1;c.setLineDash(field==="planned"?[4,4]:[]);c.beginPath();let previous=null;for(const p of trace){if(p.axis!==name||p.t<start||p[field]===null)continue;const x=35+(p.t-start)/25*(width-40),y=145-(p[field]-bounds.minimum)/span*125;if(previous===null||p.t-previous>1.2)c.moveTo(x,y);else c.lineTo(x,y);previous=p.t;}c.stroke();}c.setLineDash([]);
}
$("connect").addEventListener("click",()=>act("connect",{},"位置已读取；绿色的轴可以开始手动测试。"));
$("stop").addEventListener("click",()=>act("stop",{},"已停止后续发令。继续测试前请重新读取位置。"));
document.addEventListener("keydown",e=>{if(e.code==="Space"&&!/INPUT|SELECT|TEXTAREA/.test(e.target.tagName)){e.preventDefault();act("stop",{},"已停止后续发令。继续测试前请重新读取位置。");}});
document.querySelectorAll(".move").forEach(b=>b.addEventListener("click",()=>act("move",{axis:b.dataset.axis,delta:Number($(b.dataset.axis+"-step").value)*Number(b.dataset.sign)},"动作已开始，请观察实物。")));
document.querySelectorAll("[data-return]").forEach(b=>b.addEventListener("click",()=>act("move",{axis:b.dataset.return,return_origin:true},"正在返回本次记录的起点。")));
document.querySelectorAll("[data-target]").forEach(b=>b.addEventListener("click",()=>{const input=$(b.dataset.target+"-target");if(input.reportValidity())act("move",{axis:b.dataset.target,target:Number(input.value)},"动作已开始，请观察实物。");}));
$("observation").addEventListener("submit",e=>{e.preventDefault();const last=current?.moves.at(-1);if(last)act("observation",{move_id:last.id,direction:$("observed-direction").value,smoothness:$("observed-smoothness").value,note:$("observed-note").value},"本次观察已保存；可以继续测试另一个方向。" );});
async function poll(){if(polling||busy)return;polling=true;try{render(await api("state"));}catch(e){message("网页连接中断，请检查本地服务。",true);document.querySelectorAll(".move,[data-return],[data-target]").forEach(b=>b.disabled=true);}finally{polling=false;}}
poll();setInterval(poll,400);
