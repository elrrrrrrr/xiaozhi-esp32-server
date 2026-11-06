// Credentials stay in the local server's .env file.
export const MODELS=[{id:'qwen-flash',label:'Qwen Flash · 文本对话'}];
export function validToken(){return true;}
export function connectionURL(){
  const url=new URL('/ws',location.href);
  url.protocol=location.protocol==='https:'?'wss:':'ws:';
  return url;
}
export function safeMessage(message){return String(message||'').replace(/sk-[\w.-]{12,}/g,'[redacted]').slice(0,1200);}
