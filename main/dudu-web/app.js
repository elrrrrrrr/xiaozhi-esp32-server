const accessKey=null; // API credentials are kept on the server.
const assetPaths=JSON.parse(document.querySelector('meta[name="app-assets"]')?.content||'{}');
function appURL(path){return new URL(assetPaths[path]||path,location.href);}
const [{VOICES,DEFAULT_VOICE},{THEMES},{sessionConfig},{MODELS,connectionURL,validToken,safeMessage}]=await Promise.all([import(appURL('/voices.js').href),import(appURL('/themes.js').href),import(appURL('/session.js').href),import(appURL('/config.js').href)]);
const $ = id => document.getElementById(id);
const phone = document.querySelector('.phone');
const ui = { button:$('call-button'), title:$('call-title'), subtitle:$('call-subtitle'), message:$('message'), transcript:$('transcript'), mute:$('mute-button'), interrupt:$('interrupt-button') };
const emptyTemplate=$('empty-state').cloneNode(true);
let config, ws, stream, context, capture, micSource, analyser, silentGain;
let state='idle', generation=0, muted=false, persona='friend', activeResponse=null, suppressed=false;
let startedAt=0, tick, raf, lastSpeechStop=0, nextAudioAt=0;
let totalTokens=0, interruptions=0, lastLatency=null, inputRow=null, outputRow=null, allEntries=[], usageRecords=[];
const inputRows=new Map();
let sessionModel='', sessionMode='', connectionTimer, callTimer;
let previewing=false, previewAudio=null;
let resourceSessionId=null, resourcePollPending=false;
let theme='gordon', sessionVoice=DEFAULT_VOICE;
const sources=new Set();
const character=()=>THEMES[theme];
const mobileLayout=window.matchMedia('(max-width:650px)');
function setupResponsiveLayout(){
  const picker=document.querySelector('.theme-picker'),topics=document.querySelector('.intro .scenes'),sidebar=$('conversation-sidebar');
  const pickerAnchor=document.createComment('desktop-partner-picker'),topicsAnchor=document.createComment('desktop-topics'),sidebarAnchor=document.createComment('desktop-transcript');
  picker.before(pickerAnchor);topics.before(topicsAnchor);sidebar.before(sidebarAnchor);
  const apply=()=>{
    if(mobileLayout.matches){$('mobile-preferences').append(picker,topics);$('mobile-conversation-slot').append(sidebar);}
    else{pickerAnchor.after(picker);topicsAnchor.after(topics);sidebarAnchor.after(sidebar);$('conversation-dialog').close();}
    $('settings-title').textContent=mobileLayout.matches?'伙伴与声音':'声音与通话';
  };
  mobileLayout.addEventListener('change',apply);apply();
}
function changeVoice(){
  const selected=VOICES.find(v=>v.id===$('voice-select').value);
  $('custom-voice-label').hidden=$('voice-select').value!=='custom';
  $('voice-description').textContent=selected?`${selected.name} · ${[selected.gender,selected.description].filter(Boolean).join(' · ')}${selected.language==='英文'?'。这个声音用英文交流。':''}`:'填入已在百炼创建、适用于当前对话模型的克隆音色 ID。';
  document.querySelectorAll('.voice-chip').forEach(button=>{const chosen=button.dataset.voice===$('voice-select').value;button.classList.toggle('selected',chosen);button.setAttribute('aria-pressed',String(chosen));});
}
function setupVoices(){
  const select=$('voice-select');select.replaceChildren();
  for(const group of new Set(VOICES.map(v=>v.group))){
    const optgroup=document.createElement('optgroup');optgroup.label=group;
    VOICES.filter(v=>v.group===group).forEach(v=>{
      const option=document.createElement('option');option.value=v.id;
      const version='';
      option.textContent=`${v.name}${version} · ${v.description}`;optgroup.append(option);
    });select.append(optgroup);
  }

  $('voice-count').textContent=`${VOICES.length} 种内置声音`;
  const recommended=VOICES.map(v=>v.id);
  recommended.forEach(id=>{const voice=VOICES.find(v=>v.id===id),button=document.createElement('button');button.type='button';button.className='voice-chip';button.dataset.voice=id;button.textContent=voice.name;
    const description=document.createElement('small');description.textContent=voice.description;button.append(description);button.addEventListener('click',()=>{select.value=id;changeVoice();});$('voice-shortcuts').append(button);
  });
  select.value=DEFAULT_VOICE;changeVoice();
}
function chooseTheme(id){
  if(!THEMES[id]||!['idle','error','ended'].includes(state))return;
  resetSession();
  theme=id;const selected=character();document.body.dataset.theme=id;
  $('intro-title').textContent=selected.title;$('intro-copy').textContent=selected.intro;
  $('character-image').src=appURL(selected.image).href;$('character-image').alt=`${selected.name}在${selected.place}等嘟嘟来玩`;
  $('character-tag').textContent=`${selected.series} · ${selected.name}`;$('screen-mode').textContent=`${selected.place} · ${selected.name}专线`;
  phone.setAttribute('aria-label',`${selected.name}的电话机`);
  $('voice-select').value=selected.voice;changeVoice();
  document.querySelectorAll('.theme-card').forEach(b=>{const chosen=b.dataset.theme===id;b.classList.toggle('selected',chosen);b.setAttribute('aria-pressed',String(chosen));});
  setState('idle');
}
function note(text=''){text=safeMessage(text,accessKey);ui.message.textContent=text;ui.message.classList.toggle('error',/失败|无法|没有|权限|超时|占用|无效/.test(text));}
function setState(next){
  state=next;phone.dataset.state=next;
  const active=!['idle','error','ended'].includes(next), ready=['listening','thinking','speaking'].includes(next);
  ui.button.classList.toggle('active',active);
  ui.button.disabled=!active&&!config?.configured;
  ui.button.setAttribute('aria-label',active?'挂断电话':'拨打电话');
  $('call-button-label').textContent=active?'挂断电话':'拨打电话';
  $('phone-led').classList.toggle('active',ready);
  $('device-state').textContent={idle:'待机',connecting:'呼叫中',listening:'正在听',thinking:'想一想',speaking:'说话中',ended:'已挂断',error:'未接通'}[next]||'待机';
  ui.mute.disabled=!ready||previewing;ui.interrupt.disabled=!ready;
  $('text-input').disabled=!ready||previewing;$('send-button').disabled=!ready||previewing;
  $('model-select').disabled=active;$('mode-select').disabled=active;
  $('voice-select').disabled=active;$('custom-voice').disabled=active;$('preview-button').disabled=active;
  $('preview-button').textContent=previewing&&active?(next==='connecting'?'正在连接声音…':'正在试听…'):'▷ 试听这个声音';
  document.querySelectorAll('.theme-card,.voice-chip').forEach(b=>b.disabled=active);
  document.querySelectorAll('.scene').forEach(b=>b.disabled=active);
  const name=character().name;
  const copy={idle:[`${name}准备好了`,'按下绿色按钮，一起出发吧'],connecting:[`正在呼叫${name}`,`稍等一下，${name}马上就到`],listening:[`${name}在听呢`,'慢慢说，我在这里'],thinking:['让我想一想','这可真是一个好主意'],speaking:[`${name}正在说话`,'想说什么，随时告诉我'],ended:['下次再一起冒险','想聊天时，随时打给我'],error:['暂时没有接通','看看下方的提示，再试一次吧']}[next];
  if(copy){ui.title.textContent=copy[0];ui.subtitle.textContent=copy[1];}
  if(!active&&config?.offline){ui.subtitle.textContent='已离线，联网后就能打电话';$('call-button-label').textContent='等待联网';$('device-state').textContent='离线';ui.button.setAttribute('aria-label','等待联网');}
  $('mic-label').textContent=ready?(previewing?'音色试听 · 麦克风未开启':muted?'麦克风已静音':'麦克风已开启'):'麦克风尚未开启';
}
function addEntry(role,text=''){
  $('empty-state')?.remove();
  const node=document.createElement('div');node.className=`transcript-entry ${role}`;
  const label=document.createElement('div');label.className='entry-label';label.textContent=role==='user'?'嘟嘟 / 家长':character().name;
  const content=document.createElement('p');content.className='entry-text';content.textContent=text||'…';
  node.append(label,content);ui.transcript.append(node);ui.transcript.scrollTop=ui.transcript.scrollHeight;
  const record={role,text,time:new Date().toISOString(),interrupted:false};allEntries.push(record);
  $('export-button').disabled=false;
  return {node,label,content,record};
}
function updateRow(row,text){row.content.textContent=text||'…';row.record.text=text;ui.transcript.scrollTop=ui.transcript.scrollHeight;}
function stopAudio(){
  for(const source of sources){try{source.stop();}catch{}}
  sources.clear();nextAudioAt=0;
}
function interrupt(manual=false){
  if(!activeResponse&&!sources.size)return;
  stopAudio();suppressed=true;
  if(manual&&activeResponse)send({type:'response.cancel'});
  if(outputRow&&!outputRow.record.interrupted){
    outputRow.record.interrupted=true;
    const tag=document.createElement('span');tag.className='interrupted-label';tag.textContent='已打断';outputRow.label.append(tag);
  }
  interruptions++;$('interruptions-stat').textContent=interruptions;setState('listening');
}
function playAudio(b64,event){
  if(!context||suppressed)return;
  if(event.response_id&&activeResponse&&event.response_id!==activeResponse)return;
  const raw=atob(b64), bytes=new Uint8Array(raw.length);
  for(let i=0;i<raw.length;i++)bytes[i]=raw.charCodeAt(i);
  const pcm=new DataView(bytes.buffer), count=Math.floor(bytes.length/2);
  if(!count)return;
  const buffer=context.createBuffer(1,count,24000), values=buffer.getChannelData(0);
  for(let i=0;i<count;i++)values[i]=pcm.getInt16(i*2,true)/32768;
  const source=context.createBufferSource();source.buffer=buffer;source.connect(context.destination);
  const at=Math.max(context.currentTime+.025,nextAudioAt);
  if(lastSpeechStop&&lastLatency===null){lastLatency=Math.round(performance.now()-lastSpeechStop);$('latency-stat').textContent=`${(lastLatency/1000).toFixed(2)} 秒`;$('latency-label').textContent=`等待 ${(lastLatency/1000).toFixed(2)} 秒`;}
  nextAudioAt=at+buffer.duration;sources.add(source);source.start(at);setState('speaking');
  source.onended=()=>{sources.delete(source);if(!sources.size&&!activeResponse&&state==='speaking'){if(previewing){hangup('idle');note('试听结束，可以换一个声音再听听。');}else setState('listening');}};
}
function send(e){if(ws?.readyState===WebSocket.OPEN)ws.send(JSON.stringify(e));}
function onEvent(e){
  switch(e.type){
    case 'session.updated':
      resourceSessionId=e.resource_session_id||null;
      if(state!=='connecting')break;
      clearTimeout(connectionTimer);startedAt=Date.now();setState('listening');
      callTimer=setTimeout(()=>{hangup('ended');note('本次已通话 30 分钟，可以稍后再打给好朋友。');},30*60*1000);
      tick=setInterval(()=>{const s=Math.floor((Date.now()-startedAt)/1000);$('duration').textContent=`${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`;},1000);
      // No recording is sent before the cloud confirms the session configuration.
      {const selected=VOICES.find(v=>v.id===sessionVoice);
      sendText(previewing?(selected?.language==='英文'?`Say only: Hello Dudu! I'm your friend. Ready for an adventure?`:`请用明亮、生动、亲切的卡通角色语气，只说这一句话：嘟嘟，你好呀！我是${character().name}，一起出发吧！`):character().greeting,false);}
      break;
    case 'input_audio_buffer.speech_started':
      interrupt(false);inputRow=null;setState('listening');break;
    case 'input_audio_buffer.speech_stopped':
      if(e.reason!=='turn_invalid'){lastSpeechStop=performance.now();lastLatency=null;setState('thinking');}break;
    case 'conversation.item.input_audio_transcription.delta':
      inputRow=inputRows.get(e.item_id)||inputRow||addEntry('user');
      if(e.item_id)inputRows.set(e.item_id,inputRow);
      updateRow(inputRow,e.text!==undefined?(e.text||'')+(e.stash||''):inputRow.record.text+(e.delta||''));break;
    case 'conversation.item.input_audio_transcription.completed':
      if(e.transcript){inputRow=inputRows.get(e.item_id)||inputRow||addEntry('user');if(e.item_id)inputRows.set(e.item_id,inputRow);updateRow(inputRow,e.transcript);}break;
    case 'conversation.item.input_audio_transcription.failed':note('这句话的字幕没有识别出来，可以再说一次。');break;
    case 'response.created':
      activeResponse=e.response?.id||e.response_id||'pending';suppressed=false;outputRow=null;break;
    case 'response.audio.delta':case 'response.output_audio.delta':playAudio(e.delta,e);break;
    case 'response.audio_transcript.delta':case 'response.output_audio_transcript.delta':
      if(suppressed)break;
      if(!outputRow)outputRow=addEntry('assistant');updateRow(outputRow,outputRow.record.text+(e.delta||''));break;
    case 'response.audio_transcript.done':case 'response.output_audio_transcript.done':
      if(!suppressed&&e.transcript){if(!outputRow)outputRow=addEntry('assistant');updateRow(outputRow,e.transcript);}break;
    case 'app.usage':{
      const usage=e.usage;
      if(usage){usageRecords.push(usage);totalTokens+=Number(usage.total_tokens||0);$('tokens-stat').textContent=totalTokens.toLocaleString();}
      break;
    }
    case 'response.done':{
      const usage=e.response?.usage;
      if(usage){usageRecords.push(usage);totalTokens+=Number(usage.total_tokens||Number(usage.input_tokens||0)+Number(usage.output_tokens||0));$('tokens-stat').textContent=totalTokens.toLocaleString();}
      if(e.response?.status==='failed'){note(e.response.status_details?.error?.message||'本轮回答失败，请再说一次。');}
      if(!e.response?.id||activeResponse===e.response.id)activeResponse=null;
      if(!sources.size&&['thinking','speaking'].includes(state)){if(previewing){hangup('idle');note('试听结束。');}else setState('listening');}break;
    }
    case 'error':{
      const message=e.error?.message||e.error?.code||'百炼返回错误，请稍后重拨。';
      if(/no.*response|not.*active|cancel|truncate/i.test(message))break;
      const initializing=state==='connecting';note(message);if(initializing)hangup('error');break;
    }
    case 'app.error':note(e.message);hangup('error');break;
    case 'app.notice':note(e.message);hangup('ended');break;
    case 'app.closed':if(!['idle','ended','error'].includes(state)){note(e.message);hangup('error');}break;
  }
}
function drawWave(){
  let rms=0;
  if(analyser){const data=new Uint8Array(analyser.fftSize);analyser.getByteTimeDomainData(data);for(const v of data)rms+=((v-128)/128)**2;rms=Math.sqrt(rms/data.length);}
  const level=muted?0:Math.min(1,rms*8);
  const bars=document.querySelectorAll('.wave i');
  bars.forEach((bar,i)=>{const shape=Math.sin((i+1)/(bars.length+1)*Math.PI);const speaking=state==='speaking'?Math.abs(Math.sin(performance.now()/190+i*.6))*.65:0;bar.style.height=`${5+Math.max(level,speaking)*24*shape}px`;});
  raf=requestAnimationFrame(drawWave);
}
function resetSession(){
  resourceSessionId=null;
  totalTokens=0;interruptions=0;lastLatency=null;lastSpeechStop=0;usageRecords=[];allEntries=[];inputRow=null;outputRow=null;
  inputRows.clear();
  $('tokens-stat').textContent='—';$('latency-stat').textContent='—';$('interruptions-stat').textContent='0';$('duration').textContent='00:00';$('latency-label').textContent='随时可以插话';
  ui.transcript.replaceChildren(emptyTemplate.cloneNode(true));$('export-button').disabled=true;note();
}
async function previewVoice(){
  if(!['idle','ended','error'].includes(state))return;
  const current=++generation;
  previewing=true;note();setState('connecting');
  previewAudio=new Audio(`/api/preview?voice=${encodeURIComponent($('voice-select').value)}`);
  previewAudio.onplaying=()=>{if(current===generation)setState('speaking');};
  previewAudio.onended=()=>{if(current===generation){hangup('idle');note('试听结束。');}};
  previewAudio.onerror=()=>{if(current===generation){hangup('error');note('音色试听失败，请检查 Edge TTS 网络连接或服务端日志。');}};
  try{await previewAudio.play();}catch{if(current===generation){hangup('error');note('音色试听失败，请检查网络后重试。');}}
}
async function call(preview=false){
  if(preview){await previewVoice();return;}
  if(!['idle','ended','error'].includes(state)){hangup();return;}
  if(!config?.configured){note(config?.offline?'请联网后再拨打电话。':'请先在项目 .env 中配置 DASHSCOPE_API_KEY，然后重启本地服务。');return;}
  if(!preview&&!navigator.mediaDevices?.getUserMedia){note('请用 Chrome 或 Safari 打开 HTTPS 页面，并允许使用麦克风。');return;}
  const selectedVoice=$('voice-select').value==='custom'?$('custom-voice').value.trim():$('voice-select').value;
  sessionVoice=selectedVoice;
  if(!/^[A-Za-z0-9_.-]{1,160}$/.test(selectedVoice)){note('请填入有效的百炼克隆音色 ID。');return;}
  previewing=preview;
  const current=++generation;resetSession();setState('connecting');muted=false;ui.mute.classList.remove('muted');$('mute-label').textContent='静音';
  sessionModel=$('model-select').value;sessionMode=$('mode-select').value;
  try{
    context=new AudioContext({latencyHint:'interactive'});await context.resume();
    if(current!==generation)return;
    if(!preview){
    const incoming=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
    if(current!==generation){incoming.getTracks().forEach(t=>t.stop());return;}
    stream=incoming;
    await context.audioWorklet.addModule(appURL('/capture-worklet.js').href);
    if(current!==generation)return;
    micSource=context.createMediaStreamSource(stream);analyser=context.createAnalyser();analyser.fftSize=256;micSource.connect(analyser);
    capture=new AudioWorkletNode(context,'phone-capture');silentGain=context.createGain();silentGain.gain.value=0;
    micSource.connect(capture);capture.connect(silentGain);silentGain.connect(context.destination);
    capture.port.onmessage=({data})=>{
      if(current!==generation||muted||!['listening','thinking','speaking'].includes(state)||ws?.readyState!==WebSocket.OPEN)return;
      if(ws.bufferedAmount>256000){note('网络暂时跟不上，请稍后重拨。');hangup('error');return;}
      const bytes=new Uint8Array(data);let raw='';for(const b of bytes)raw+=String.fromCharCode(b);
      send({type:'input_audio_buffer.append',audio:btoa(raw)});
    };
    }
    ws=new WebSocket(connectionURL(accessKey,sessionModel));
    ws.onopen=()=>{if(current===generation)send({type:'session.update',event_id:crypto.randomUUID(),session:sessionConfig({mode:sessionMode,persona,voice:selectedVoice,theme})});};
    ws.onmessage=({data})=>{if(current===generation){try{onEvent(JSON.parse(data));}catch(err){console.error('Event processing failed:',safeMessage(err.message,accessKey));}}};
    ws.onerror=()=>{if(current===generation){note('未能连接本地语音服务，请检查终端日志。');hangup('error');}};
    ws.onclose=()=>{if(current===generation&&!['idle','ended','error'].includes(state)){note('通话连接已断开，请重新拨打。');hangup('error');}};
    connectionTimer=setTimeout(()=>{if(current===generation){note('连接超时，请挂断后重试。');hangup('error');}},25000);
    drawWave();
  }catch(err){
    if(current!==generation)return;
    const messages={NotAllowedError:'麦克风权限未开启。请在地址栏的权限设置中允许麦克风，然后重拨。',NotFoundError:'没有找到麦克风。请连接麦克风后重拨。',NotReadableError:'麦克风正被其他应用占用，请关闭占用后重拨。'};
    note(messages[err.name]||`无法开始通话：${err.message}`);hangup('error');
  }
}
function hangup(end='ended'){
  generation++;clearTimeout(connectionTimer);clearTimeout(callTimer);clearInterval(tick);cancelAnimationFrame(raf);
  if(previewAudio){previewAudio.pause();previewAudio.removeAttribute('src');previewAudio.load();previewAudio=null;}
  if(ws){ws.onclose=null;ws.onerror=null;ws.onmessage=null;try{ws.close();}catch{}ws=null;}
  stopAudio();activeResponse=null;suppressed=false;
  if(capture){capture.port.onmessage=null;capture.disconnect();capture=null;}
  micSource?.disconnect();silentGain?.disconnect();micSource=null;analyser=null;silentGain=null;
  stream?.getTracks().forEach(t=>t.stop());stream=null;
  if(context){context.close().catch(()=>{});context=null;}
  previewing=false;document.querySelectorAll('.wave i').forEach(bar=>bar.style.height='6px');setState(end);
}
function sendText(text,show=true){
  if(!text.trim()||!['listening','thinking','speaking'].includes(state))return;
  if(activeResponse||sources.size)interrupt(true);
  if(show)addEntry('user',text.trim());inputRow=null;lastSpeechStop=performance.now();lastLatency=null;
  send({type:'conversation.item.create',item:{type:'message',role:'user',content:[{type:'input_text',text:text.trim().slice(0,1200)}]}});
  send({type:'response.create'});setState('thinking');
}
ui.button.addEventListener('click',()=>call(false));
$('settings-button').addEventListener('click',()=>$('parent-dialog').showModal());
$('settings-close').addEventListener('click',()=>$('parent-dialog').close());
$('transcript-button').addEventListener('click',()=>$('conversation-dialog').showModal());
$('transcript-close').addEventListener('click',()=>$('conversation-dialog').close());
$('conversation-dialog').addEventListener('click',event=>{if(event.target!==event.currentTarget)return;const rect=event.currentTarget.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)event.currentTarget.close();});
$('parent-dialog').addEventListener('click',event=>{if(event.target!==event.currentTarget)return;const rect=event.currentTarget.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)event.currentTarget.close();});
$('preview-button').addEventListener('click',()=>call(true));
$('voice-select').addEventListener('change',changeVoice);
document.querySelectorAll('.theme-card').forEach(button=>button.addEventListener('click',()=>{chooseTheme(button.dataset.theme);if(mobileLayout.matches)$('parent-dialog').close();}));
ui.mute.addEventListener('click',()=>{muted=!muted;stream?.getAudioTracks().forEach(t=>t.enabled=!muted);ui.mute.classList.toggle('muted',muted);$('mute-label').textContent=muted?'取消静音':'静音';ui.mute.setAttribute('aria-label',muted?'取消麦克风静音':'静音麦克风');$('mic-label').textContent=muted?'麦克风已静音':'麦克风已开启';});
ui.interrupt.addEventListener('click',()=>interrupt(true));
$('clear-button').addEventListener('click',()=>{ui.transcript.replaceChildren(emptyTemplate.cloneNode(true));inputRows.clear();inputRow=null;outputRow=null;});
$('text-form').addEventListener('submit',e=>{e.preventDefault();const input=$('text-input');sendText(input.value);input.value='';});
document.querySelectorAll('.scene').forEach(button=>button.addEventListener('click',()=>{persona=button.dataset.persona;document.querySelectorAll('.scene').forEach(b=>{const selected=b.dataset.persona===persona;b.classList.toggle('selected',selected);b.setAttribute('aria-pressed',String(selected));});}));
$('export-button').addEventListener('click',async()=>{
  const resources=await fetchResources(true);
  const report={createdAt:new Date().toISOString(),theme,voice:sessionVoice,model:sessionModel,turnDetection:sessionMode,interruptions,totalTokens,usageRecords,transcript:allEntries,resources};
  const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download=`嘟嘟电话-${new Date().toISOString().slice(0,10)}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
window.addEventListener('pagehide',()=>hangup());
async function fetchResources(includeSamples=false){
  const query=new URLSearchParams();
  if(resourceSessionId)query.set('session_id',resourceSessionId);
  if(includeSamples)query.set('samples','1');
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),4000);
  try{
    const response=await fetch(`/api/resources?${query}`,{cache:'no-store',signal:controller.signal});
    if(!response.ok)return null;
    const data=await response.json();
    const current=data.current,summary=data.session?.summary||data.window;
    if(!current)return data;
    const mib=value=>`${(value/1048576).toFixed(0)} MiB`;
    $('resource-rss').textContent=mib(current.rss_bytes);
    $('resource-rss-peak').textContent=summary.rss_bytes?mib(summary.rss_bytes.max):'—';
    $('resource-cpu').textContent=current.cpu_core_percent===null?'—':`${current.cpu_core_percent.toFixed(1)}%`;
    $('resource-cpu-summary').textContent=summary.cpu_core_percent?`${summary.cpu_core_percent.mean.toFixed(1)}% / ${summary.cpu_core_percent.max.toFixed(1)}%`:'—';
    $('resource-available').textContent=mib(current.host_available_bytes);
    $('resource-lag').textContent=current.event_loop_lag_ms===null?'—':`${current.event_loop_lag_ms.toFixed(1)} ms`;
    $('resource-scope').textContent=data.session?'本次通话统计':'服务采样统计';
    return data;
  }catch{return null;}finally{clearTimeout(timeout);}
}
$('resource-export').addEventListener('click',async()=>{
  const data=await fetchResources(true);if(!data){note('暂时无法读取资源采样，请检查本地服务。');return;}
  const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
  const link=document.createElement('a');link.href=url;link.download=`嘟嘟电话-资源-${new Date().toISOString().slice(0,10)}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
setInterval(async()=>{
  if(document.hidden||!$('parent-dialog').open||resourcePollPending)return;
  resourcePollPending=true;
  try{await fetchResources();}finally{resourcePollPending=false;}
},1000);
async function refreshConnection(){
  let health;
  try{const response=await fetch('/api/health',{cache:'no-store'});if(!response.ok)throw new Error();health=await response.json();}
  catch{config={configured:false,offline:true};$('connection-label').textContent='本地服务未连接';$('connection-dot').classList.remove('ready');if(['idle','ended','error'].includes(state)){setState(state);note('请先启动本地服务。');}return;}
  config={configured:health.configured,offline:false};
  $('connection-dot').classList.toggle('ready',health.configured);
  $('connection-label').textContent=health.configured?'好朋友在线':'等待配置文本模型';
  $('model-select').replaceChildren(new Option(health.model,health.model));
  if(['idle','ended','error'].includes(state)){setState(state);note(health.configured?'':'可以先试听声音；配置 .env 中的 DASHSCOPE_API_KEY 并重启后即可通话。');}
}
window.addEventListener('offline',()=>{if(!['idle','ended','error'].includes(state))hangup('ended');refreshConnection();});
window.addEventListener('online',refreshConnection);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshConnection();});
function init(){setupResponsiveLayout();setupVoices();chooseTheme('gordon');$('provider-label').textContent='本地语音识别 · 文本对话 · Edge TTS';refreshConnection();fetchResources();}
init();
