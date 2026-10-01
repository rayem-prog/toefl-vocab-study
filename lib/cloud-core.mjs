import './photo_core.js';
const PC=globalThis.PhotoCore;
export const key=s=>String(s||'').trim().toLowerCase();
const clone=x=>JSON.parse(JSON.stringify(x));
export function utc(value){if(!value)return '';const s=String(value);const zoned=/Z$|[+-]\d\d:\d\d$/.test(s)?s:s.replace(' ','T')+'+08:00';const t=Date.parse(zoned);if(!Number.isFinite(t))throw Error('时间格式无效');return new Date(t).toISOString()}
export function emptyState(){return {format:'toefl_vocab_sync',version:4,revision:0,word_lists:[],words:[],photo_state:{batches:[],items:[],sessions:[],answers:[],review_actions:[]},deleted_words:[],migration_archive:[],receipts:{}}}
const fields=['phonetic','meaning','example','note','category','list_name'];
function list(s,name){name=String(name||'默认词单').trim().slice(0,150);let l=s.word_lists.find(l=>l.name===name);if(!l){l={id:Math.max(0,...s.word_lists.map(l=>Number(l.id)||0))+1,name,description:'',color:'#2563EB'};s.word_lists.push(l)}return l}
function addWord(s,w,restore=false){const k=key(w.word);if(!/^[a-z]+(?:[ '’-][a-z]+)*$/.test(k)||k.length>100)throw Error('英文单词格式无效');if(s.deleted_words.some(t=>t.word_key===k)&&!restore)return null;const tomb=s.deleted_words.find(t=>t.word_key===k);const recovered=restore&&tomb?.word?clone(tomb.word):null;if(restore)s.deleted_words=s.deleted_words.filter(t=>t.word_key!==k);if(recovered)w={...w,schedule:recovered.schedule,reviews:recovered.reviews,last_review_effective_at:recovered.last_review_effective_at};let found=s.words.find(x=>key(x.word)===k);if(found)return found;const l=list(s,w.list_name);found={id:Math.max(0,...s.words.map(w=>Number(w.id)||0))+1,word:String(w.word).trim(),list_id:l.id,list_name:l.name,created_at:utc(w.created_at||new Date().toISOString()),photo_pending:!!w.photo_pending,reviews:[],schedule:w.schedule?clone(w.schedule):null};for(const f of fields)if(f!=='list_name')found[f]=String(w[f]||'').slice(0,10000);if(found.schedule?.due)found.schedule.due=utc(found.schedule.due);if(recovered){found.reviews=recovered.reviews||[];if(recovered.last_review_effective_at)found.last_review_effective_at=recovered.last_review_effective_at;}s.words.push(found);return found}
export function reviewSchedule(input,rating,context='review',now=Date.now()){
 const s={reps:0,lapses:0,state:0,stability:0,...clone(input||{})},steps=[10,60,240],intervals=[1,2,4,7,15,30,60];let minutes=0,days=0;const prot=s.state===2&&(s.stability>=7||s.reps>=4),learning=s.state===0||(s.state===1&&s.reps<3);
 if(context==='again'){s.lapses++;if(prot){s.stability=Math.max(3.5,s.stability*.7);s.state=2;s.reps++}else{s.reps=0;s.state=1;s.stability=.1}minutes=-1/60;}
 else if(rating===1){s.lapses++;if(prot&&!learning){s.reps++;s.stability=Math.max(3.5,s.stability*.7);s.state=2;minutes=60}else{s.reps=0;s.state=context==='review'&&!learning?3:1;s.stability=context==='review'&&!learning?.5:.1;minutes=10}}
 else if(learning){s.state=1;if(rating===2)minutes=steps[Math.min(s.reps,2)];else{s.reps++;if(s.reps>=3){s.state=2;s.stability=1;days=1}else minutes=steps[s.reps]}}
 else{s.reps++;days=intervals[Math.min(s.reps,6)];if(rating===2)days=Math.max(1,Math.floor(days*.7));if(rating===4)days=Math.ceil(days*1.5);s.stability=days;s.state=2}
 s.due=new Date(now+minutes*60000+days*86400000).toISOString();return s;
}
function validateRecord(kind,r){if(!r||typeof r.id!=='string'||!r.id||r.id.length>150)throw Error('照片记录缺少ID');if(['items','answers','review_actions'].includes(kind)&&!key(r.word_key))throw Error('缺少单词引用');if(kind==='sessions'&&(!['flash','spell'].includes(r.mode)||!Array.isArray(r.queue)||r.queue.length>5000))throw Error('测试会话格式无效');if(kind==='answers'&&!['known','vague','unknown','wrong','guessed','skipped'].includes(r.result))throw Error('作答结果无效');if(JSON.stringify(r).length>200000)throw Error('照片记录过大')}
export function applyOperations(input,ops,now=Date.now()){
 const s=clone(input),results=[];s.receipts||={};PC.ensure(s);if(!Array.isArray(ops)||ops.length>100)throw Error('每批最多100次操作');let order=0;
 for(const op of ops){if(!op||typeof op.id!=='string'||!op.id||op.id.length>150)throw Error('操作缺少唯一编号');if(s.receipts[op.id]){results.push(s.receipts[op.id]);continue}let status='applied';const at=new Date(now+order++).toISOString();
 switch(op.type){
 case 'word.add':if(!addWord(s,op.word,op.restore===true))status='deleted';break;
 case 'word.update':{const w=s.words.find(w=>key(w.word)===key(op.word_key));if(!w){status='deleted';break}for(const f of fields)if(f in (op.patch||{}))w[f]=String(op.patch[f]).slice(0,10000);w.list_id=list(s,w.list_name).id;break}
 case 'word.delete':{const k=key(op.word_key),deleted=s.words.find(w=>key(w.word)===k)||s.deleted_words.find(t=>t.word_key===k)?.word;s.words=s.words.filter(w=>key(w.word)!==k);s.deleted_words=s.deleted_words.filter(t=>t.word_key!==k);s.deleted_words.push({word_key:k,created_at:at,id:op.id,...(deleted?{word:deleted}:{})});break}
 case 'list.put':{const l=list(s,op.list?.name);for(const f of ['description','color'])if(f in op.list)l[f]=String(op.list[f]).slice(0,1000);break}
 case 'list.delete':{const l=s.word_lists.find(l=>l.name===op.name);if(l){s.word_lists=s.word_lists.filter(x=>x!==l);for(const w of s.words.filter(w=>w.list_name===l.name)){const fallback=list(s,'默认词单');w.list_name=fallback.name;w.list_id=fallback.id}}break}
 case 'review':{if(![1,2,3,4].includes(op.rating)||!['review','quiz','again'].includes(op.context||'review'))throw Error('复习评分无效');const w=s.words.find(w=>key(w.word)===key(op.word_key));if(!w){status='deleted';break}if(w.reviews.some(r=>r.id===op.id)){status='duplicate';break}const t=Math.max(now+order,Date.parse(w.last_review_effective_at||'')||0);w.schedule=reviewSchedule(w.schedule,op.rating,op.context||'review',t);w.last_review_effective_at=new Date(t).toISOString();w.reviews.push({id:op.id,rating:op.rating,accepted_at:new Date(t).toISOString(),reviewed_at:op.reviewed_at?utc(op.reviewed_at):at});w.photo_pending=false;break}
 case 'photo.record':{const k=op.kind;if(!['batches','items','sessions','answers','review_actions'].includes(k))throw Error('未知照片记录类型');validateRecord(k,op.record);const r=clone(op.record);if(s.photo_state[k].some(x=>x.id===r.id))break;
  if(r.word_key)r.word_key=key(r.word_key);
  if(k==='answers'){const session=s.photo_state.sessions.find(x=>x.id===r.session_id);if(!session||!session.queue.includes(r.word_key))throw Error('单词不属于测试会话');const old=PC.answers(s,r.session_id).get(r.word_key);if(old&&(!op.correction||op.supersedes!==old.id)){status='conflict';break}if(!old&&op.correction){status='conflict';break}}
  const latest=Math.max(0,...Object.values(s.photo_state).flat().map(r=>Date.parse(r.created_at)||0));r.created_at=new Date(Math.max(now+order,latest+1)).toISOString();s.photo_state[k].push(r);PC.reconcile(s);break}
 case 'import.merge':{const merged=mergeImport(s,op.data);Object.assign(s,merged);break}
 default:throw Error('不支持的同步操作');}
 if(status==='conflict'&&!s.migration_archive.some(a=>a.id==='conflict-'+op.id))s.migration_archive.push({id:'conflict-'+op.id,kind:'conflicting_answer',operation:clone(op)});const result={id:op.id,status};s.receipts[op.id]=result;results.push(result);
 }s.revision=(input.revision||0)+1;return {state:s,results};
}
export function mergeImport(input,data){
 if(!data||!Array.isArray(data.words)||![2,3,4].includes(data.version||2))throw Error('仅支持完整version 2、3、4 JSON备份');const s=clone(input);PC.ensure(s);
 for(const l of data.word_lists||[]){const target=list(s,l.name);if(!target.description)target.description=String(l.description||'');if(l.color)target.color=String(l.color)}
 for(const row of data.words){let w=s.words.find(w=>key(w.word)===key(row.word)),existed=!!w;if(s.deleted_words.some(t=>t.word_key===key(row.word)))continue;w||=addWord(s,row);if(!w)continue;for(const f of fields)if(!w[f]&&row[f])w[f]=String(row[f]);
  const oldLogs=w.reviews.length,lastBefore=Math.max(0,...w.reviews.map(r=>Date.parse(r.reviewed_at)||0));const incoming=(row.reviews||[]).map(r=>({...r,reviewed_at:utc(r.reviewed_at)}));const ids=new Set(w.reviews.map(r=>r.id||`${r.rating}|${r.reviewed_at}`));let added=0;for(const r of incoming){const identity=r.id||`${r.rating}|${r.reviewed_at}`;if(!ids.has(identity)){w.reviews.push(r);ids.add(identity);added++}}
  // Legacy import is a baseline, never replay historical ratings. A newer backup may
  // replace a baseline only before cloud reviews and with strictly later history.
  if(existed&&!w.last_review_effective_at&&row.schedule){const last=rs=>Math.max(0,...rs.map(r=>Date.parse(r.reviewed_at)||0));if(!w.schedule||(!oldLogs&&added)||last(incoming)>lastBefore){w.schedule=clone(row.schedule);if(w.schedule.due)w.schedule.due=utc(w.schedule.due)}}
 }
 for(const t of data.deleted_words||[]){const k=key(t.word_key||t);if(!k)continue;s.words=s.words.filter(w=>key(w.word)!==k);if(!s.deleted_words.some(x=>x.word_key===k))s.deleted_words.push({word_key:k,created_at:utc(t.created_at||new Date().toISOString())})}
 for(const kind of Object.keys(s.photo_state)){const seen=new Set(s.photo_state[kind].map(r=>r.id));for(const raw of data.photo_state?.[kind]||[]){validateRecord(kind,raw);if(!seen.has(raw.id)){const r=clone(raw);r.created_at=utc(r.created_at||new Date().toISOString());if(r.word_key)r.word_key=key(r.word_key);s.photo_state[kind].push(r);seen.add(r.id)}}}
 const archive=new Map((s.migration_archive||[]).map(r=>[r.id,r]));for(const r of data.migration_archive||[])if(r?.id&&!archive.has(r.id))archive.set(r.id,clone(r));s.migration_archive=[...archive.values()];PC.reconcile(s);return s;
}
export function publicState(s){const out=clone(s);delete out.receipts;out.summary={total_words:s.words.length,total_lists:s.word_lists.length,total_review_logs:s.words.reduce((n,w)=>n+w.reviews.length,0),archived_records:s.migration_archive.length};return out}
