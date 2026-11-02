import {readState,commitOperations} from './cloud-store.mjs';import {publicState} from './cloud-core.mjs';import {publicConfig,recognize,lookup,boundedText} from './cloud-vision.mjs';
import {migrateLegacyAccount} from './cloud-accounts.mjs';
const json=(v,status=200)=>Response.json(v,{status,headers:{'Cache-Control':'no-store'}});
export async function api(request,env){const path=new URL(request.url).pathname;
 const user={userId:request.headers.get('oai-authenticated-user-id'),email:request.headers.get('oai-authenticated-user-email')};
 if(!(user.userId&&user.email))return json({error:'请先登录你的ChatGPT账号'},401);
 const pageAccount=request.headers.get('X-Toefl-Account');
 if((pageAccount&&pageAccount!==user.userId)||(request.method==='POST'&&!pageAccount))return json({error:'登录账号已变化或页面版本较旧，请刷新页面后同步；待上传操作仍保留在原账号'},409);
 if(request.method==='POST'){const origin=request.headers.get('Origin');if(origin&&origin!==new URL(request.url).origin)return json({error:'请从本网站提交操作'},403)}
 try{
 if(path==='/api/photo/settings'){if(request.method!=='GET')return json({error:'云端识别配置由维护者安全管理，无需在设备上填写密钥'},403);return json(publicConfig(env))}
 if(!env.DB)return json({error:'云端数据库暂不可用，操作已保留，请稍后重试'},503);
 await migrateLegacyAccount(env.DB,user,env.LEGACY_OWNER_EMAIL);
 if(request.method==='GET'){
 if(path==='/api/status'){const s=await readState(env.DB,user.userId);return json({status:'online',sync_version:4,capabilities:['photo_state','photo_recognition','idempotent_operations','independent_account_libraries'],revision:s.revision,stats:publicState(s).summary})}
 if(path==='/api/sync'||path==='/api/export'){const s=publicState(await readState(env.DB,user.userId));if(path==='/api/export')return new Response(JSON.stringify(s,null,2),{headers:{'Content-Type':'application/json;charset=utf-8','Content-Disposition':'attachment; filename="toefl-cloud-backup.json"','Cache-Control':'no-store'}});return json(s)}
 }
 if(request.method==='POST'){
 if(!request.headers.get('Content-Type')?.includes('application/json'))return json({error:'请求需为JSON'},415);
 const limit=path==='/api/photo/recognize'?6*1024*1024:8*1024*1024;
 if(Number(request.headers.get('Content-Length'))>limit)return json({error:'请求过大，请拆分或裁剪'},413);
 let body;try{body=JSON.parse(await boundedText(request,limit))}catch{return json({error:'请求JSON无效或过大'},400)}
 if(path==='/api/photo/recognize')return json(await recognize(env,body.images));
 if(path==='/api/photo/lookup')return json(await lookup(String(body.word||'').trim()));
 if(path==='/api/sync'){if(!Array.isArray(body.operations))return json({error:'旧版全量写入已停用，请使用云端网页或JSON迁移入口'},409);return json(await commitOperations(env.DB,user.userId,body.operations))}
 if(path==='/api/import'){if(!body.id||!body.data)return json({error:'导入需要唯一操作ID和完整备份'},400);const result=await commitOperations(env.DB,user.userId,[{id:body.id,type:'import.merge',data:body.data}]);return json({...result,report:result.state.summary})}
 }
 return json({error:'接口不存在'},404);
 }catch(e){if(e.status===503||e.status===409)return json({error:e.message},e.status);if(path.startsWith('/api/photo/'))return json({error:e.message.includes('识别')||e.message.includes('图片')||e.message.includes('词典')||e.message.includes('主词')?e.message:'识别服务暂不可用，请重试'},502);const messages=['操作','同步','单词','词条','时间','备份','照片','会话','作答','复习','评分'];return json({error:messages.some(s=>e.message.includes(s))?e.message:'云端保存失败，操作已保留，请重试'},400)}
}
