import {readEvents,loadHistory,saveHistory,TurnGuard,HISTORY_KEY} from './core.js';

const $=id=>document.getElementById(id),guard=new TurnGuard();
let apiKey='',oramaUrl='',history=[],lastReply='',recognition=null,micTimer=null,audioContext=null,gain=null,sources=new Set(),audioEnd=0,audioFinished=false,activeTurn=null,voiceController=null,voiceEpoch=0;
const labels={idle:'Ready when you are',listening:'I’m listening',processing:'Thinking it through',speaking:'Hermes is speaking'};
const state=(name,detail)=>{$('voice-stage').dataset.state=name;$('state').textContent=labels[name];$('voice-detail').textContent=detail||({idle:'Speak or type to begin',listening:'Release to send, or tap the microphone to finish',processing:'Connecting the dots…',speaking:'Press Stop or start talking to interrupt'})[name];};
function notice(text){$('notice').textContent=text;$('notice').hidden=!text;}
function openSettings(){$('settings').showModal();}
for(const id of ['settings-open','connect-open','voice-settings'])$(id).onclick=openSettings;
function safeStorage(){try{return window.localStorage;}catch{return null;}}
const storage=safeStorage();
try{if(storage?.getItem('hermes.remember')==='yes'||storage?.getItem('jarvis.remember')==='yes'){$('remember').checked=true;history=loadHistory(storage);}}catch{}

function save(){if($('remember').checked&&!saveHistory(storage,history))notice('History could not be saved on this device. This conversation remains available in this tab.');}
function contextHistory(){let items=history.slice(-18).map(x=>({...x,content:x.content.slice(0,8000)}));while(items.reduce((n,x)=>n+x.content.length,0)>24000)items.shift();return items;}
function addMessage(role,text){
 $('messages').querySelector('.welcome')?.remove();
 const article=document.createElement('div');article.className='message '+role;
 const author=document.createElement('span');author.className='author';author.textContent=role==='user'?'You':'Hermes';
 const content=document.createElement('span');content.textContent=text;article.append(author,content);$('messages').append(article);scrollMessages();return {article,content,author};
}
function scrollMessages(){$('messages').scrollTop=$('messages').scrollHeight;}
for(const message of history)addMessage(message.role,message.content);
lastReply=history.filter(m=>m.role==='assistant').at(-1)?.content||'';$('replay').disabled=!lastReply;
function stopMic(cancel=true){clearTimeout(micTimer);if(recognition){const current=recognition;recognition=null;if(cancel){current.onend=null;current.onresult=null;current.onerror=null;current.abort();}else current.stop();}$('mic').setAttribute('aria-pressed','false');$('mic').setAttribute('aria-label','Start microphone');}
function stopAudio(){voiceEpoch++;voiceController?.abort();voiceController=null;for(const source of sources){try{source.stop();}catch{}}sources.clear();audioEnd=0;audioFinished=false;}
function stopAll(){guard.cancel();activeTurn=null;stopMic();stopAudio();$('send').disabled=false;state('idle');}
$('stop').onclick=()=>{stopAll();notice('Stopped. You can continue whenever you’re ready.');};
document.addEventListener('keydown',e=>{if(e.key==='Escape')stopAll();});
window.addEventListener('pagehide',stopAll);

async function unlockAudio(){
 const C=window.AudioContext||window.webkitAudioContext;if(!C)return false;
 try{if(!audioContext){audioContext=new C();gain=audioContext.createGain();gain.gain.value=Number($('volume').value);gain.connect(audioContext.destination);}await audioContext.resume();return audioContext.state==='running';}catch{return false;}
}
$('volume').oninput=()=>{if(gain)gain.gain.value=Number($('volume').value);};
$('speak').onchange=()=>{if(!$('speak').checked){const wasVoice=voiceController||sources.size;stopAudio();if(wasVoice)state('idle');}};
const BASE = window.location.pathname.startsWith('/hermes') ? '/hermes' : '';
const oramaSession = document.querySelector('meta[name="orama-session"]')?.content;
const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;

async function call(path,body,signal){
 if(!apiKey && !oramaSession)throw Error('Connect your workspace in settings first.');
  const headers = body===undefined?{}:{'Content-Type':'application/json'};
  if(apiKey) headers['Authorization'] = 'Bearer '+apiKey;
  if(csrfToken) headers['X-CSRF-Token'] = csrfToken;
  const res=await fetch(BASE + path,{method:body===undefined?'GET':'POST',headers,body:body===undefined?undefined:JSON.stringify(body),signal,cache:'no-store'});
  if(!res.ok){if(res.status===401){$('connection-label').textContent=oramaSession?'Sign-in required':'Reconnect required';$('connect-open').classList.remove('connected');}
   throw Error(({401:oramaSession?'Your Orama session expired. Refresh the page to sign in again.':'Your Hermes key was not accepted. Reconnect in settings.',422:'Please check the message and voice settings.',429:'Hermes is busy. Wait a moment and try again.',503:'The service is unavailable. Check your server settings.'})[res.status]||`Request failed (${res.status}). Please try again.`);}
  return res;
 }
async function jsonCall(path,body){const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),90000);try{return await(await call(path,body,controller.signal)).json();}finally{clearTimeout(timer);}}

async function scheduleAudio(event,turn,epoch){
 if(!guard.current(turn.id)||epoch!==voiceEpoch)return;
 if(!audioContext||audioContext.state!=='running')throw Error('Tap “Play last reply” to enable audio playback on this browser.');
 const bytes=Uint8Array.from(atob(event.data),x=>x.charCodeAt(0));
 const buffer=await audioContext.decodeAudioData(bytes.buffer);
 if(!guard.current(turn.id)||epoch!==voiceEpoch)return;
 const source=audioContext.createBufferSource();source.buffer=buffer;source.connect(gain);sources.add(source);
 const start=Math.max(audioContext.currentTime+.035,audioEnd);audioEnd=start+buffer.duration;
 source.onended=()=>{sources.delete(source);if(guard.current(turn.id)&&audioFinished&&!sources.size)state('idle');};
 state('speaking',`${event.provider==='piper'?'Piper · local voice':'Gemini · neural voice'} · ${$('language').value}`);source.start(start);
}
async function speak(text,turn){
 if(!guard.current(turn.id))return;
 const epoch=++voiceEpoch,controller=new AbortController();voiceController=controller;
 const abort=()=>controller.abort();turn.signal.addEventListener('abort',abort,{once:true});
 state('processing','Preparing your voice…');audioFinished=false;
 const request={text:text.slice(0,2000),language:$('language').value,voice:$('voice').value,provider:$('tts-provider').value,local_voice:$('local-voice').value};
 const timer=setTimeout(()=>{if(guard.current(turn.id)){stopAll();notice('Voice timed out. The text is still available.');}},80000);
 let done=false;
 try{const res=await call('/v1/tts/stream',request,controller.signal);
  for await(const e of readEvents(res.body)){
   if(!guard.current(turn.id)||epoch!==voiceEpoch)return;
   if(e.type==='audio')await scheduleAudio(e,turn,epoch);
   else if(e.type==='warning')notice(e.message+($('tts-provider').value==='auto'?' Trying local voice.':''));
   else if(e.type==='error')throw Error(e.message);
   else if(e.type==='done')done=true;
  }
  if(!done)throw Error('Voice connection ended early. Try replaying the reply.');
  if(epoch===voiceEpoch){audioFinished=true;if(!sources.size)state('idle');}
 }catch(e){if(guard.current(turn.id)&&epoch===voiceEpoch){stopAudio();state('idle');if(e.name!=='AbortError')notice(e.message);}}
 finally{clearTimeout(timer);turn.signal.removeEventListener('abort',abort);if(voiceController===controller)voiceController=null;}
}
async function send(message){
 message=message.trim();if(!message)return;
 if(!apiKey && !oramaSession){notice('Connect Hermes using your application key to start a conversation.');openSettings();return;}
 const prior=contextHistory();stopAll();const turn=guard.next();activeTurn=turn;
 // Resume synchronously from the user's gesture, before the network round trip.
 void unlockAudio();notice('');state('processing');$('send').disabled=true;
 addMessage('user',message);history.push({role:'user',content:message});save();$('message').value='';
 const reply=addMessage('assistant','');let complete=false;let text='';
 const timer=setTimeout(()=>{if(guard.current(turn.id)){stopAll();notice('The conversation timed out. Please try again.');}},150000);
 try{const res=await call('/v1/chat/stream',{message,history:prior,provider:$('provider').value},turn.signal);
  for await(const e of readEvents(res.body)){
   if(!guard.current(turn.id))return;
   if(e.type==='delta'){text+=e.text;reply.content.textContent=text;scrollMessages();}
   else if(e.type==='reset'){text='';reply.content.textContent='';}
   else if(e.type==='warning')notice(e.message+($('provider').value==='auto'?' Trying the local model.':''));
   else if(e.type==='error')throw Error(e.message);
   else if(e.type==='done'){complete=true;reply.author.textContent='Hermes · '+e.provider;}
  }
  if(!complete||!text)throw Error('The response ended early. Please try again.');
  history.push({role:'assistant',content:text});history=history.slice(-20);save();lastReply=text;$('replay').disabled=false;
  $('send').disabled=false;clearTimeout(timer);
  if($('speak').checked)await speak(text,turn);else state('idle');
 }catch(e){if(guard.current(turn.id)){reply.author.textContent='Hermes · incomplete';state('idle');if(e.name!=='AbortError')notice(e.message);}}
 finally{clearTimeout(timer);if(guard.current(turn.id))$('send').disabled=false;}
}
$('chat-form').onsubmit=e=>{e.preventDefault();void send($('message').value);};
$('message').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();void send($('message').value);}};
for(const button of document.querySelectorAll('[data-prompt]'))button.onclick=()=>void send(button.dataset.prompt);
$('replay').onclick=async()=>{stopAll();notice('');const turn=guard.next();await unlockAudio();await speak(lastReply,turn);};
$('test-voice').onclick=async()=>{stopAll();notice('');$('settings').close();const turn=guard.next();await unlockAudio();await speak($('language').value==='el-GR'?'Γεια σου. Είμαι ο Ερμής. Πώς μπορώ να σε βοηθήσω;':'Hello. I’m Hermes. What’s on your mind?',turn);};
$('clear').onclick=()=>{stopAll();history=[];lastReply='';$('replay').disabled=true;$('messages').replaceChildren();notice('');try{storage?.removeItem(HISTORY_KEY);}catch{}};
$('remember').onchange=()=>{try{if($('remember').checked){storage?.setItem('hermes.remember','yes');save();}else{storage?.removeItem('hermes.remember');storage?.removeItem('jarvis.remember');storage?.removeItem(HISTORY_KEY);}}catch{notice('Browser storage is unavailable. History will stay in this tab only.');}};

const Recognition=window.SpeechRecognition||window.webkitSpeechRecognition;
const micSupported=!!Recognition&&window.isSecureContext;
if(!micSupported){$('mic').disabled=true;$('ptt').disabled=true;$('mic-help').textContent=window.isSecureContext?'Voice input unavailable · use keyboard dictation':'Open Jarvis over HTTPS for voice input';}
else $('mic-help').textContent='Tap mic to toggle · hold to talk';
function beginMic(){
 if(!micSupported)return;if(!apiKey && !oramaSession){openSettings();notice('Connect your workspace before using the microphone.');return;}
 stopAll();void unlockAudio();notice('');const turn=guard.next(),rec=new Recognition();recognition=rec;let transcript='';let failed=false;
 rec.lang=$('language').value;rec.interimResults=true;rec.continuous=true;
 rec.onresult=e=>{if(!guard.current(turn.id))return;transcript=Array.from(e.results).map(r=>r[0].transcript).join(' ').trim().slice(0,2000);$('message').value=transcript;};
 rec.onerror=e=>{if(!guard.current(turn.id))return;failed=true;notice(e.error==='not-allowed'?'Microphone permission was denied. Allow microphone access in browser settings, or type your message.':`Microphone unavailable (${e.error}). You can type instead.`);};
 rec.onend=()=>{clearTimeout(micTimer);if(!guard.current(turn.id))return;recognition=null;$('mic').setAttribute('aria-pressed','false');$('mic').setAttribute('aria-label','Start microphone');state('idle');if(transcript&&!failed)void send(transcript);};
 try{rec.start();state('listening');$('mic').setAttribute('aria-pressed','true');$('mic').setAttribute('aria-label','Finish microphone');micTimer=setTimeout(()=>stopMic(false),30000);}catch{recognition=null;state('idle');notice('The microphone could not start. Try again or type your message.');}
}
$('mic').onclick=()=>{if(recognition)stopMic(false);else beginMic();};
let held=false;
$('ptt').onpointerdown=e=>{if(e.button!==0)return;e.preventDefault();held=true;$('ptt').setPointerCapture(e.pointerId);beginMic();};
$('ptt').onpointerup=()=>{if(held){held=false;stopMic(false);}};
$('ptt').onpointercancel=()=>{held=false;stopAll();};
$('ptt').onkeydown=e=>{if((e.code==='Space'||e.code==='Enter')&&!e.repeat){e.preventDefault();held=true;beginMic();}};
$('ptt').onkeyup=e=>{if(held&&(e.code==='Space'||e.code==='Enter')){e.preventDefault();held=false;stopMic(false);}};

const serviceNames={orama:['◈','Orama'],gemini:['✦','Gemini'],openai:['❖','OpenAI / LLM'],ollama:['◉','Ollama'],plex:['›','Plex'],jellyfin:['△','Jellyfin'],nas:['▤','NAS']};
const statusNames={online:'Reachable',configured:'Configured',not_configured:'Not configured',unavailable:'Unavailable',unknown:'Not checked'};
function showStatuses(data={}){
 $('statuses').replaceChildren();for(const [id,[symbol,name]] of Object.entries(serviceNames)){
  const info=data[id]||{status:'unknown'},row=document.createElement('div');row.className='status-row';
  const icon=document.createElement('span');icon.className='status-symbol';icon.textContent=symbol;
  const title=document.createElement('b');title.textContent=name;
  const value=document.createElement('span');value.className='status-value';value.dataset.status=info.status;value.textContent=statusNames[info.status]||'Unknown';if(info.detail)row.title=info.detail;
  row.append(icon,title,value);$('statuses').append(row);
 }
}
showStatuses();
async function checkStatus(){try{const result=await jsonCall('/v1/status');showStatuses(result.services);$('status-time').textContent='Checked '+new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})+' · reachability, not device health';const m=result.models||{};const parts=[];if(m.gemini)parts.push('Gemini: '+m.gemini);if(m.openai)parts.push('OpenAI: '+m.openai);if(m.ollama)parts.push('Ollama: '+m.ollama);$('model-details').textContent=parts.join(' · ');try{const cfg=await(await fetch(BASE+'/v1/config')).json();if(cfg?.orama_url)oramaUrl=cfg.orama_url;if(cfg?.node_name&&$('node-tag'))$('node-tag').textContent=cfg.node_name;}catch{}}catch(e){showStatuses();notice(e.name==='AbortError'?'Connection checks timed out.':e.message);}}
async function recommendations(){const result=await jsonCall('/v1/recommendations');renderPicks(result.recommendations||[]);}
function renderPicks(items){
 $('recommendations').replaceChildren();if(!items.length){const empty=document.createElement('div');empty.className='empty-recommendations';empty.textContent='No picks yet. Configure Plex or Jellyfin and TMDB, then refresh your library.';$('recommendations').append(empty);return;}
 for(const item of items.slice(0,8)){
  const card=document.createElement('article');card.className='movie-card';const poster=document.createElement('div');poster.className='poster';poster.textContent='◈';
  if(typeof item.poster_path==='string'&&/^\/[A-Za-z0-9._-]+\.(jpg|png|webp)$/.test(item.poster_path)){
   const img=document.createElement('img');img.src='https://image.tmdb.org/t/p/w342'+item.poster_path;img.alt='';img.loading='lazy';img.referrerPolicy='no-referrer';poster.replaceChildren(img);
  }
  const badge=document.createElement('span');badge.className='badge';badge.textContent=(item.kind==='series'?'SERIES':'MOVIE')+(item.year?' · '+item.year:'');poster.append(badge);
  const info=document.createElement('div');info.className='movie-info';const title=document.createElement('h3');title.textContent=item.title;
  const reason=document.createElement('p');reason.textContent=item.reason;info.append(title,reason);
  if(oramaUrl){
   const req=document.createElement('a');req.className='request-btn';req.target='_blank';req.rel='noopener noreferrer';
   req.href=`${oramaUrl.replace(/\/$/,'')}/media/${item.kind}/${item.source_id}`;req.textContent='Request in Orama ↗';info.append(req);
  }
  card.append(poster,info);$('recommendations').append(card);
 }
}
async function loadVoices(){const data=await jsonCall('/v1/voices');const selected=$('local-voice').value;$('local-voice').replaceChildren(new Option('Match selected language','auto'));for(const v of data.local_voices||[]){if(v.language===$('language').value)$('local-voice').add(new Option(v.name+(v.available?'':' · not installed'),v.id));}if(Array.from($('local-voice').options).some(o=>o.value===selected))$('local-voice').value=selected;$('voice-availability').textContent=(data.local_voices||[]).filter(v=>v.available).length+' local voices installed. Missing voices need server setup.';}
function voiceSummary(){$('voice-summary').replaceChildren(document.createTextNode($('tts-provider').value==='piper'?'Piper · local':$('voice').value));const small=document.createElement('small');small.textContent=$('language').value+' · '+($('tts-provider').value==='auto'?'automatic fallback':$('tts-provider').value);$('voice-summary').append(small);}
for(const id of ['language','voice','tts-provider','local-voice'])$(id).onchange=()=>{stopAll();voiceSummary();if(id==='language'){$('local-voice').value='auto';if(apiKey||oramaSession)loadVoices().catch(e=>notice(e.message));}};
$('connect').onclick=async()=>{
 stopAll();apiKey=$('key').value.trim();$('key').value='';$('connect').disabled=true;notice('');
 try{await loadVoices();$('settings').close();$('connection-label').textContent='Workspace connected';$('connect-open').classList.add('connected');await Promise.all([checkStatus(),recommendations()]);}
 catch(e){notice(e.message);$('settings').close();}finally{$('connect').disabled=false;}
};
$('status-refresh').onclick=()=>void checkStatus();
$('refresh').onclick=async()=>{$('refresh').disabled=true;$('refresh').textContent='Finding your next favorite…';notice('');try{await jsonCall('/v1/refresh',{});await recommendations();}catch(e){notice(e.name==='AbortError'?'Refresh timed out. Existing picks are preserved.':e.message);}finally{$('refresh').disabled=false;$('refresh').textContent='Refresh picks ↻';}};
if(oramaSession){
  $('connection-label').textContent='Orama connected';
  $('connect-open').classList.add('connected');
  Promise.all([loadVoices(), checkStatus(), recommendations()]).catch(()=>{});
}
setInterval(()=>{if((apiKey||oramaSession)&&!document.hidden)void checkStatus();},60000);
