export const HISTORY_KEY='hermes.history.v2';
const cleanHistory=items=>Array.isArray(items)?items.filter(x=>x&&['user','assistant'].includes(x.role)&&typeof x.content==='string').slice(-20).map(x=>({role:x.role,content:x.content.slice(0,8000)})):[];
export function loadHistory(storage){try{return cleanHistory(JSON.parse(storage.getItem(HISTORY_KEY)||storage.getItem('jarvis.history.v2')||'[]'));}catch{return [];}}
export function saveHistory(storage,items){try{storage.setItem(HISTORY_KEY,JSON.stringify(cleanHistory(items)));return true;}catch{return false;}}
export class TurnGuard{
 constructor(){this.id=0;this.controller=null;}
 next(){this.controller?.abort();this.controller=new AbortController();return {id:++this.id,signal:this.controller.signal};}
 current(id){return id===this.id;}
 cancel(){this.controller?.abort();this.id++;}
}
export async function* readEvents(stream){
 if(!stream)throw Error('Streaming is unavailable in this browser.');
 const reader=stream.getReader(),decoder=new TextDecoder();let pending='';
 try{while(true){const {done,value}=await reader.read();pending+=decoder.decode(value||new Uint8Array(),{stream:!done});
  if(pending.length>24000000)throw Error('Response exceeded the safety limit.');
  let index;while((index=pending.indexOf('\n'))>=0){const line=pending.slice(0,index).trim();pending=pending.slice(index+1);if(line)yield JSON.parse(line);}
  if(done){if(pending.trim())yield JSON.parse(pending);break;}
 }}finally{await reader.cancel().catch(()=>{});reader.releaseLock();}
}
