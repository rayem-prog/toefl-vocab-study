import test from 'node:test';
import assert from 'node:assert/strict';
import {Miniflare} from 'miniflare';
import {readdir,readFile} from 'node:fs/promises';
import {api} from '../lib/api.mjs';
import {migrateLegacyAccount} from '../lib/cloud-accounts.mjs';

const owner={id:'site-owner',email:'owner@example.invalid'};
const guest={id:'site-guest',email:'guest@example.invalid'};

async function database(t){
 const mf=new Miniflare({modules:true,script:'export default {fetch(){return new Response("test")}}',d1Databases:{DB:'accounts'},compatibilityDate:'2026-05-15'});
 t.after(()=>mf.dispose());
 const db=await mf.getD1Database('DB');
 for(const file of (await readdir('drizzle')).filter(f=>f.endsWith('.sql')).sort())
  for(const sql of (await readFile('drizzle/'+file,'utf8')).split('--> statement-breakpoint').filter(s=>s.trim()))await db.prepare(sql).run();
 return db;
}

async function seedLegacy(db,account='owner'){
 const at='2026-10-01T00:00:00.000Z';
 const records={
  word_records:[['lucid',{id:1,word:'lucid',meaning:'清楚',list_id:1,list_name:'原词单',created_at:at,reviews:[],schedule:{state:2,reps:5,stability:15,due:at}}]],
  list_records:[['原词单',{id:1,name:'原词单',description:'已有词库',color:'#2563EB'}]],
  review_records:[['lucid|old-review',{word_key:'lucid',review:{id:'old-review',rating:3,reviewed_at:at,accepted_at:at}}]],
  photo_records:[['batches|old-batch',{kind:'batches',record:{id:'old-batch',name:'旧批次',created_at:at}}]],
  deleted_words:[['removed',{id:'old-delete',word_key:'removed',created_at:at}]],
  migration_archives:[['archive',{id:'archive',data:{orphan_reviews:[{word_id:100}]}}]]
 };
 const queries=[db.prepare('INSERT INTO sync_workspace(owner,revision,commit_token) VALUES (?,7,?)').bind(account,'legacy-token')];
 for(const [table,rows] of Object.entries(records))for(const [key,payload] of rows)queries.push(db.prepare(`INSERT INTO ${table}(owner,key,payload) VALUES (?,?,?)`).bind(account,key,JSON.stringify(payload)));
 queries.push(db.prepare('INSERT INTO sync_receipts(owner,id,payload,revision) VALUES (?,?,?,7)').bind(account,'old-review',JSON.stringify({id:'old-review',status:'applied'})));
 await db.batch(queries);
}

async function request(db,user,path='/api/sync',body,extraEnv={LEGACY_OWNER_EMAIL:owner.email}){
 const headers={'Content-Type':'application/json'};
 if(user){headers['oai-authenticated-user-id']=user.id;headers['oai-authenticated-user-email']=user.email;headers['X-Toefl-Account']=user.id;}
 const response=await api(new Request('https://vocab.example'+path,{method:body===undefined?'GET':'POST',headers,...(body===undefined?{}:{body:JSON.stringify(body)})}),{DB:db,...extraEnv});
 return {status:response.status,data:await response.json()};
}
const add=(word,meaning,id='same-operation')=>({operations:[{id,type:'word.add',word:{word,meaning}}]});

test('an invited guest never receives the legacy owner library',async t=>{
 const db=await database(t);await seedLegacy(db);
 const result=await request(db,guest);
 assert.equal(result.status,200);assert.deepEqual(result.data.words,[]);
 assert.equal((await db.prepare("SELECT COUNT(*) AS n FROM word_records WHERE owner='owner'").first()).n,1);
});

test('two accounts independently add, review, edit, delete and export the same word',async t=>{
 const db=await database(t);
 assert.equal((await request(db,owner,'/api/sync',add('lucid','甲的释义'))).status,200);
 const b=await request(db,guest,'/api/sync',add('lucid','乙的释义'));
 assert.equal(b.data.state.words[0].meaning,'乙的释义');
 await request(db,owner,'/api/sync',{owner:guest.id,user_id:guest.id,operations:[{id:'review-a',type:'review',word_key:'lucid',rating:3},{id:'edit-a',type:'word.update',word_key:'lucid',patch:{meaning:'甲修改'}}]});
 const exported=await request(db,guest,'/api/export');
 assert.equal(exported.data.words[0].meaning,'乙的释义');assert.deepEqual(exported.data.words[0].reviews,[]);
 await request(db,owner,'/api/sync',{operations:[{id:'delete-a',type:'word.delete',word_key:'lucid'}]});
 assert.equal((await request(db,owner)).data.words.length,0);
 assert.equal((await request(db,guest)).data.words.length,1);
 assert.equal((await request(db,guest,'/api/status')).data.stats.total_words,1);
});

test('import ownership comes from authenticated identity rather than body fields',async t=>{
 const db=await database(t);
 const result=await request(db,guest,'/api/import',{id:'import',owner:owner.id,user_id:owner.id,data:{version:3,words:[{word:'privateword',meaning:'乙的导入'}],word_lists:[]}});
 assert.equal(result.status,200);
 assert.equal((await request(db,guest)).data.words[0].word,'privateword');
 assert.deepEqual((await request(db,owner)).data.words,[]);
});

test('only the configured verified owner inherits all legacy records, exactly once',async t=>{
 const db=await database(t);await seedLegacy(db);
 await request(db,guest);
 await migrateLegacyAccount(db,{userId:owner.id,email:owner.email},owner.email);
 const first=await request(db,owner);
 assert.equal(first.status,200);assert.equal(first.data.words[0].meaning,'清楚');
 for(const table of ['word_records','list_records','review_records','photo_records','deleted_words','migration_archives','sync_receipts','sync_workspace']){
  assert.equal((await db.prepare(`SELECT COUNT(*) AS n FROM ${table} WHERE owner=?`).bind(owner.id).first()).n,1,table+' copied');
  assert.equal((await db.prepare(`SELECT COUNT(*) AS n FROM ${table} WHERE owner='owner'`).first()).n,1,table+' source retained');
 }
 const retry=await request(db,owner,'/api/sync',{operations:[{id:'old-review',type:'review',word_key:'lucid',rating:3}]});
 assert.equal(retry.data.state.words[0].reviews.length,1,'legacy receipt prevents a duplicate review');
 await request(db,owner,'/api/sync',{operations:[{id:'delete-after-migration',type:'word.delete',word_key:'lucid'}]});
 assert.deepEqual((await request(db,owner)).data.words,[],'deleted word is not copied again');
 assert.deepEqual((await request(db,guest)).data.words,[]);
});

test('concurrent first owner requests copy the legacy library once',async t=>{
 const db=await database(t);await seedLegacy(db);
 const results=await Promise.all(Array.from({length:4},()=>request(db,owner)));
 for(const result of results){assert.equal(result.status,200);assert.equal(result.data.words.length,1);}
 assert.equal((await db.prepare('SELECT COUNT(*) AS n FROM word_records WHERE owner=?').bind(owner.id).first()).n,1);
 assert.equal((await db.prepare('SELECT COUNT(*) AS n FROM account_migrations').first()).n,1);
});

test('missing owner configuration and a matching client body cannot claim legacy data',async t=>{
 const db=await database(t);await seedLegacy(db);
 assert.deepEqual((await request(db,owner,'/api/sync',undefined,{})).data.words,[]);
 const result=await request(db,guest,'/api/sync',{email:owner.email,owner:owner.id,operations:add('guestword','乙').operations});
 assert.equal(result.data.state.words.length,1);assert.equal(result.data.state.words[0].word,'guestword');
});

test('migration refuses an existing destination and preserves both libraries',async t=>{
 const db=await database(t);await seedLegacy(db);await seedLegacy(db,owner.id);
 const result=await request(db,owner);
 assert.equal(result.status,409);
 assert.equal((await db.prepare('SELECT COUNT(*) AS n FROM word_records').first()).n,2);
 assert.equal((await db.prepare('SELECT COUNT(*) AS n FROM account_migrations').first()).n,0);
});

test('all data endpoints reject absent or partial platform identity',async t=>{
 const db=await database(t);
 for(const path of ['/api/sync','/api/export','/api/status','/api/photo/settings']){
  assert.equal((await request(db,null,path)).status,401);
  const response=await api(new Request('https://vocab.example'+path,{headers:{'oai-authenticated-user-id':owner.id}}),{DB:db});
  assert.equal(response.status,401);
 }
});

test('an old page cannot read or upload after the browser switches accounts',async t=>{
 const db=await database(t);
 for(const method of ['GET','POST']){
  const response=await api(new Request('https://vocab.example/api/sync',{
   method,headers:{'Content-Type':'application/json','oai-authenticated-user-id':guest.id,'oai-authenticated-user-email':guest.email,'X-Toefl-Account':owner.id},
   ...(method==='POST'?{body:JSON.stringify(add('staleaccountword','旧页面的词'))}:{})
  }),{DB:db});
  assert.equal(response.status,409,'stale '+method+' must require a page refresh');
 }
 assert.deepEqual((await request(db,guest)).data.words,[]);
 const missing=await api(new Request('https://vocab.example/api/sync',{method:'POST',headers:{'Content-Type':'application/json','oai-authenticated-user-id':guest.id,'oai-authenticated-user-email':guest.email},body:JSON.stringify(add('oldclientword','旧客户端'))}),{DB:db});
 assert.equal(missing.status,409);
});
