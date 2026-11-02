import {emptyState,applyOperations,publicState} from './cloud-core.mjs';
const tables=['word_records','list_records','review_records','photo_records','deleted_words','migration_archives'];
function requireAccount(owner){if(typeof owner!=='string'||!owner.trim()||owner==='owner')throw Error('同步账号无效');}
export async function readState(db,owner,ids=[]){
 requireAccount(owner);
 await db.prepare('INSERT OR IGNORE INTO sync_workspace(owner,revision,commit_token) VALUES (?,0,\'\')').bind(owner).run();
 const queries=[db.prepare('SELECT revision FROM sync_workspace WHERE owner=?').bind(owner),...tables.map(t=>db.prepare(`SELECT key,payload FROM ${t} WHERE owner=? ORDER BY key`).bind(owner))];
 if(ids.length)queries.push(db.prepare(`SELECT id,payload FROM sync_receipts WHERE owner=? AND id IN (${ids.map(()=>'?').join(',')})`).bind(owner,...ids));
 const results=await db.batch(queries),s=emptyState();s.revision=results[0].results[0].revision;
 s.words=results[1].results.map(r=>JSON.parse(r.payload));s.word_lists=results[2].results.map(r=>JSON.parse(r.payload));
 for(const r of results[3].results){const v=JSON.parse(r.payload),w=s.words.find(w=>w.word.trim().toLowerCase()===v.word_key);if(w)w.reviews.push(v.review)}
 for(const r of results[4].results){const v=JSON.parse(r.payload);s.photo_state[v.kind].push(v.record)}
 s.deleted_words=results[5].results.map(r=>JSON.parse(r.payload));s.migration_archive=results[6].results.map(r=>JSON.parse(r.payload));
 for(const row of results[7]?.results||[])s.receipts[row.id]=JSON.parse(row.payload);
 for(const w of s.words)w.reviews.sort((a,b)=>a.reviewed_at.localeCompare(b.reviewed_at)||String(a.id||'').localeCompare(String(b.id||'')));
 for(const rows of Object.values(s.photo_state))rows.sort((a,b)=>a.created_at.localeCompare(b.created_at)||a.id.localeCompare(b.id));
 return s;
}
function documents(s){const all={word_records:new Map(),list_records:new Map(),review_records:new Map(),photo_records:new Map(),deleted_words:new Map(),migration_archives:new Map()};
 for(const w of s.words){const k=w.word.trim().toLowerCase(),{reviews,...record}=w;all.word_records.set(k,JSON.stringify({...record,reviews:[]}));for(const r of reviews)all.review_records.set(k+'|'+(r.id||r.rating+'|'+r.reviewed_at),JSON.stringify({word_key:k,review:r}))}
 for(const l of s.word_lists)all.list_records.set(l.name,JSON.stringify(l));for(const [kind,rows] of Object.entries(s.photo_state))for(const r of rows)all.photo_records.set(kind+'|'+r.id,JSON.stringify({kind,record:r}));for(const t of s.deleted_words)all.deleted_words.set(t.word_key,JSON.stringify(t));for(const a of s.migration_archive)all.migration_archives.set(a.id,JSON.stringify(a));return all;
}
export async function commitOperations(db,owner,ops){
 requireAccount(owner);
 if(!Array.isArray(ops)||!ops.length||ops.length>100||ops.some(o=>!o||typeof o.id!=='string'))throw Error('需提供1至100个带ID的操作');if(new Set(ops.map(o=>o.id)).size!==ops.length)throw Error('同批操作ID不得重复');
 for(let attempt=0;attempt<8;attempt++){
  const before=await readState(db,owner,ops.map(o=>o.id));const fresh=ops.filter(o=>!before.receipts[o.id]);if(!fresh.length)return {state:publicState(before),results:ops.map(o=>before.receipts[o.id])};
  const {state,results}=applyOperations(before,ops),token=crypto.randomUUID(),a=documents(before),b=documents(state);
  const guard='EXISTS (SELECT 1 FROM sync_workspace WHERE owner=? AND commit_token=?)';
  const sql=[db.prepare('UPDATE sync_workspace SET revision=?,commit_token=? WHERE owner=? AND revision=?').bind(state.revision,token,owner,before.revision)];
  for(const table of tables){for(const k of a[table].keys())if(!b[table].has(k))sql.push(db.prepare(`DELETE FROM ${table} WHERE owner=? AND key=? AND ${guard}`).bind(owner,k,owner,token));for(const [k,payload] of b[table])if(a[table].get(k)!==payload)sql.push(db.prepare(`INSERT INTO ${table}(owner,key,payload) SELECT ?,?,? WHERE ${guard} ON CONFLICT(owner,key) DO UPDATE SET payload=excluded.payload`).bind(owner,k,payload,owner,token))}
  for(const r of results)if(!before.receipts[r.id])sql.push(db.prepare(`INSERT OR IGNORE INTO sync_receipts(owner,id,payload,revision) SELECT ?,?,?,? WHERE ${guard}`).bind(owner,r.id,JSON.stringify(r),state.revision,owner,token));
  const res=await db.batch(sql);if(res[0].meta.changes===1)return {state:publicState(state),results};
 }const error=Error('同步繁忙，操作已保留，请稍后重试');error.status=503;throw error;
}
