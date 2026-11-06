export const DEFAULT_VOICE='zh-CN-XiaoxiaoNeural';
export const VOICES=[
  {id:'zh-CN-XiaoxiaoNeural',name:'晓晓',gender:'女',description:'温暖亲切'},
  {id:'zh-CN-XiaoyiNeural',name:'晓伊',gender:'女',description:'明亮活泼'},
  {id:'zh-CN-YunxiNeural',name:'云希',gender:'男',description:'自然少年感'},
  {id:'zh-CN-YunjianNeural',name:'云健',gender:'男',description:'有力叙事'},
  {id:'zh-CN-YunxiaNeural',name:'云夏',gender:'男',description:'轻快年轻'},
  {id:'zh-CN-YunyangNeural',name:'云扬',gender:'男',description:'清晰稳重'},
].map(v=>({...v,group:'普通话',language:'中文'}));
