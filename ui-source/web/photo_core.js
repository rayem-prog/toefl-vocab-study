/* Pure batch/diagnostic rules shared by browser and Node tests. */
(function(root){
  'use strict';
  const fields=['batches','items','sessions','answers','review_actions'];
  const key=s=>String(s||'').trim().toLowerCase();
  const uid=()=>globalThis.crypto?.randomUUID?.()||'p-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2)+'-'+Math.random().toString(36).slice(2);
  function ensure(d){if(!d.photo_state||typeof d.photo_state!=='object')d.photo_state={};for(const f of fields)if(!Array.isArray(d.photo_state[f]))d.photo_state[f]=[];return d.photo_state;}
  function stamp(d){const values=fields.flatMap(f=>ensure(d)[f].map(r=>Date.parse(r.created_at)||0));return new Date(Math.max(Date.now(),values.reduce((a,b)=>Math.max(a,b),0)+1)).toISOString();}
  function word(d,k){return d.words.find(w=>key(w.word)===key(k));}
  function items(d,bid){const map=new Map();for(const i of ensure(d).items.filter(i=>i.batch_id===bid).sort((a,b)=>a.created_at.localeCompare(b.created_at)||a.id.localeCompare(b.id)))map.set(i.word_key,i);return [...map.values()];}
  function createBatch(d,name,entries){
    if(!entries.length)throw Error('请至少选择一个主词');
    const cleaned=[],seen=new Set();
    for(const e of entries){const k=key(e.word);
      if(!/^[a-z]+(?:[ '\u2019-][a-z]+)*$/.test(k)||k.length>100)throw Error('请校对英文主词：'+e.word);
      if((e.needs_confirmation||e.certainty==='low'||e.certainty==='medium')&&!e.confirmed)throw Error('请确认不确定的主词：'+e.word);
      if(!seen.has(k)){seen.add(k);cleaned.push({...e,word_key:k});}
    }
    const p=ensure(d),at=stamp(d),batch={id:uid(),name:String(name||'词汇书照片').slice(0,150),created_at:at};
    let list=d.word_lists.find(l=>l.name==='📷 拍照录入');
    for(const e of cleaned){
      if(!word(d,e.word_key)){
        if(!list){list={id:d.word_lists.reduce((m,l)=>Math.max(m,Number(l.id)||0),0)+1,name:'📷 拍照录入',color:'#0D9488',created_at:at};d.word_lists.push(list);}
        const id=Math.max(Date.now(),d.words.reduce((m,w)=>Math.max(m,Number(w.id)||0),0)+1);
        d.words.push({id,word:e.word.trim(),meaning:e.meaning||'',phonetic:e.phonetic||'',example:'',note:'拍照录入',category:'plan',list_id:list.id,list_name:list.name,created_at:at,schedule:null,reviews:[],photo_pending:true});
      }
      p.items.push({id:uid(),batch_id:batch.id,word_key:e.word_key,source_word:e.word.trim(),meaning:e.meaning||'',phonetic:e.phonetic||'',source_image_id:e.source_image_id||'',location_hint:e.location_hint||'',created_at:at});
    }
    p.batches.push(batch);return batch;
  }
  function updateMeaning(d,bid,k,text){
    const item=items(d,bid).find(i=>i.word_key===key(k)),w=word(d,k);if(!item||!w||!String(text).trim())throw Error('请填写有效释义');
    const meaning=String(text).trim().slice(0,3000);ensure(d).items.push({...item,id:uid(),meaning,created_at:stamp(d)});if(!w.meaning)w.meaning=meaning;
  }
  function startSession(d,bid,mode='flash',keys=null){
    if(!['flash','spell'].includes(mode))throw Error('不支持的题型');
    const allowed=new Set(items(d,bid).map(i=>i.word_key));
    const queue=[...new Set(keys||allowed)].filter(k=>allowed.has(k)&&word(d,k));
    if(!queue.length)throw Error('没有可测试单词');
    if(mode==='spell'&&queue.some(k=>!(items(d,bid).find(i=>i.word_key===k)?.meaning||word(d,k)?.meaning)))throw Error('拼写测试需要每个词都有释义，请先补全');
    for(let i=queue.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[queue[i],queue[j]]=[queue[j],queue[i]];}
    const s={id:uid(),batch_id:bid,mode,queue,created_at:stamp(d)};ensure(d).sessions.push(s);return s;
  }
  function answers(d,sid){const out=new Map();for(const a of ensure(d).answers.filter(a=>a.session_id===sid).sort((a,b)=>a.created_at.localeCompare(b.created_at)||a.id.localeCompare(b.id)))out.set(a.word_key,a);return out;}
  function current(d,sid){const s=ensure(d).sessions.find(s=>s.id===sid);if(!s)return null;const a=answers(d,sid);return s.queue.find(k=>!a.has(k))||null;}
  function answer(d,sid,k,result,text='',id=uid()){
    const p=ensure(d),s=p.sessions.find(s=>s.id===sid);if(!s||!s.queue.includes(k))throw Error('单词不属于本次测试');
    if(!['known','vague','unknown','wrong','guessed','skipped'].includes(result))throw Error('无效结果');
    if(p.answers.some(a=>a.id===id))return;
    p.answers.push({id,session_id:sid,word_key:k,result,user_answer:String(text).slice(0,500),revealed:result!=='skipped',created_at:stamp(d)});
  }
  function sessionSummary(d,sid){const s=ensure(d).sessions.find(s=>s.id===sid),a=answers(d,sid),out={total:s?.queue.length||0,known:0,vague:0,unknown:0,wrong:0,guessed:0,skipped:0,answered:0};for(const k of s?.queue||[])if(a.has(k)){out[a.get(k).result]++;out.answered++;}return out;}
  function summary(d,bid){
    const p=ensure(d),ss=new Set(p.sessions.filter(s=>s.batch_id===bid).map(s=>s.id)),latest=new Map();
    for(const a of p.answers.filter(a=>ss.has(a.session_id)&&a.result!=='skipped').sort((a,b)=>a.created_at.localeCompare(b.created_at)||a.id.localeCompare(b.id)))latest.set(a.word_key,a);
    const rows=items(d,bid).filter(i=>word(d,i.word_key)).map(i=>({...i,result:latest.get(i.word_key)?.result||'untested'}));
    return {all:rows,pending:rows.filter(i=>['unknown','wrong','vague','guessed'].includes(i.result)).sort((a,b)=>Number(['vague','guessed'].includes(a.result))-Number(['vague','guessed'].includes(b.result))),untested:rows.filter(i=>i.result==='untested'),known:rows.filter(i=>i.result==='known')};
  }
  function reconcile(d){
    for(const a of ensure(d).review_actions){const w=word(d,a.word_key),at=Date.parse(a.created_at);if(!w||!Number.isFinite(at))continue;
      if((w.reviews||[]).some(r=>Date.parse(r.accepted_at||r.reviewed_at)>=at))continue;
      w.photo_pending=false;
      if(!w.schedule)w.schedule={due:a.created_at,reps:0,lapses:0,state:0,stability:0,difficulty:0};
      else if(!w.schedule.due||Date.parse(w.schedule.due)>at)w.schedule.due=a.created_at;
    }
  }
  function queueReview(d,keys,eventId=uid()){
    const p=ensure(d);for(const k of new Set(keys)){if(!word(d,k))continue;const id=eventId+':'+key(k);if(p.review_actions.some(a=>a.id===id))continue;
      p.review_actions.push({id,word_key:key(k),created_at:stamp(d)});
    }reconcile(d);
  }
  const stable=x=>JSON.stringify(x,(_,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.keys(v).sort().map(k=>[k,v[k]])):v);
  function merge(d,incoming){const p=ensure(d),other=incoming.photo_state||{};for(const f of fields){const map=new Map(p[f].map(r=>[r.id,r]));for(const r of other[f]||[]){if(!r||typeof r.id!=='string')continue;const old=map.get(r.id);if(!old||stable(r)<stable(old))map.set(r.id,JSON.parse(JSON.stringify(r)));}p[f]=[...map.values()];}reconcile(d);}
  root.PhotoCore={ensure,key,uid,word,items,createBatch,updateMeaning,startSession,current,answer,answers,summary,sessionSummary,merge,queueReview,reconcile};
  if(typeof module!=='undefined')module.exports=root.PhotoCore;
})(globalThis);
