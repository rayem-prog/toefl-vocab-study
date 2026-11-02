/* Photo workflow UI. Generated inline by build_web.py; images and provider keys never enter sync data. */
const PhotoUI={
  images:[],entries:[],batchId:'',sessionId:'',studyKeys:[],studyIndex:0,controller:null,revealed:false,guess:'',spellText:'',initialized:false,
  el(id){return document.getElementById('photo-'+id);},
  message(text,error=false){const e=this.el('message');e.hidden=!text;e.textContent=text;e.classList.toggle('error',error);},
  persist(){PhotoCore.ensure(appData);saveData();try{localStorage.setItem('TOEFL_PHOTO_VIEW',JSON.stringify({batchId:this.batchId,sessionId:this.sessionId}));}catch{}this.syncLater();},
  syncLater(){clearTimeout(this.syncTimer);this.syncTimer=setTimeout(()=>triggerAutoCloudPush(),1000);},
  init(){
    PhotoCore.ensure(appData);
    if(!this.initialized){
      this.initialized=true;
      try{const v=JSON.parse(localStorage.getItem('TOEFL_PHOTO_VIEW')||'{}');this.batchId=v.batchId||'';this.sessionId=v.sessionId||'';}catch{}
      const z=this.el('drop-zone');z.addEventListener('dragover',e=>{e.preventDefault();e.stopPropagation();z.classList.add('dragging');});z.addEventListener('dragleave',()=>z.classList.remove('dragging'));
      z.addEventListener('drop',e=>{e.preventDefault();e.stopPropagation();z.classList.remove('dragging');this.addFiles(e.dataTransfer.files);});
      this.loadConfig();
    }
    this.render();
  },
  async request(path,body,signal){
    const cfg=getCloudSettings();const base=(location.protocol.startsWith('http')?location.origin:cfg.serverUrl).replace(/\/+$/,'');
    const headers={'Content-Type':'application/json'};if(cfg.token)headers['X-Sync-Token']=cfg.token;
    if(window.CLOUD_ACCOUNT?.userId)headers['X-Toefl-Account']=window.CLOUD_ACCOUNT.userId;
    const control=signal?null:new AbortController();const timeout=control?setTimeout(()=>control.abort(),15000):null;
    try{const r=await fetch(base+'/api/photo/'+path,{method:body===undefined?'GET':'POST',headers,body:body===undefined?undefined:JSON.stringify(body),signal:signal||control.signal});
      let data;try{data=await r.json();}catch{throw Error('服务器未提供图片识别接口，请启动此文件夹的新版 server.py');}
      if(!r.ok)throw Error(r.status===404?'当前服务器版本不支持拍照测词，请启动新版 server.py':data.error||'服务器请求失败');return data;
    }catch(e){
      if(e instanceof TypeError){
        const local=['localhost','127.0.0.1','::1'].includes(location.hostname);
        throw Error(local?'无法连接本地服务。请双击项目文件夹中的“启动拍照测词(Mac).command”，服务启动后再点“检查设置”。已保存的设置和学习进度会保留。':'无法连接词汇服务器，请确认电脑上的“启动拍照测词(Mac).command”已启动，并检查两台设备的网络连接。');
      }
      if(e.name==='AbortError'&&!signal)throw Error('服务器响应超时，请检查服务是否运行，再点“检查设置”重试。');
      throw e;
    }finally{clearTimeout(timeout);}
  },
  async loadConfig(){try{const c=await this.request('settings');this.el('config-status').textContent=c.configured?'已配置':'尚未配置';this.el('endpoint').value=c.endpoint||'';this.el('model').value=c.model||'';this.el('api-key').placeholder=c.has_key?'密钥已保存；留空保留':'填写API Key，只保存至服务器';}catch(e){this.el('config-status').textContent=e.message;}}
  ,async saveConfig(){const b=this.el('save-config');b.disabled=true;try{const data={endpoint:this.el('endpoint').value.trim(),model:this.el('model').value.trim(),api_key:this.el('api-key').value.trim()};if(!data.endpoint||!data.model)throw Error('请填写接口地址和视觉模型名称');await this.request('settings',data);this.el('api-key').value='';await this.loadConfig();this.message('识别设置已保存，可以选择照片开始识别。');}catch(e){this.el('api-key').value='';this.message(e.message,true);}finally{b.disabled=false;}},
  showUpload(){this.el('upload').hidden=false;this.el('upload').scrollIntoView({behavior:'smooth',block:'start'});},
  async addFiles(files){if(this.controller)return this.message('请先取消当前识别，再添加照片。',true);this.invalidatePreview();this.message('');
    for(const file of Array.from(files||[])){
      if(this.images.length>=8){this.message('一批最多8张照片，请先完成当前批次。',true);break;}
      if(!file.type.startsWith('image/')||file.size>20*1024*1024){this.message('请选择20MB以内的图片。',true);continue;}
      try{
        const bitmap=await createImageBitmap(file,{imageOrientation:'from-image'});const ratio=Math.min(1,2200/Math.max(bitmap.width,bitmap.height));
        const canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.round(bitmap.width*ratio));canvas.height=Math.max(1,Math.round(bitmap.height*ratio));canvas.getContext('2d').drawImage(bitmap,0,0,canvas.width,canvas.height);bitmap.close();
        const data=canvas.toDataURL('image/jpeg',.94);if(data.length>10*1024*1024)throw Error('图片处理后仍过大，请裁剪后上传');
        this.images.push({id:PhotoCore.uid(),name:file.name,data_url:data});
      }catch(e){this.message('无法读取照片，请改用JPEG或PNG：'+file.name,true);}
    }this.renderImages();
  },
  invalidatePreview(){this.entries=[];this.el('preview').hidden=true;this.el('preview').innerHTML='';},
  renderImages(){const box=this.el('images');box.innerHTML=this.images.map((im,i)=>`<div class="photo-image"><img src="${im.data_url}" alt="照片 ${i+1} 预览"><p>${i+1}. ${esc(im.name)}</p><div class="photo-actions"><button class="btn btn-outline" data-rotate="${i}">旋转</button><button class="btn btn-outline" data-crop="${i}">裁剪</button><button class="btn btn-outline" data-remove="${i}">移除</button></div></div>`).join('');
    box.querySelectorAll('[data-remove]').forEach(b=>b.onclick=()=>{if(this.controller)return;this.images.splice(Number(b.dataset.remove),1);this.invalidatePreview();this.renderImages();});
    box.querySelectorAll('[data-rotate]').forEach(b=>b.onclick=()=>this.rotate(Number(b.dataset.rotate)));box.querySelectorAll('[data-crop]').forEach(b=>b.onclick=()=>this.crop(Number(b.dataset.crop)));this.el('recognize').disabled=!this.images.length||!!this.controller;
  },
  async bitmap(im){const blob=await(await fetch(im.data_url)).blob();return createImageBitmap(blob);},
  async rotate(i){if(this.controller)return;const im=this.images[i];if(!im)return;const bmp=await this.bitmap(im),c=document.createElement('canvas');c.width=bmp.height;c.height=bmp.width;const ctx=c.getContext('2d');ctx.translate(c.width,0);ctx.rotate(Math.PI/2);ctx.drawImage(bmp,0,0);bmp.close();im.data_url=c.toDataURL('image/jpeg',.94);this.invalidatePreview();this.renderImages();},
  async crop(i){
    if(this.controller)return;const im=this.images[i];if(!im)return;const bmp=await this.bitmap(im);
    let dialog=document.getElementById('photo-crop-dialog');if(dialog)dialog.remove();dialog=document.createElement('dialog');dialog.id='photo-crop-dialog';dialog.innerHTML='<h3>拖动框选要识别的区域</h3><canvas id="photo-crop-canvas"></canvas><div class="photo-actions"><button id="photo-apply-crop" class="btn btn-primary">保留选区</button><button id="photo-close-crop" class="btn btn-outline">取消</button></div>';document.body.appendChild(dialog);
    const canvas=dialog.querySelector('canvas'),ratio=Math.min(1,680/bmp.width,500/bmp.height);canvas.width=Math.round(bmp.width*ratio);canvas.height=Math.round(bmp.height*ratio);const context=canvas.getContext('2d');let start=null,selection=null;
    const draw=()=>{context.clearRect(0,0,canvas.width,canvas.height);context.drawImage(bmp,0,0,canvas.width,canvas.height);if(selection){context.fillStyle='rgba(15,23,42,.4)';context.fillRect(0,0,canvas.width,canvas.height);context.drawImage(bmp,selection.x/ratio,selection.y/ratio,selection.w/ratio,selection.h/ratio,selection.x,selection.y,selection.w,selection.h);context.strokeStyle='#5EEAD4';context.lineWidth=2;context.strokeRect(selection.x,selection.y,selection.w,selection.h);}};
    const point=e=>{const r=canvas.getBoundingClientRect();return{x:Math.max(0,Math.min(canvas.width,(e.clientX-r.left)*canvas.width/r.width)),y:Math.max(0,Math.min(canvas.height,(e.clientY-r.top)*canvas.height/r.height))};};
    canvas.onpointerdown=e=>{start=point(e);canvas.setPointerCapture(e.pointerId);};canvas.onpointermove=e=>{if(!start)return;const p=point(e);selection={x:Math.min(start.x,p.x),y:Math.min(start.y,p.y),w:Math.abs(p.x-start.x),h:Math.abs(p.y-start.y)};draw();};canvas.onpointerup=()=>{start=null;};
    dialog.querySelector('#photo-close-crop').onclick=()=>dialog.close();dialog.querySelector('#photo-apply-crop').onclick=()=>{if(!selection||selection.w<15||selection.h<15)return;const out=document.createElement('canvas');out.width=Math.round(selection.w/ratio);out.height=Math.round(selection.h/ratio);out.getContext('2d').drawImage(bmp,selection.x/ratio,selection.y/ratio,out.width,out.height,0,0,out.width,out.height);im.data_url=out.toDataURL('image/jpeg',.94);this.invalidatePreview();this.renderImages();dialog.close();};dialog.onclose=()=>{bmp.close();dialog.remove();};draw();dialog.showModal();
  },
  async recognize(){
    if(this.controller||!this.images.length)return;this.invalidatePreview();this.controller=new AbortController();this.renderImages();this.el('cancel').hidden=false;
    const controller=this.controller;const timer=setTimeout(()=>controller.abort(),480000);
    this.message('正在识别主词… 图片只用于这次识别，不加入词库同步。');
    try{const r=await this.request('recognize',{images:this.images.map(({id,data_url})=>({id,data_url}))},controller.signal);
      this.entries=r.words.map(e=>({...e,selected:true,confirmed:!e.needs_confirmation&&e.certainty==='high'}));
      this.unresolved=r.unresolved||[];this.warnings=r.warnings||[];this.renderPreview();
      this.message(`识别出 ${this.entries.length} 个候选主词。请核对拼写和释义，确认后再保存测试。${this.unresolved.length?'另有 '+this.unresolved.length+' 处无法辨认，可重拍或手动补词。':''}`);
    }catch(e){this.message(e.name==='AbortError'?'识别已取消或超时，照片仍保留，可重新识别。':e.message,true);}finally{clearTimeout(timer);this.controller=null;this.el('cancel').hidden=true;this.renderImages();}
  },
  cancelRecognition(){this.controller?.abort();},
  renderPreview(){
    const box=this.el('preview');box.hidden=false;
    box.innerHTML=`<h3 style="margin-top:24px">校对主词</h3><p class="photo-muted">勾选要测试的词。修改主词或补充释义后再确认；已有词保留原词单与进度。</p><div id="photo-entries"></div><div class="photo-actions"><button id="photo-add-entry" class="btn btn-outline">＋ 手动补一个主词</button><button id="photo-fill-meanings" class="btn btn-outline">查词补全缺失释义</button></div><div id="photo-unresolved"></div><label>本次批次名称<input id="photo-batch-name" class="input-field" value="${esc(new Date().toLocaleDateString('zh-CN')+' · 本次照片')}"></label><button id="photo-confirm-batch" class="btn btn-primary">确认主词，保存批次</button>`;
    this.renderEntries();box.querySelector('#photo-add-entry').onclick=()=>{this.entries.push({word:'',meaning:'',phonetic:'',certainty:'low',needs_confirmation:true,confirmed:false,selected:true});this.renderEntries();};box.querySelector('#photo-fill-meanings').onclick=()=>this.fillMeanings();box.querySelector('#photo-confirm-batch').onclick=()=>this.confirmBatch();
    this.el('unresolved').innerHTML=[...(this.unresolved||[]).map(e=>`${e.visible_text||''} · ${e.location_hint||''} · ${e.reason||'无法辨认'}`),...(this.warnings||[])].map(t=>`<p class="photo-muted">${esc(t)}</p>`).join('');
  },
  renderEntries(){const box=this.el('entries');if(!box)return;box.innerHTML=this.entries.map((e,i)=>{
    const existing=PhotoCore.word(appData,e.word);return `<div class="photo-entry"><input type="checkbox" data-select-row="${i}" aria-label="选择主词 ${i+1}" ${e.selected?'checked':''}><label>主词<input class="input-field" data-word-row="${i}" value="${esc(e.word)}" autocomplete="off"></label><label>中文释义<input class="input-field" data-meaning-row="${i}" value="${esc(e.meaning||existing?.meaning||'')}"></label><label>音标<input class="input-field" data-phonetic-row="${i}" value="${esc(e.phonetic||existing?.phonetic||'')}"></label><div class="photo-entry-meta"><span>${esc(e.location_hint||'人工校对')} · ${existing?'已有词，复用记录':'新词'} · ${e.certainty==='high'?'识别较确定':'需确认'}${e.meaning_source==='dictionary'?' · 词典补充':''}</span><label><input type="checkbox" data-confirm-row="${i}" ${e.confirmed?'checked':''}> 已核对主词与释义</label><span>${esc(e.reason||'')}</span></div></div>`;}).join('');
    for(const f of ['word','meaning','phonetic'])box.querySelectorAll(`[data-${f}-row]`).forEach(el=>{const i=Number(el.dataset[f+'Row']);if(f!=='word'&&!this.entries[i][f])this.entries[i][f]=el.value;el.oninput=()=>{this.entries[i][f]=el.value;if(f==='word'){this.entries[i].confirmed=false;this.entries[i].needs_confirmation=true;box.querySelector(`[data-confirm-row="${i}"]`).checked=false;}};});
    box.querySelectorAll('[data-select-row]').forEach(el=>el.onchange=()=>this.entries[Number(el.dataset.selectRow)].selected=el.checked);box.querySelectorAll('[data-confirm-row]').forEach(el=>el.onchange=()=>this.entries[Number(el.dataset.confirmRow)].confirmed=el.checked);
  },
  async fillMeanings(){const b=this.el('fill-meanings');b.disabled=true;let found=0;try{for(const e of this.entries.filter(e=>e.selected&&!e.meaning&&e.word.trim())){const existing=PhotoCore.word(appData,e.word);if(existing?.meaning){e.meaning=existing.meaning;continue;}const r=await this.request('lookup',{word:e.word});if(r.meaning){e.meaning=r.meaning;if(!e.phonetic)e.phonetic=r.phonetic||'';e.meaning_source='dictionary';e.needs_confirmation=true;e.confirmed=false;found++;}}this.renderEntries();this.message(`已补充 ${found} 个词的释义。词典补充请核对后确认；未查到的可以手动填写。`);}catch(e){this.renderEntries();this.message(e.message,true);}finally{b.disabled=false;}},
  confirmBatch(){
    try{const entries=this.entries.filter(e=>e.selected);const b=PhotoCore.createBatch(appData,this.el('batch-name').value,entries);this.batchId=b.id;this.sessionId='';this.images=[];this.invalidatePreview();this.renderImages();this.el('upload').hidden=true;this.persist();this.render();this.message(`已保存 ${PhotoCore.items(appData,b.id).length} 个主词，可以开始测试。`);}catch(e){this.message(e.message,true);}
  },
  selectBatch(id){this.batchId=id;this.sessionId='';this.studyKeys=[];this.revealed=false;this.restoreSession();this.persist();this.render();},
  restoreSession(){const p=PhotoCore.ensure(appData);const sessions=p.sessions.filter(s=>s.batch_id===this.batchId).sort((a,b)=>b.created_at.localeCompare(a.created_at));if(!sessions.some(s=>s.id===this.sessionId))this.sessionId=(sessions.find(s=>PhotoCore.current(appData,s.id))||sessions[0])?.id||'';},
  render(){const p=PhotoCore.ensure(appData),batches=p.batches.slice().sort((a,b)=>b.created_at.localeCompare(a.created_at));if(!batches.some(b=>b.id===this.batchId))this.batchId=batches[0]?.id||'';
    const select=this.el('batch-select');select.innerHTML=batches.length?batches.map(b=>`<option value="${esc(b.id)}">${esc(b.name)} · ${PhotoCore.items(appData,b.id).length}词</option>`).join(''):'<option value="">还没有照片批次</option>';select.value=this.batchId;
    this.el('batch-count').textContent=batches.length?`${batches.length} 个照片批次`:'';this.el('learning').hidden=!this.batchId;
    if(!this.batchId)return;this.restoreSession();this.renderTest();this.renderResults();
  },
  startTest(pending=false){try{const keys=pending?PhotoCore.summary(appData,this.batchId).pending.map(i=>i.word_key):null;this.beginSession(keys);}catch(e){this.message(e.message,true);}},
  beginSession(keys){
    // Both recall and spelling need a usable answer; do not invent a missing definition.
    const rows=PhotoCore.items(appData,this.batchId).filter(i=>!keys||keys.includes(i.word_key));
    const missing=rows.filter(i=>!(i.meaning||PhotoCore.word(appData,i.word_key)?.meaning));
    if(missing.length)throw Error('请先为以下主词补充释义：'+missing.map(i=>i.source_word||i.word_key).join('、')+'。可在下方单词行点击“补充释义”。');
    const s=PhotoCore.startSession(appData,this.batchId,this.el('test-mode').value,keys);this.sessionId=s.id;this.revealed=false;this.studyKeys=[];this.el('study').hidden=true;this.message('');this.persist();this.renderTest();this.el('test-card').scrollIntoView({behavior:'smooth',block:'start'});
  },
  testUntested(){try{this.beginSession(PhotoCore.summary(appData,this.batchId).untested.map(i=>i.word_key));}catch(e){this.message(e.message,true);}},
  session(){return PhotoCore.ensure(appData).sessions.find(s=>s.id===this.sessionId);},
  current(){const k=PhotoCore.current(appData,this.sessionId);if(!k)return null;const w=PhotoCore.word(appData,k),item=PhotoCore.items(appData,this.batchId).find(i=>i.word_key===k);return w?{...w,word_key:k,meaning:item?.meaning||w.meaning,phonetic:item?.phonetic||w.phonetic}:null;},
  renderTest(){
    const s=this.session(),sum=s?PhotoCore.sessionSummary(appData,s.id):null,k=s?PhotoCore.current(appData,s.id):null;
    this.el('test-progress').textContent=sum?`${sum.answered} / ${sum.total}`:'';this.el('test-setup').hidden=!!k;this.el('question').hidden=!k;this.el('test-done').hidden=!s||!!k;
    if(s&&!k){const tested=sum.total-sum.skipped;const metric=s.mode==='flash'?'自评记得比例':'拼写正确率';const rate=tested?Math.round(sum.known/tested*100):0;this.el('test-summary').textContent=`本轮共 ${sum.total} 词：记得/答对 ${sum.known}，模糊 ${sum.vague}，不认识 ${sum.unknown}，答错 ${sum.wrong}，猜中 ${sum.guessed}，跳过 ${sum.skipped}。${metric} ${rate}%（跳过不计入分母）。`;return;}
    if(!k)return;const w=this.current();if(!w){this.message('当前词已从词库删除，可跳过继续。',true);this.el('question-word').textContent=k;return;}
    this.el('question-label').textContent=s.mode==='flash'?'先在脑海里回忆中文释义，再选择':'根据中文释义拼写英文';
    this.el('question-word').textContent=s.mode==='flash'?w.word:w.meaning;
    this.el('flash-controls').hidden=s.mode!=='flash'||this.revealed;this.el('spell-controls').hidden=s.mode!=='spell'||this.revealed;this.el('answer').hidden=!this.revealed;this.el('skip').hidden=this.revealed;
    this.el('spell-input').value='';
    if(this.revealed){this.el('answer-word').textContent=w.word;this.el('answer-phonetic').textContent=w.phonetic||'';this.el('answer-meaning').textContent=w.meaning||'';this.el('correct-result').value=this.guess;this.el('spell-feedback').textContent=s.mode==='spell'?`你的拼写：${this.spellText||'（空白）'} · ${this.guess==='known'?'拼写正确':'请核对完整拼写'}`:'';}
  },
  reveal(result){if(!this.current()||this.revealed)return;this.guess=result;this.spellText='';this.revealed=true;this.renderTest();},
  submitSpell(){if(this.revealed)return;const w=this.current();if(!w)return;this.spellText=this.el('spell-input').value;this.guess=PhotoCore.key(this.spellText)===PhotoCore.key(w.word)?'known':'wrong';this.revealed=true;this.renderTest();},
  confirmAnswer(){if(!this.revealed)return;const w=this.current();if(!w)return;PhotoCore.answer(appData,this.sessionId,w.word_key,this.el('correct-result').value,this.spellText);this.revealed=false;this.persist();this.renderTest();this.renderResults();if(!PhotoCore.current(appData,this.sessionId))this.showResults();},
  skip(){const k=PhotoCore.current(appData,this.sessionId);if(!k)return;PhotoCore.answer(appData,this.sessionId,k,'skipped');this.revealed=false;this.persist();this.renderTest();this.renderResults();},
  speakCurrent(){const w=this.current();if(w)speakWord(w.word);},
  renderResults(){
    if(!this.batchId)return;const sum=PhotoCore.summary(appData,this.batchId);this.el('pending-count').textContent=`${sum.pending.length} 词待背`;this.el('overview').textContent=`本批次 ${sum.all.length} 个主词 · ${sum.known.length} 个记得/已巩固 · ${sum.pending.length} 个需要背诵 · ${sum.untested.length} 个未测。重测后更新清单，初测结果保留在历史中。`;
    const filter=this.el('result-filter').value;const rows=sum[filter]||[];const labels={known:'记得 / 已巩固',vague:'记忆模糊',unknown:'不认识',wrong:'答错',guessed:'猜中，仍需背诵',untested:'尚未测试'};
    this.el('results').innerHTML=rows.length?rows.map(i=>{const w=PhotoCore.word(appData,i.word_key);return `<div class="photo-word-row"><div><strong>${esc(w.word)}</strong><small>${labels[i.result]}</small></div><p>${esc(i.meaning||w.meaning||'释义待补充')}</p><div class="photo-actions"><button class="btn btn-outline" data-speak-key="${esc(i.word_key)}">🔊</button>${!(i.meaning||w.meaning)?`<button class="btn btn-outline" data-edit-key="${esc(i.word_key)}">补充释义</button>`:''}</div></div>`;}).join(''):`<p class="photo-muted">${filter==='pending'?(sum.untested.length?'还没有待背结果。先完成测试，不认识和模糊的词会列在这里。':'本批次没有待背词，可以查看全部主词或再测一遍。'):'这个分类暂无单词。'}</p>`;
    this.el('results').querySelectorAll('[data-speak-key]').forEach(b=>b.onclick=()=>{const w=PhotoCore.word(appData,b.dataset.speakKey);if(w)speakWord(w.word);});this.el('results').querySelectorAll('[data-edit-key]').forEach(b=>b.onclick=()=>this.editMeaning(b.dataset.editKey));
    for(const id of ['study-start','retest','queue-review'])this.el(id).disabled=!sum.pending.length;this.el('test-untested').disabled=!sum.untested.length;
    this.el('session-history').innerHTML=PhotoCore.ensure(appData).sessions.filter(s=>s.batch_id===this.batchId).sort((a,b)=>b.created_at.localeCompare(a.created_at)).map(s=>{const n=PhotoCore.sessionSummary(appData,s.id);return `<p>${esc(new Date(s.created_at).toLocaleString('zh-CN'))} · ${s.mode==='flash'?'回忆自评':'拼写'} · ${n.answered}/${n.total} 已处理 · 记得 ${n.known} / 模糊 ${n.vague} / 不认识或错 ${n.unknown+n.wrong+n.guessed} / 跳过 ${n.skipped} <button class="btn btn-outline" data-session-id="${esc(s.id)}">${PhotoCore.current(appData,s.id)?'继续测试':'查看本轮统计'}</button></p>`;}).join('')||'<p>还没有测试记录。</p>';
    this.el('session-history').querySelectorAll('[data-session-id]').forEach(b=>b.onclick=()=>{this.sessionId=b.dataset.sessionId;this.revealed=false;this.persist();this.renderTest();this.el('test-card').scrollIntoView({behavior:'smooth'});});
  },
  editMeaning(k){const text=prompt('为 '+k+' 填写已核对的中文释义：');if(!text?.trim())return;const i=PhotoCore.items(appData,this.batchId).find(i=>i.word_key===k);if(!i)return;
    PhotoCore.updateMeaning(appData,this.batchId,k,text);this.persist();this.render();
  },
  showResults(){this.el('result-filter').value='pending';this.renderResults();this.el('results-card').scrollIntoView({behavior:'smooth',block:'start'});},
  queueReview(){const keys=PhotoCore.summary(appData,this.batchId).pending.map(i=>i.word_key);if(!keys.length)return;const latest=PhotoCore.ensure(appData).sessions.filter(s=>s.batch_id===this.batchId).sort((a,b)=>b.created_at.localeCompare(a.created_at))[0];PhotoCore.queueReview(appData,keys,'batch-review-'+(latest?.id||this.batchId));this.persist();this.message(`已将 ${keys.length} 个待背词加入今日复习，原有学习进度已保留。`);},
  openReview(){this.queueReview();switchTab('review');},
  startStudy(){this.studyKeys=PhotoCore.summary(appData,this.batchId).pending.map(i=>i.word_key);this.studyIndex=0;if(!this.studyKeys.length)return;this.el('study').hidden=false;this.renderStudy();this.el('study').scrollIntoView({behavior:'smooth',block:'start'});},
  studyWord(){const k=this.studyKeys[this.studyIndex],w=PhotoCore.word(appData,k),i=PhotoCore.items(appData,this.batchId).find(i=>i.word_key===k);return w?{...w,meaning:i?.meaning||w.meaning,phonetic:i?.phonetic||w.phonetic}:null;},
  renderStudy(){const w=this.studyWord();if(!w){this.finishStudy();return;}this.el('study-progress').textContent=`${this.studyIndex+1} / ${this.studyKeys.length}`;this.el('study-word').textContent=w.word;this.el('study-answer').hidden=true;this.el('study-reveal').hidden=false;this.el('study-next').hidden=true;this.el('study-meaning').textContent=w.meaning||'';this.el('study-phonetic').textContent=w.phonetic||'';},
  revealStudy(){this.el('study-answer').hidden=false;this.el('study-reveal').hidden=true;this.el('study-next').hidden=false;},
  speakStudy(){const w=this.studyWord();if(w)speakWord(w.word);},
  nextStudy(){this.studyIndex++;this.renderStudy();},
  finishStudy(){this.el('study').hidden=true;this.message('背诵结束。可以只重测待背词，检查是否记住。');this.showResults();}
};
