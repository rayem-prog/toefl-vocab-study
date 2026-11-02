import test from 'node:test';import assert from 'node:assert/strict';import {createRequire} from 'node:module';import {createServer} from 'node:http';import {readFile,readdir} from 'node:fs/promises';import {Miniflare} from 'miniflare';import {api} from '../lib/api.mjs';import {commitOperations} from '../lib/cloud-store.mjs';
const {chromium}=createRequire(import.meta.url)('/Users/zhaoruiyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');let mf,db,server,browser,url;
test.before(async()=>{
 mf=new Miniflare({modules:true,script:'export default {fetch(){return new Response("test")}}',d1Databases:{DB:'browser-test'},compatibilityDate:'2026-05-15'});
 db=await mf.getD1Database('DB');
 for(const f of (await readdir('drizzle')).filter(f=>f.endsWith('.sql')).sort())for(const q of (await readFile('drizzle/'+f,'utf8')).split('--> statement-breakpoint').filter(q=>q.trim()))await db.prepare(q).run();
 server=createServer(async(req,res)=>{
  try{
   const parsed=new URL(req.url,'http://127.0.0.1');
   const cookie=req.headers.cookie?.split(';').map(c=>c.trim()).find(c=>c.startsWith('test_session='))?.split('=')[1];
   const chosen=parsed.searchParams.get('account')||cookie||'1';
   const account=chosen==='guest-user'?'guest-user':'test-user';
   const email=account==='test-user'?'test@example.invalid':'guest@example.invalid';
   if(!req.url.startsWith('/api/')){
    const html=await readFile('lib/vocab.html','utf8');
    res.setHeader('Content-Type','text/html');
    res.setHeader('Set-Cookie','test_session='+chosen+'; Path=/');
    res.end(html.replace('<head>','<head><script>window.CLOUD_ACCOUNT='+JSON.stringify({userId:account,email})+';</script>'));
    return;
   }
   const chunks=[];for await(const chunk of req)chunks.push(chunk);
   const headers=new Headers(req.headers);
   for(const name of [...headers.keys()])if(name.startsWith('oai-authenticated-user-'))headers.delete(name);
   if(cookie){headers.set('oai-authenticated-user-id',account);headers.set('oai-authenticated-user-email',email);}
   const request=new Request(url+req.url,{method:req.method,headers,...(req.method==='POST'?{body:Buffer.concat(chunks)}:{})});
   const response=await api(request,{DB:db});
   res.writeHead(response.status,Object.fromEntries(response.headers));res.end(Buffer.from(await response.arrayBuffer()));
  }catch(e){res.statusCode=500;res.end('test server error');}
 });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 url='http://127.0.0.1:'+server.address().port;
 browser=await chromium.launch({headless:true});
});
test.after(async()=>{await browser?.close();if(server)await new Promise(r=>server.close(r));await mf?.dispose()});
async function device(account){const ctx=await browser.newContext();const p=await ctx.newPage();await p.goto(account?url+'?account='+account:url);await p.waitForFunction(()=>typeof PhotoCore!=='undefined');return {ctx,p}}
async function ready(p){assert.equal(await p.evaluate(()=>typeof CloudSync),'object','云端浏览器同步尚未实现');await p.waitForFunction(()=>CloudSync.ready);await p.evaluate(()=>CloudSync.flush())}
async function add(p,word){await p.evaluate(()=>switchTab('add'));await p.locator('#add-word-input').fill(word);await p.locator('#add-meaning-input').fill('测试释义');await p.locator('[onclick="saveNewWordAction()"]').click();await p.evaluate(()=>CloudSync.flush())}
test('two browsers share additions, edits, soft deletion, and reload without resurrection',async()=>{const a=await device(),b=await device();try{await ready(a.p);await ready(b.p);await add(a.p,'cloudtest');await b.p.evaluate(()=>CloudSync.flush());assert.equal(await b.p.evaluate(()=>appData.words.some(w=>w.word==='cloudtest')),true);await b.p.evaluate(()=>{appData.words.find(w=>w.word==='cloudtest').meaning='更新';saveData()});await b.p.evaluate(()=>CloudSync.flush());await a.p.evaluate(()=>CloudSync.flush());assert.equal(await a.p.evaluate(()=>appData.words.find(w=>w.word==='cloudtest').meaning),'更新');await a.p.evaluate(()=>{appData.words=appData.words.filter(w=>w.word!=='cloudtest');saveData()});await a.p.evaluate(()=>CloudSync.flush());await b.p.reload();await ready(b.p);assert.equal(await b.p.evaluate(()=>appData.words.some(w=>w.word==='cloudtest')),false)}finally{await a.ctx.close();await b.ctx.close()}});
test('offline outbox survives restart and concurrent reviews both count',async()=>{await commitOperations(db,'test-user',[{id:'seed-review',type:'word.add',word:{word:'lucid',meaning:'清楚',schedule:{state:2,reps:5,stability:15}}}]);const a=await device(),b=await device();try{await ready(a.p);await ready(b.p);await a.ctx.setOffline(true);await a.p.evaluate(()=>{reviewQueue=[appData.words.find(w=>w.word==='lucid')];reviewIndex=0;reviewRevealed=true;rateReviewCard(3)});assert.equal(await a.p.evaluate(()=>CloudSync.outbox.length),1);await b.p.evaluate(()=>{reviewQueue=[appData.words.find(w=>w.word==='lucid')];reviewIndex=0;reviewRevealed=true;rateReviewCard(3)});await b.p.evaluate(()=>CloudSync.flush());await a.ctx.setOffline(false);await a.p.reload();await ready(a.p);await b.p.evaluate(()=>CloudSync.flush());assert.equal(await a.p.evaluate(()=>appData.words.find(w=>w.word==='lucid').reviews.length),2);assert.equal(await b.p.evaluate(()=>appData.words.find(w=>w.word==='lucid').schedule.reps),7);assert.equal(await a.p.evaluate(()=>CloudSync.outbox.length),0)}finally{await a.ctx.close();await b.ctx.close()}});
test('single-word photo session resumes on another device and initial test protects progress',async()=>{const a=await device(),b=await device();try{await ready(a.p);await ready(b.p);const ids=await a.p.evaluate(()=>{const batch=PhotoCore.createBatch(appData,'测试批次',[{word:'lucid',meaning:'清楚',certainty:'high',confirmed:true}]);const s=PhotoCore.startSession(appData,batch.id);saveData();return {batch:batch.id,session:s.id}});await a.p.evaluate(()=>CloudSync.flush());await b.p.evaluate(()=>CloudSync.flush());assert.equal(await b.p.evaluate(id=>PhotoCore.current(appData,id),ids.session),'lucid');await b.p.evaluate(id=>{PhotoCore.answer(appData,id,'lucid','unknown');saveData()},ids.session);await b.p.evaluate(()=>CloudSync.flush());await a.p.reload();await ready(a.p);assert.equal(await a.p.evaluate(id=>PhotoCore.current(appData,id),ids.session),null);assert.equal(await a.p.evaluate(id=>PhotoCore.summary(appData,id).pending.length,ids.batch),1);const before=await a.p.evaluate(()=>appData.words.find(w=>w.word==='lucid').schedule);await a.p.evaluate(()=>{PhotoCore.queueReview(appData,['lucid'],'queue-test');saveData()});await a.p.evaluate(()=>CloudSync.flush());const after=await a.p.evaluate(()=>appData.words.find(w=>w.word==='lucid').schedule);assert.equal(after.stability,before.stability);assert.equal(after.reps,before.reps);assert.equal(await a.p.evaluate(()=>appData.words.find(w=>w.word==='lucid').reviews.length),2)}finally{await a.ctx.close();await b.ctx.close()}});
test('unauthenticated and old full-snapshot writes are rejected',async()=>{assert.equal((await fetch(url+'/api/sync')).status,401);const r=await fetch(url+'/api/sync',{method:'POST',headers:{Cookie:'test_session=1','Content-Type':'application/json'},body:JSON.stringify({version:3,words:[]})});assert.equal(r.status,409)});
test('offline new-word and review operations retain separate schedule snapshots',async()=>{const a=await device();try{await ready(a.p);await a.ctx.setOffline(true);await a.p.evaluate(()=>{appData.words.push({id:9999,word:'offlinefresh',meaning:'测试',list_id:appData.word_lists[0].id,schedule:{state:0,reps:0,stability:0},reviews:[]});saveData();reviewQueue=[appData.words.find(w=>w.word==='offlinefresh')];reviewIndex=0;reviewRevealed=true;rateReviewCard(3)});await a.ctx.setOffline(false);await a.p.evaluate(()=>CloudSync.flush());assert.equal(await a.p.evaluate(()=>appData.words.find(w=>w.word==='offlinefresh').schedule.reps),1);assert.equal(await a.p.evaluate(()=>appData.words.find(w=>w.word==='offlinefresh').reviews.length),1)}finally{await a.ctx.close()}});
test('remote answer advances a revealed card before the next local confirmation',async()=>{const a=await device(),b=await device();try{await ready(a.p);await ready(b.p);const ids=await a.p.evaluate(()=>{const batch=PhotoCore.createBatch(appData,'并发页面测试',[{word:'cloudalpha',meaning:'甲',certainty:'high'},{word:'cloudbeta',meaning:'乙',certainty:'high'}]);const session=PhotoCore.startSession(appData,batch.id);PhotoUI.batchId=batch.id;PhotoUI.sessionId=session.id;saveData();switchTab('photo');PhotoUI.reveal('known');return {session:session.id,first:session.queue[0],second:session.queue[1]}});await a.p.evaluate(()=>CloudSync.flush());await b.p.evaluate(()=>CloudSync.flush());await b.p.evaluate(ids=>{PhotoCore.answer(appData,ids.session,ids.first,'unknown');saveData()},ids);await b.p.evaluate(()=>CloudSync.flush());await a.p.evaluate(()=>CloudSync.flush());assert.equal(await a.p.locator('#photo-question-word').textContent(),ids.second);assert.equal(await a.p.evaluate(()=>PhotoUI.revealed),false)}finally{await a.ctx.close();await b.ctx.close()}});
test('complete offline backup restores pending operations without double counting',async()=>{const a=await device(),b=await device();try{await ready(a.p);await ready(b.p);await a.ctx.setOffline(true);const backup=await a.p.evaluate(()=>{appData.words.push({id:99991,word:'offlinebackup',meaning:'备份',list_id:appData.word_lists[0].id,schedule:{state:0,reps:0,stability:0},reviews:[]});saveData();reviewQueue=[appData.words.find(w=>w.word==='offlinebackup')];reviewIndex=0;reviewRevealed=true;rateReviewCard(3);return CloudSync.backup()});await b.p.evaluate(data=>CloudSync.importData(data),backup);assert.equal(await b.p.evaluate(()=>appData.words.find(w=>w.word==='offlinebackup').schedule.reps),1);assert.equal(await b.p.evaluate(()=>appData.words.find(w=>w.word==='offlinebackup').reviews.length),1);await a.ctx.setOffline(false);await a.p.evaluate(()=>CloudSync.flush());assert.equal(await a.p.evaluate(()=>appData.words.find(w=>w.word==='offlinebackup').reviews.length),1)}finally{await a.ctx.close();await b.ctx.close()}});

test('independent accounts stay separate across devices and account switches in one browser',async()=>{
 const a=await device(),same=await device(),b=await device('guest-user');
 try{
  await ready(a.p);await ready(same.p);await ready(b.p);
  assert.equal(await a.p.locator('[data-cloud-account]').count(),1,'current account is visible');
  assert.equal(await a.p.locator('[data-cloud-account]').textContent(),'test@example.invalid');
  assert.equal(await b.p.evaluate(()=>appData.words.length),0,'new guest starts empty');
  await add(a.p,'accountprivate');
  await same.p.evaluate(()=>CloudSync.flush());
  assert.equal(await same.p.evaluate(()=>appData.words.some(w=>w.word==='accountprivate')),true);
  await b.p.evaluate(()=>CloudSync.flush());
  assert.equal(await b.p.evaluate(()=>appData.words.some(w=>w.word==='accountprivate')),false);
  await add(b.p,'accountprivate');
  await b.p.evaluate(()=>{appData.words.find(w=>w.word==='accountprivate').meaning='乙的独立释义';saveData()});
  await b.p.evaluate(()=>CloudSync.flush());
  await a.p.evaluate(()=>CloudSync.flush());
  assert.equal(await a.p.evaluate(()=>appData.words.find(w=>w.word==='accountprivate').meaning),'测试释义');
  await a.p.goto(url+'?account=guest-user');await ready(a.p);
  assert.equal(await a.p.evaluate(()=>appData.words.find(w=>w.word==='accountprivate').meaning),'乙的独立释义');
  await a.p.goto(url+'?account=1');await ready(a.p);
  assert.equal(await a.p.evaluate(()=>appData.words.find(w=>w.word==='accountprivate').meaning),'测试释义');
  assert.equal(await a.p.evaluate(()=>CloudSync.outbox.length),0,'account switch does not upload the other library');
 }finally{await a.ctx.close();await same.ctx.close();await b.ctx.close();}
});

test('a stale tab retains its outbox when another tab signs in to a different account',async()=>{
 const a=await device();let switching;
 try{
  await ready(a.p);
  switching=await a.ctx.newPage();await switching.goto(url+'?account=guest-user');await ready(switching);
  await a.p.evaluate(()=>{appData.words.push({id:999992,word:'staletabword',meaning:'原账号待上传',list_id:appData.word_lists[0].id,reviews:[],schedule:null});saveData()});
  await a.p.evaluate(()=>CloudSync.flush());
  assert.equal(await a.p.evaluate(()=>CloudSync.outbox.length),1,'stale upload remains in original account journal');
  await switching.evaluate(()=>CloudSync.flush());
  assert.equal(await switching.evaluate(()=>appData.words.some(w=>w.word==='staletabword')),false);
  await switching.goto(url+'?account=1');await ready(switching);
  await a.p.evaluate(()=>CloudSync.flush());
  assert.equal(await a.p.evaluate(()=>CloudSync.outbox.length),0);
  await switching.evaluate(()=>CloudSync.flush());
  assert.equal(await switching.evaluate(()=>appData.words.some(w=>w.word==='staletabword')),true);
 }finally{await a.ctx.close();}
});

test('photo requests carry the page account through the authenticated request boundary',async()=>{
 const a=await device();
 try{
  await ready(a.p);
  const response=a.p.waitForResponse(r=>r.url().endsWith('/api/photo/recognize'));
  await a.p.evaluate(async()=>{try{await PhotoUI.request('recognize',{images:[{id:'test-page',data_url:'data:image/png;base64,iVBORw0KGgo='}]})}catch{}});
  assert.equal((await response).status(),502,'request reaches the unconfigured local vision service');
 }finally{await a.ctx.close();}
});
