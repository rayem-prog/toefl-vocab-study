import {readState,commitOperations} from './cloud-store.mjs';import {publicState} from './cloud-core.mjs';import {publicConfig,recognize,lookup,boundedText} from './cloud-vision.mjs';
const json=(v,status=200)=>Response.json(v,{status,headers:{'Cache-Control':'no-store'}});
export async function api(request,env){const path=new URL(request.url).pathname;const bootstrap=['/api/import','/api/photo/recognize'].includes(path)&&env.BOOTSTRAP_TOKEN&&request.headers.get('Authorization')==='Bearer '+env.BOOTSTRAP_TOKEN;
 if(!bootstrap&&!(request.headers.get('oai-authenticated-user-id')&&request.headers.get('oai-authenticated-user-email')))return json({error:'请先登录同一ChatGPT账号'},401);
 if(request.method==='POST'){const origin=request.headers.get('Origin');if(origin&&origin!==new URL(request.url).origin)return json({error:'请从本网站提交操作'},403)}
 try{
 if(path==='/api/photo/settings'){if(request.method!=='GET')return json({error:'云端识别配置由维护者安全管理，无需在设备上填写密钥'},403);return json(publicConfig(env))}
 if(!env.DB)return json({error:'云端数据库暂不可用，操作已保留，请稍后重试'},503);
 if(request.method==='GET'){
 if(path==='/api/status'){const s=await readState(env.DB);return json({status:'online',sync_version:4,capabilities:['photo_state','photo_recognition','idempotent_operations'],revision:s.revision,stats:publicState(s).summary})}
 if(path==='/api/sync'||path==='/api/export'){const s=publicState(await readState(env.DB));if(path==='/api/export')return new Response(JSON.stringify(s,null,2),{headers:{'Content-Type':'application/json;charset=utf-8','Content-Disposition':'attachment; filename="toefl-cloud-backup.json"','Cache-Control':'no-store'}});return json(s)}
 }
 if(request.method==='POST'){
 if(!request.headers.get('Content-Type')?.includes('application/json'))return json({error:'请求需为JSON'},415);
 const limit=path==='/api/photo/recognize'?6*1024*1024:8*1024*1024;
 if(Number(request.headers.get('Content-Length'))>limit)return json({error:'请求过大，请拆分或裁剪'},413);
 let body;try{body=JSON.parse(await boundedText(request,limit))}catch{return json({error:'请求JSON无效或过大'},400)}
 if(path==='/api/photo/recognize')return json(await recognize(env,body.images));
 if(path==='/api/photo/lookup')return json(await lookup(String(body.word||'').trim()));
 if(path==='/api/sync'){if(!Array.isArray(body.operations))return json({error:'旧版全量写入已停用，请使用云端网页或JSON迁移入口'},409);return json(await commitOperations(env.DB,body.operations))}
 if(path==='/api/import'){if(!body.id||!body.data)return json({error:'导入需要唯一操作ID和完整备份'},400);const result=await commitOperations(env.DB,[{id:body.id,type:'import.merge',data:body.data}]);return json({...result,report:result.state.summary})}
 }
 return json({error:'接口不存在'},404);
 }catch(e){if(e.status===503)return json({error:e.message},503);if(path.startsWith('/api/photo/'))return json({error:e.message.includes('识别')||e.message.includes('图片')||e.message.includes('词典')||e.message.includes('主词')?e.message:'识别服务暂不可用，请重试'},502);const messages=['操作','同步','单词','词条','时间','备份','照片','会话','作答','复习','评分'];return json({error:messages.some(s=>e.message.includes(s))?e.message:'云端保存失败，操作已保留，请重试'},400)}
}
