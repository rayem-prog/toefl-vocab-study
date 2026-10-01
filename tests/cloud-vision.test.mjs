import test from 'node:test';import assert from 'node:assert/strict';let vision;try{vision=await import('../lib/cloud-vision.mjs')}catch{}
test('vision validates main words and retains uncertainty without guessing',()=>{assert.ok(vision,'云端图片适配器尚未实现');const r=vision.validateResult({words:[{source_word:'lucid',entry_role:'main',certainty:'high',needs_confirmation:false},{source_word:'clarity',entry_role:'derivative'},{source_word:'vivid',entry_role:'main',certainty:'low'},{source_word:'a...',entry_role:'main'}]},'image');assert.deepEqual(r.words.map(w=>w.word),['lucid','vivid']);assert.equal(r.words[1].needs_confirmation,true);assert.equal(r.unresolved.length,1)});
test('missing vision config is actionable and invalid image rejected',()=>{assert.ok(vision);assert.throws(()=>vision.validateImage('data:image/png;base64,YWJj'),/图片/);assert.deepEqual(vision.publicConfig({}),{configured:false,has_key:false,endpoint:'',model:'',readonly:true})});

test('vision request works when runtime has no AbortSignal.timeout',async()=>{
 const original=AbortSignal.timeout;AbortSignal.timeout=undefined;
 try{
  const image='data:image/png;base64,'+Buffer.from([137,80,78,71,13,10,26,10,0,0,0,13,73,72,68,82,0,0,0,1,0,0,0,1,...Array(30).fill(0)]).toString('base64');
  const result=await vision.recognize({PHOTO_VISION_ENDPOINT:'https://example.test',PHOTO_VISION_MODEL:'vision',PHOTO_VISION_API_KEY:'test-secret'},[{id:'page',data_url:image}],async()=>Response.json({choices:[{message:{content:JSON.stringify({words:[],warnings:[],unresolved:[]})}}]}));
  assert.equal(result.words.length,0);
 }finally{AbortSignal.timeout=original;}
});
