(() => {
  'use strict';
  const video=document.querySelector('#camera');
  const canvas=document.querySelector('#fx');
  const ctx=canvas.getContext('2d',{alpha:true});
  const gate=document.querySelector('#gate');
  const startBtn=document.querySelector('#start');
  const errorEl=document.querySelector('#error');
  const modeBtn=document.querySelector('#mode');
  const clearBtn=document.querySelector('#clear');
  const shutterBtn=document.querySelector('#shutter');
  const readout=document.querySelector('#readout');
  const status=document.querySelector('#status');
  const tip=document.querySelector('#tip');
  const flash=document.querySelector('#flash');
  const toast=document.querySelector('#toast');
  const TAU=Math.PI*2;
  const MODES=['BREACH','WATCHER','SWARM'];
  let modeIndex=0,dpr=1,w=0,h=0,started=false,orientationActive=false;
  let yaw=0,pitch=0,lastFrame=performance.now(),pulse=0,stream=null,toastTimer=0;
  let pointerStart=null,pinchStart=null;
  const pointers=new Map(),anomalies=[];
  const stars=Array.from({length:46},()=>({a:Math.random()*TAU,r:.15+Math.random()*.85,s:.3+Math.random()*1.7,p:Math.random()*TAU}));

  function resize(){
    dpr=Math.min(devicePixelRatio||1,2); w=innerWidth; h=innerHeight;
    canvas.width=Math.round(w*dpr); canvas.height=Math.round(h*dpr);
    canvas.style.width=w+'px'; canvas.style.height=h+'px';
    ctx.setTransform(dpr,0,0,dpr,0,0);
  }
  addEventListener('resize',resize,{passive:true}); resize();

  async function startExperience(){
    errorEl.textContent=''; startBtn.disabled=true; startBtn.textContent='REQUESTING ACCESS…';
    try{
      if(!window.isSecureContext) throw new Error('This page is not running as a secure HTTPS page.');
      if(!navigator.mediaDevices?.getUserMedia) throw new Error('Camera access is unavailable in this browser. Open this link in Chrome.');
      stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:'environment'},width:{ideal:1920},height:{ideal:1080}},audio:false});
      video.srcObject=stream; await video.play(); await requestMotionPermission();
      gate.style.display='none'; document.body.classList.add('live'); status.textContent='LIVE'; started=true;
      setTimeout(()=>tip.style.opacity='.35',6500);
      showToast(orientationActive?'MOTION LOCK ACTIVE':'CAMERA ACTIVE');
    }catch(err){
      const name=err?.name||'';
      if(name==='NotAllowedError') errorEl.textContent='Camera permission was blocked. Tap the lock/camera icon in Chrome and allow Camera, then retry.';
      else if(name==='NotFoundError') errorEl.textContent='No usable rear camera was found.';
      else errorEl.textContent=err?.message||'Camera access failed.';
      startBtn.disabled=false; startBtn.textContent='TRY AGAIN';
    }
  }

  async function requestMotionPermission(){
    try{
      if(typeof DeviceOrientationEvent!=='undefined'&&typeof DeviceOrientationEvent.requestPermission==='function'){
        const permission=await DeviceOrientationEvent.requestPermission(); if(permission!=='granted') return;
      }
      addEventListener('deviceorientation',e=>{
        if(e.alpha==null||e.beta==null) return;
        orientationActive=true; yaw=e.alpha; pitch=e.beta;
      },true);
    }catch(_){ }
  }

  function angleDelta(a,b){let d=a-b;while(d>180)d-=360;while(d<-180)d+=360;return d;}
  function worldToScreen(a){
    if(!orientationActive)return{x:a.screenX,y:a.screenY,visible:true};
    const fovX=58,fovY=fovX*h/w,dx=angleDelta(a.anchorYaw,yaw),dy=a.anchorPitch-pitch;
    const x=w*.5+(dx/fovX)*w,y=h*.5-(dy/fovY)*h;
    return{x,y,visible:x>-a.size*2&&x<w+a.size*2&&y>-a.size*2&&y<h+a.size*2};
  }

  function placeAnomaly(x,y){
    const nx=x/w-.5,ny=.5-y/h,fovX=58,fovY=fovX*h/w,now=performance.now();
    anomalies.push({type:MODES[modeIndex],anchorYaw:orientationActive?yaw+nx*fovX:0,anchorPitch:orientationActive?pitch+ny*fovY:0,screenX:x,screenY:y,size:Math.max(70,Math.min(w,h)*(.13+Math.random()*.055)),born:now,seed:Math.random()*9999,phase:Math.random()*TAU,hue:[182,298,32][modeIndex]+Math.random()*18-9});
    navigator.vibrate?.(22); pulse=1; showToast(`${MODES[modeIndex]} PLACED`);
  }

  function nearestAnomaly(x,y){let best=null,bestD=Infinity;for(const a of anomalies){const p=worldToScreen(a),d=Math.hypot(p.x-x,p.y-y);if(d<a.size*1.2&&d<bestD){best=a;bestD=d;}}return best;}
  canvas.addEventListener('pointerdown',e=>{
    canvas.setPointerCapture?.(e.pointerId); pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});
    if(pointers.size===1)pointerStart={x:e.clientX,y:e.clientY,t:performance.now()};
    if(pointers.size===2){const pts=[...pointers.values()],target=nearestAnomaly((pts[0].x+pts[1].x)/2,(pts[0].y+pts[1].y)/2);pinchStart={distance:Math.hypot(pts[0].x-pts[1].x,pts[0].y-pts[1].y),size:target?.size||0,target};}
  });
  canvas.addEventListener('pointermove',e=>{
    if(!pointers.has(e.pointerId))return; pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});
    if(pointers.size===2&&pinchStart?.target){const pts=[...pointers.values()],distance=Math.hypot(pts[0].x-pts[1].x,pts[0].y-pts[1].y);pinchStart.target.size=Math.max(42,Math.min(Math.min(w,h)*.42,pinchStart.size*distance/Math.max(1,pinchStart.distance)));}
  });
  canvas.addEventListener('pointerup',e=>{
    const p=pointers.get(e.pointerId);pointers.delete(e.pointerId);
    if(pointerStart&&p&&performance.now()-pointerStart.t<320&&Math.hypot(p.x-pointerStart.x,p.y-pointerStart.y)<16&&pointers.size===0)placeAnomaly(p.x,p.y);
    if(pointers.size<2)pinchStart=null;if(pointers.size===0)pointerStart=null;
  });
  canvas.addEventListener('pointercancel',e=>{pointers.delete(e.pointerId);pinchStart=null;pointerStart=null;});

  modeBtn.addEventListener('click',()=>{modeIndex=(modeIndex+1)%MODES.length;modeBtn.textContent=MODES[modeIndex];showToast(`MODE: ${MODES[modeIndex]}`);});
  clearBtn.addEventListener('click',()=>{anomalies.length=0;navigator.vibrate?.([15,35,15]);showToast('FIELD CLEARED');});
  startBtn.addEventListener('click',startExperience); shutterBtn.addEventListener('click',capture);

  function drawBreach(a,x,y,s,t){
    ctx.save();ctx.translate(x,y);const age=Math.min(1,(t-a.born)/620),breathe=1+Math.sin(t*.0027+a.phase)*.045;ctx.scale(age*breathe,age*breathe);ctx.globalCompositeOperation='screen';
    for(let ring=7;ring>=0;ring--){const rr=s*(.37+ring*.035+Math.sin(t*.002+ring+a.seed)*.018);ctx.beginPath();for(let i=0;i<=72;i++){const ang=i/72*TAU,noise=Math.sin(ang*3+a.seed)+.55*Math.sin(ang*7-t*.002)+.25*Math.sin(ang*13+t*.004),r=rr+noise*s*.025,px=Math.cos(ang)*r*1.02,py=Math.sin(ang)*r*.76;i?ctx.lineTo(px,py):ctx.moveTo(px,py);}ctx.closePath();ctx.strokeStyle=`hsla(${a.hue+ring*8},100%,${64+ring*2}%,${.08+(7-ring)*.045})`;ctx.lineWidth=1+(7-ring)*.55;ctx.stroke();}
    const g=ctx.createRadialGradient(0,0,s*.03,0,0,s*.44);g.addColorStop(0,'rgba(0,0,0,.98)');g.addColorStop(.52,'rgba(0,0,0,.96)');g.addColorStop(.72,`hsla(${a.hue},100%,50%,.18)`);g.addColorStop(1,'rgba(0,0,0,0)');ctx.fillStyle=g;ctx.beginPath();ctx.ellipse(0,0,s*.48,s*.37,0,0,TAU);ctx.fill();
    ctx.globalCompositeOperation='lighter';for(const st of stars){const ang=st.a+t*.00012*st.s,r=s*.38*st.r;ctx.globalAlpha=.15+.65*Math.max(0,Math.sin(t*.003+st.p));ctx.fillStyle=`hsl(${a.hue+st.s*22},100%,78%)`;ctx.fillRect(Math.cos(ang)*r,Math.sin(ang)*r*.72,st.s,st.s);}ctx.restore();
  }

  function drawWatcher(a,x,y,s,t){
    ctx.save();ctx.translate(x,y+Math.sin(t*.002+a.phase)*s*.025);const age=Math.min(1,(t-a.born)/540);ctx.scale(age,age);const lookX=Math.max(-.18,Math.min(.18,(w*.5-x)/w))*s,lookY=Math.max(-.12,Math.min(.12,(h*.45-y)/h))*s;
    ctx.globalCompositeOperation='screen';const glow=ctx.createRadialGradient(0,0,0,0,0,s*.68);glow.addColorStop(0,`hsla(${a.hue},100%,65%,.20)`);glow.addColorStop(1,'transparent');ctx.fillStyle=glow;ctx.beginPath();ctx.arc(0,0,s*.7,0,TAU);ctx.fill();
    ctx.globalCompositeOperation='source-over';ctx.strokeStyle='rgba(5,5,8,.95)';ctx.fillStyle='rgba(5,5,8,.94)';ctx.lineCap='round';ctx.lineWidth=s*.055;ctx.beginPath();ctx.moveTo(-s*.07,s*.28);ctx.lineTo(-s*.19,s*.53);ctx.moveTo(s*.03,s*.29);ctx.lineTo(s*.15,s*.57);ctx.stroke();ctx.beginPath();ctx.ellipse(-s*.02,s*.26,s*.12,s*.21,-.10,0,TAU);ctx.fill();
    ctx.save();ctx.rotate(-.08);const white=ctx.createRadialGradient(-s*.12,-s*.15,s*.02,0,0,s*.52);white.addColorStop(0,'rgba(255,255,255,.98)');white.addColorStop(.62,'rgba(220,232,238,.96)');white.addColorStop(1,'rgba(86,96,112,.92)');ctx.fillStyle=white;ctx.beginPath();ctx.ellipse(0,0,s*.47,s*.39,0,0,TAU);ctx.fill();ctx.strokeStyle='rgba(4,5,8,.9)';ctx.lineWidth=s*.035;ctx.stroke();ctx.translate(lookX,lookY);const iris=ctx.createRadialGradient(-s*.03,-s*.04,s*.01,0,0,s*.19);iris.addColorStop(0,'#020305');iris.addColorStop(.32,`hsl(${a.hue},78%,23%)`);iris.addColorStop(.72,`hsl(${a.hue},96%,48%)`);iris.addColorStop(1,'#030508');ctx.fillStyle=iris;ctx.beginPath();ctx.arc(0,0,s*.17,0,TAU);ctx.fill();ctx.fillStyle='#000';ctx.beginPath();ctx.arc(0,0,s*.075,0,TAU);ctx.fill();ctx.fillStyle='rgba(255,255,255,.92)';ctx.beginPath();ctx.arc(-s*.048,-s*.052,s*.022,0,TAU);ctx.fill();ctx.restore();ctx.restore();
  }

  function drawSwarm(a,x,y,s,t){
    ctx.save();ctx.translate(x,y);ctx.globalCompositeOperation='screen';const age=Math.min(1,(t-a.born)/500);for(let i=0;i<21;i++){const q=(i+1)/21,ang=a.phase+i*2.399963+t*(.00032+.00025*q),orbit=s*(.12+.58*q)*(1+.12*Math.sin(t*.002+i)),px=Math.cos(ang)*orbit,py=Math.sin(ang)*orbit*.67,sz=s*(.045+.04*(1-q));ctx.save();ctx.translate(px,py);ctx.rotate(ang+Math.PI/2);ctx.globalAlpha=age*(.26+.7*(1-q));ctx.fillStyle=`hsl(${a.hue+i*4},100%,${62+i%3*8}%)`;ctx.beginPath();ctx.moveTo(0,-sz*1.4);ctx.quadraticCurveTo(sz*.75,0,0,sz*.8);ctx.quadraticCurveTo(-sz*.52,0,0,-sz*1.4);ctx.fill();ctx.restore();}ctx.restore();
  }

  function render(t){
    requestAnimationFrame(render);lastFrame=t;ctx.clearRect(0,0,w,h);
    for(const a of anomalies){const p=worldToScreen(a);if(!p.visible)continue;if(a.type==='BREACH')drawBreach(a,p.x,p.y,a.size,t);else if(a.type==='WATCHER')drawWatcher(a,p.x,p.y,a.size,t);else drawSwarm(a,p.x,p.y,a.size,t);}
    const spread=12+pulse*12;ctx.save();ctx.translate(w/2,h/2);ctx.strokeStyle=`rgba(255,255,255,${.34+pulse*.32})`;for(let i=0;i<4;i++){ctx.save();ctx.rotate(i*Math.PI/2);ctx.beginPath();ctx.moveTo(spread,-8);ctx.lineTo(spread,0);ctx.lineTo(spread+8,0);ctx.stroke();ctx.restore();}ctx.restore();pulse*=.91;
    readout.textContent=`${anomalies.length.toString().padStart(2,'0')} ENTITIES · ${orientationActive?`${yaw.toFixed(0)}° / ${pitch.toFixed(0)}°`:'MOTION UNAVAILABLE'}`;
  }
  requestAnimationFrame(render);

  function showToast(text){clearTimeout(toastTimer);toast.textContent=text;toast.classList.add('show');toastTimer=setTimeout(()=>toast.classList.remove('show'),1400);}
  async function capture(){
    if(!started||video.readyState<2)return;flash.animate([{opacity:0},{opacity:.86},{opacity:0}],{duration:220});
    const out=document.createElement('canvas');out.width=Math.round(w*dpr);out.height=Math.round(h*dpr);const oc=out.getContext('2d'),vw=video.videoWidth,vh=video.videoHeight,scale=Math.max(out.width/vw,out.height/vh),dw=vw*scale,dh=vh*scale;oc.drawImage(video,(out.width-dw)/2,(out.height-dh)/2,dw,dh);oc.drawImage(canvas,0,0,out.width,out.height);out.toBlob(blob=>{if(!blob)return;const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=`reality-breach-${Date.now()}.png`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1500);showToast('CAPTURE SAVED');},'image/png');
  }
  addEventListener('beforeunload',()=>stream?.getTracks?.().forEach(track=>track.stop()));
})();
