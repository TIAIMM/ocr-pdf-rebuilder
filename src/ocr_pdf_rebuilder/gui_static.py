"""Bundled standalone page; no gateway, CDN or build step required."""

HTML_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>文字识别</title>
<style>
:root{color-scheme:light;--ink:#182c2b;--muted:#526663;--line:#d3e0dc;--brand:#176b5b}
*{box-sizing:border-box} [hidden]{display:none!important}
body{margin:0;background:#f2f6f4;color:var(--ink);font:16px/1.5 system-ui,"Microsoft YaHei",sans-serif}
button,select,input,textarea{font:inherit}button{cursor:pointer;border:1px solid var(--line);border-radius:7px;background:white;color:var(--ink);padding:7px 13px;min-height:40px}
button.primary{background:var(--brand);color:white;border-color:var(--brand)}button.danger{color:#a12828}
button:disabled{opacity:.55;cursor:default}:focus-visible{outline:3px solid #126aad;outline-offset:2px}
main{height:100dvh;min-height:560px;padding:16px;display:grid;grid-template-rows:auto auto minmax(0,1fr);gap:12px;max-width:1800px;margin:auto}
header{display:flex;justify-content:space-between;align-items:center;gap:12px}h1{font-size:23px;margin:0}h2{font-size:17px;margin:0}
.hint,.path{color:var(--muted);font-size:14px}.path{overflow-wrap:anywhere}
.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.status{background:#e0eee8;border-radius:20px;padding:4px 12px;font-size:14px}
#message{color:#a12828;font-size:14px;max-height:44px;overflow:auto}
.workspace{min-height:0;display:grid;grid-template-columns:minmax(290px,34%) minmax(0,1fr);gap:14px}
.card{border:1px solid var(--line);background:#fff;border-radius:12px;min-height:0;min-width:0}
.files{padding:16px;display:flex;flex-direction:column;gap:10px;overflow:auto}
label{display:block;font-size:14px;margin-bottom:4px}select,input,textarea{width:100%;padding:7px;border:1px solid #a9bdb4;border-radius:6px;background:white;color:var(--ink)}
textarea{resize:vertical;min-height:90px}.input-list{overflow:auto;min-height:80px;flex:1}
.file-choice{display:flex;align-items:flex-start;gap:8px;cursor:pointer;font-size:16px}.file-choice input{width:auto;margin-top:6px;flex:none}.file-choice span{min-width:0;overflow-wrap:anywhere}
.file-tools{display:flex;gap:10px;align-items:center}.file-tools label{margin:0}.start-area{margin-top:auto;display:grid;gap:6px}.start-area button{width:100%}
details{font-size:14px;color:var(--muted);margin-top:5px}summary{cursor:pointer;width:fit-content}details p{overflow-wrap:anywhere;white-space:pre-wrap;margin:5px 0}
ul{list-style:none;margin:0;padding:0}li{padding:9px 0;border-bottom:1px solid #e8eeeb;overflow-wrap:anywhere}
li .path{display:block}a{color:var(--brand);text-underline-offset:3px}.empty{color:var(--muted);padding:12px 0}
.right{display:grid;grid-template-rows:136px minmax(0,1fr);gap:12px;min-height:0}
.progress-card{padding:10px 16px;display:grid;grid-template-rows:auto auto minmax(0,1fr);gap:4px}
.progress-head{display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:14px}.progress-head button{min-height:32px;padding:4px 12px}
.progress-card progress{display:block;height:10px;margin-top:4px}
progress{width:100%;height:13px;accent-color:var(--brand)}#fileProgress{overflow:auto;font-size:14px}
.file-progress{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;border-top:1px solid #e8eeeb;padding:4px 0}.file-progress span:first-child{overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.details{display:flex;flex-direction:column;padding:0 12px 12px;min-height:0}
.tabs{display:flex;gap:6px;border-bottom:1px solid var(--line);padding:7px 0}
.tabs button{border:0;border-radius:5px;padding:5px 15px}.tabs [aria-selected=true]{background:#e0eee8;color:#125342;font-weight:650}
.panel{flex:1;min-height:0;overflow:auto;padding-top:8px}
#panel-logs{display:flex;flex-direction:column}pre{margin:0;flex:1;min-height:196px;overflow:auto;padding:10px 12px;background:#132521;color:#ecf5ef;border-radius:7px;font:14px/22px Consolas,"Microsoft YaHei",monospace;white-space:pre-wrap;overflow-wrap:anywhere}
.log-tools{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-bottom:6px;font-size:14px}.log-tools button{font-size:14px;min-height:32px;padding:3px 9px}.log-tools label{display:flex;align-items:center;gap:5px;margin:0}.log-tools input{width:auto}.log-tools [role=status]{color:var(--muted);font-size:13px}
.narrow-tabs{display:none}
dialog{position:fixed;margin:0 0 0 auto;inset:0 0 0 auto;height:100dvh;max-height:none;width:min(450px,95vw);max-width:95vw;border:0;border-left:1px solid var(--line);padding:22px;box-shadow:-8px 0 35px #0002;color:var(--ink)}
dialog::backdrop{background:#102a2644}dialog header{margin-bottom:24px}dialog p{overflow-wrap:anywhere}dialog h3{font-size:16px}
@media(min-width:900px){body{overflow:hidden}}
@media(max-width:899px){
 main{padding:10px;gap:8px}.narrow-tabs{display:flex;gap:6px}.narrow-tabs button{flex:1}
 .workspace{grid-template-columns:1fr}.workspace[data-view=files] .right{display:none}.workspace[data-view=monitor] .files{display:none}
 .right{grid-template-rows:136px minmax(0,1fr)}h1{font-size:19px}header .hint{display:none}
 #message{max-width:100%;margin:0}.toolbar{gap:6px}.toolbar button{padding:6px 10px}
 .tabs button{flex:1;padding:5px 8px}
}
@media(max-height:599px){body{overflow:auto}main{height:640px;min-height:640px}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
</style>
</head>
<body>
<main>
<header><div><h1>文字识别</h1><div class="hint">独立运行 · PDF 文件</div></div><div class="toolbar"><span id="status" class="status" role="status">连接中</span><button id="advanced" aria-haspopup="dialog">高级设置</button></div></header>
<div><div id="message" role="alert"></div>
<div class="narrow-tabs" role="tablist" aria-label="工作区"><button id="view-files" role="tab" aria-controls="files" aria-selected="false" tabindex="-1">文件与设置</button><button id="view-monitor" role="tab" aria-controls="monitor" aria-selected="true">进度与记录</button></div></div>
<section class="workspace" id="workspace" data-view="monitor">
<section class="card files" id="files" aria-label="文件与设置">
<h2>文件与设置</h2>
<div><label for="pipeline">识别管线</label><select id="pipeline"><option value="paddle">PaddleOCR-VL</option></select></div>
<div class="hint" id="sourceHint">文件来自 OCR 服务的默认输入目录，可勾选本次要识别的 PDF。</div>
<details><summary>查看输入位置</summary><p id="inputPath" class="path"></p></details>
<div class="file-tools"><label class="file-choice"><input id="selectAll" type="checkbox" checked><span>全选</span></label><span id="selectionCount" class="hint"></span></div>
<ul id="inputs" class="input-list"></ul>
<div class="start-area"><button id="start" class="primary" disabled>开始识别</button><div class="hint">关闭页面不会停止识别。停止后可复用检查点。</div></div>
</section>
<section class="right" id="monitor" aria-label="进度与记录">
<section class="card progress-card"><div class="progress-head"><h2>整体处理</h2><button id="stop" class="danger" disabled>停止识别</button></div><div><div id="totalText" class="hint">等待任务</div><progress id="totalProgress" max="100" value="0" aria-label="整体处理进度"></progress></div><div id="fileProgress"></div></section>
<section class="card details">
<div class="tabs" role="tablist" aria-label="任务记录"><button id="tab-logs" role="tab" aria-controls="panel-logs" aria-selected="true">实时日志</button><button id="tab-results" role="tab" aria-controls="panel-results" aria-selected="false" tabindex="-1">成果</button><button id="tab-history" role="tab" aria-controls="panel-history" aria-selected="false" tabindex="-1">历史</button></div>
<div class="panel" id="panel-logs" role="tabpanel" aria-labelledby="tab-logs"><div class="log-tools"><label title="仅暂停日志刷新，状态与进度继续更新"><input id="pauseLog" type="checkbox">暂停刷新</label><button id="copyLog" title="复制当前日志">复制</button><button id="latestLog">回到最新</button><span id="logNotice" role="status"></span></div><pre id="log" tabindex="0" aria-label="实时任务日志">等待任务启动……</pre></div>
<div class="panel" id="panel-results" role="tabpanel" aria-labelledby="tab-results" hidden><p class="hint">成果可下载，展开详情查看来源。</p><ul id="outputs"></ul></div>
<div class="panel" id="panel-history" role="tabpanel" aria-labelledby="tab-history" hidden><p class="hint">此 GUI 保留最近 30 次结束记录；历史记录不代表结果质量验收。</p><ul id="history"></ul></div>
</section>
</section>
</section>
</main>
<dialog id="advancedPanel" aria-labelledby="advancedTitle"><header><h2 id="advancedTitle">高级设置与运行信息</h2><button id="closeAdvanced" aria-label="关闭高级设置">关闭</button></header>
<label for="sourceMode">输入方式</label><select id="sourceMode"><option value="directory">默认目录勾选</option><option value="selected">手动输入路径</option></select>
<div id="selection" hidden><label for="inputFiles">PDF 完整路径，每行一个</label><textarea id="inputFiles" spellcheck="false"></textarea><p class="hint">使用 OCR 服务所在系统的路径；WSL 使用 /mnt/d/… 等路径。</p></div>
<label for="outputDir">输出目录（可选覆盖）</label><input id="outputDir" spellcheck="false" placeholder="留空自动分配"><p class="hint">勾选部分文件时自动创建独立输出目录。覆盖时须填写空目录的完整路径。</p>
<label for="refreshInterval">状态刷新间隔</label><select id="refreshInterval"><option value="1500">1.5 秒</option><option value="3000">3 秒</option><option value="5000">5 秒</option></select>
<details><summary>运行位置与任务身份</summary><p id="runtimePaths" class="path"></p><p id="identity" class="path"></p><p id="summary" class="path"></p></details>
<h3>检查点与恢复</h3><p>默认目录任务会复用完整性匹配的检查点。指定路径任务使用独立输出目录及来源清单。</p><p>停止识别任务会等待子进程清理；关闭 GUI 服务请使用桌面管理器。</p>
</dialog>
<script>
const csrf=__CSRF_TOKEN__;
const $=id=>document.getElementById(id);
const labels={idle:'空闲',starting:'正在启动',running:'正在识别',stopping:'正在安全停止',completed:'已完成',interrupted:'已停止',failed:'失败'};
let snapshot=null, logIdentity=null, logOffset=0, busy=false, polling=false, connectionError='', actionError='', followLog=true;
const signatures=new Map(), excluded=new Set();
const identity=s=>s.instance_id+':'+s.run_id;
const fmtSize=n=>n<1048576?(n/1024).toFixed(1)+' KB':(n/1048576).toFixed(1)+' MB';
const directoryFiles=s=>s.directory_inputs||(!s.external_run_id?s.inputs:[])||[];
const defaultOutput=s=>s.default_output_dirs?.[$('pipeline').value]||s.default_output_dir;
function message(){ $('message').textContent=connectionError||actionError; }
function stableUpdate(id,data,build){
 const signature=JSON.stringify(data);
 if(signatures.get(id)===signature)return;
 const target=$(id),focus=target.contains(document.activeElement)?document.activeElement.dataset.focusKey:null;
 const scrolls=[];for(let node=target;node;node=node.parentElement)scrolls.push([node,node.scrollTop,node.scrollLeft]);
 const opened=new Set([...target.querySelectorAll('details[open]')].map(d=>d.dataset.focusKey));
 build(target);signatures.set(id,signature);
 for(const d of target.querySelectorAll('details'))d.open=opened.has(d.dataset.focusKey);
 if(focus){const replacement=[...target.querySelectorAll('[data-focus-key]')].find(n=>n.dataset.focusKey===focus);replacement?.focus({preventScroll:true});}
 for(const [node,top,left] of scrolls){node.scrollTop=top;node.scrollLeft=left;}
}
function item(target,title,detail,url,key){
 const li=document.createElement('li'),name=document.createElement(url?'a':'span');name.textContent=title;
 if(url){name.href=url;name.dataset.focusKey='link:'+key;}
 const small=document.createElement('span');small.className='path';small.textContent=detail;li.append(name,small);target.append(li);return li;
}
function details(parent,key,text){
 const d=document.createElement('details'),summary=document.createElement('summary'),p=document.createElement('p');
 d.dataset.focusKey='details:'+key;summary.dataset.focusKey='summary:'+key;summary.textContent='查看详情';p.textContent=text;d.append(summary,p);parent.append(d);
}
function controls(){
 const s=snapshot,manual=$('sourceMode').value==='selected',files=s?directoryFiles(s):[];
 const count=files.filter(f=>!excluded.has(f.name)).length,locked=!s||s.running||busy||s.closing;
 $('start').disabled=locked||(!manual&&!count);
 $('stop').disabled=!s||busy||!s.running||s.status==='stopping';
 for(const id of ['pipeline','sourceMode','inputFiles','outputDir'])$(id).disabled=locked;
 $('selectAll').disabled=locked||manual||!files.length;
 $('selectAll').checked=!!files.length&&count===files.length;
 $('selectAll').indeterminate=count>0&&count<files.length;
 $('selectionCount').textContent=manual?'使用手动路径':count+'/'+files.length+' 个文件';
 $('sourceHint').textContent=manual?'已启用高级设置中的手动路径。':'文件来自 OCR 服务的默认输入目录，可勾选本次要识别的 PDF。';
 for(const box of $('inputs').querySelectorAll('input[type=checkbox]')){box.disabled=locked||manual;box.checked=!excluded.has(box.dataset.name);}
}
function render(s){
 $('status').textContent=labels[s.status]||s.status;
 const old=$('pipeline').value;
 stableUpdate('pipeline',s.pipelines,target=>{
  target.replaceChildren();for(const p of s.pipelines){const o=document.createElement('option');o.value=p.id;o.textContent=p.label;target.append(o);}
 });
 const pipeline=s.running?s.pipeline:(s.pipelines.some(p=>p.id===old)?old:s.pipeline);
 if($('pipeline').value!==pipeline)$('pipeline').value=pipeline;
 $('inputPath').textContent=s.input_dir;
 stableUpdate('inputs',directoryFiles(s),target=>{
  target.replaceChildren();for(const f of directoryFiles(s)){
   const li=document.createElement('li'),label=document.createElement('label'),box=document.createElement('input'),text=document.createElement('span');
   label.className='file-choice';box.type='checkbox';box.dataset.name=f.name;box.dataset.focusKey='input:'+f.name;
   box.onchange=()=>{if(box.checked)excluded.delete(f.name);else excluded.add(f.name);controls();};
   text.textContent=f.name;label.append(box,text);li.append(label);
   const size=document.createElement('span');size.className='path';size.textContent=fmtSize(f.size);li.append(size);target.append(li);
  }
  if(!directoryFiles(s).length)item(target,'暂无输入 PDF','将 PDF 放入默认目录，或在高级设置中输入路径。');
 });
 stableUpdate('outputs',s.outputs,target=>{
  target.replaceChildren();for(const f of s.outputs){const p=f.provenance||{};
   const li=item(target,f.name,fmtSize(f.size)+' · '+(f.current_run?'本次成果':p.status==='unverified'?'来源未核实':'历史成果'),f.download_url,f.path||f.name);
   details(li,f.path||f.name,'来源：'+(p.source_path||'未核实')+'\n产物：'+(f.path||'')+'\n来源状态：'+(p.status||'unverified')+(p.source_sha256?'\n来源 SHA-256：'+p.source_sha256:'')+(p.artifact_sha256?'\n成果 SHA-256：'+p.artifact_sha256:'')+(p.evidence_path?'\n清单：'+p.evidence_path:''));
  }
  if(!s.outputs.length)item(target,'暂无成果','识别成果将在这里显示。');
 });
 const files=s.file_progress||[],avg=files.length?files.reduce((n,f)=>n+Number(f.percent||0),0)/files.length:0;
 $('totalProgress').value=avg;$('totalText').textContent='整体处理 '+avg.toFixed(1)+'% · '+files.filter(f=>['completed','skipped'].includes(f.status)).length+'/'+files.length+' 文件';
 stableUpdate('fileProgress',files,target=>{
  target.replaceChildren();for(const f of files){const d=document.createElement('div');d.className='file-progress';const n=document.createElement('span'),v=document.createElement('span');n.textContent=f.name+' · '+(f.stage||'等待');n.title=n.textContent;v.textContent=Number(f.percent||0).toFixed(1)+'%'+(f.page_total?' · '+f.page_current+'/'+f.page_total+' 页':'');d.append(n,v);target.append(d);}
 });
 stableUpdate('history',s.history||[],target=>{
  target.replaceChildren();for(const h of s.history||[]){const key=(h.instance_id||h.service_instance_id||'')+':'+h.run_id;
   const li=item(target,(labels[h.status]||h.status)+' · '+h.pipeline+' · #'+h.run_id,new Date(h.started_at*1000).toLocaleString());
   details(li,key,'输出位置：'+h.output_dir);
  }
  if(!(s.history||[]).length)item(target,'暂无历史','识别结束后会保存记录。');
 });
 $('runtimePaths').textContent='输入：'+s.input_dir+'\n当前输出：'+s.output_dir+'\n默认输出：'+(defaultOutput(s)||'未提供');
 $('identity').textContent='服务：'+s.instance_id+' / 批次：'+s.run_id+(s.external_run_id?' / 外部任务：'+s.external_run_id:'');
 $('summary').textContent=s.summary?JSON.stringify(s.summary.counts||{},null,2):'暂无摘要';
 controls();
}
async function refresh(){
 if(polling)return;polling=true;
 try{
  const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error('状态读取失败');const s=await r.json();snapshot=s;render(s);
  if(logIdentity!==identity(s)){logIdentity=identity(s);logOffset=0;followLog=true;$('log').textContent='';$('log').scrollTop=0;}
  if($('pauseLog').checked){connectionError='';message();return;}
  const query=new URLSearchParams({offset:logOffset,expected_instance_id:s.instance_id,expected_run_id:s.run_id});
  const lr=await fetch('/api/log?'+query,{cache:'no-store'});if(!lr.ok)throw Error('日志读取失败');const l=await lr.json();
  if(identity(l)!==identity(s)){snapshot=null;connectionError='任务已切换，正在同步';controls();message();return;}
  const log=$('log'),top=log.scrollTop,left=log.scrollLeft;
  const follow=followLog;
  if(l.reset)log.textContent='';
  if(l.text){log.append(document.createTextNode(l.text));if(log.textContent.length>400000)log.textContent=log.textContent.slice(-400000);}
  log.scrollTop=follow?log.scrollHeight:(l.reset?0:top);log.scrollLeft=left;logOffset=l.next_offset;
  connectionError='';message();
 }catch(e){snapshot=null;connectionError='连接失败：'+e.message;$('status').textContent='连接失败';controls();message();}
 finally{polling=false;}
}
async function action(path,body){
 if(busy)return;busy=true;actionError='';message();controls();
 try{const r=await fetch(path,{method:'POST',headers:{'X-CSRF-Token':csrf,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined});const data=await r.json();if(!r.ok)throw Error(data.error||r.statusText);}
 catch(e){actionError=e.message;message();}
 finally{busy=false;await refresh();controls();}
}
$('start').onclick=()=>{
 if(!snapshot)return;
 const data={pipeline:$('pipeline').value},output=$('outputDir').value.trim();
 if($('sourceMode').value==='selected'){
  data.input_files=$('inputFiles').value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
  data.run_id='standalone-'+crypto.randomUUID();
  const base=defaultOutput(snapshot);
  if(!output&&!base){actionError='服务未提供默认输出目录，请在高级设置中指定。';message();return;}
  data.output_dir=output||base.replace(/[\\/]+$/,'')+'/runs/'+data.run_id;
 }else{
  const files=directoryFiles(snapshot),chosen=files.filter(f=>!excluded.has(f.name));
  if(!chosen.length)return;
  data.input_names=chosen.map(f=>f.name);if(output)data.output_dir=output;
 }
 action('/api/start',data);
 if(matchMedia('(max-width:899px)').matches)$('view-monitor').click();
};
$('stop').onclick=()=>{if(snapshot)action('/api/stop?'+new URLSearchParams({expected_instance_id:snapshot.instance_id,expected_run_id:snapshot.run_id}));};
$('selectAll').onchange=()=>{for(const f of directoryFiles(snapshot)){if($('selectAll').checked)excluded.delete(f.name);else excluded.add(f.name);}controls();};
$('sourceMode').onchange=()=>{$('selection').hidden=$('sourceMode').value!=='selected';controls();};
function tabGroup(ids,choose){
 const tabs=ids.map($);tabs.forEach((tab,index)=>{
  tab.onclick=()=>{tabs.forEach(t=>{const active=t===tab;t.setAttribute('aria-selected',String(active));t.tabIndex=active?0:-1;});choose(tab.id);};
  tab.onkeydown=e=>{let next=index;if(e.key==='ArrowRight')next=(index+1)%tabs.length;else if(e.key==='ArrowLeft')next=(index+tabs.length-1)%tabs.length;else if(e.key==='Home')next=0;else if(e.key==='End')next=tabs.length-1;else return;e.preventDefault();tabs[next].click();tabs[next].focus();};
 });
}
tabGroup(['tab-logs','tab-results','tab-history'],id=>{for(const key of ['logs','results','history'])$('panel-'+key).hidden=id!=='tab-'+key;});
tabGroup(['view-files','view-monitor'],id=>{$('workspace').dataset.view=id.slice(5);});
$('advanced').onclick=()=>$('advancedPanel').showModal();
$('closeAdvanced').onclick=()=>$('advancedPanel').close();
$('log').onscroll=()=>{followLog=$('log').scrollHeight-$('log').scrollTop-$('log').clientHeight<50;};
$('latestLog').onclick=()=>{followLog=true;$('log').scrollTop=$('log').scrollHeight;if(!$('pauseLog').checked)refresh();};
$('copyLog').onclick=async()=>{try{await navigator.clipboard.writeText($('log').textContent);$('logNotice').textContent='已复制';}catch(e){$('logNotice').textContent='复制失败，请选中日志手动复制';}};
let refreshTimer;
function refreshDelay(){return document.hidden?Math.max(15000,Number($('refreshInterval').value)):Number($('refreshInterval').value);}
function scheduleRefresh(){clearTimeout(refreshTimer);refreshTimer=setTimeout(async()=>{await refresh();scheduleRefresh();},refreshDelay());}
$('refreshInterval').onchange=scheduleRefresh;
$('pauseLog').onchange=()=>{if(!$('pauseLog').checked)refresh();};
document.addEventListener('visibilitychange',()=>{scheduleRefresh();if(!document.hidden)refresh();});
refresh();scheduleRefresh();
</script>
</body></html>
"""
