"use strict";
/* ---------- helpers ---------- */
const $=s=>document.querySelector(s);
const DISKNAMING={report:null,busy:false,overridePage:0,overrideOpen:false};
function toast(msg,cls){const d=document.createElement("div");d.className="toast "+(cls||"");
  d.textContent=msg;$("#toasts").append(d);setTimeout(()=>d.remove(),5000)}
let DEVICE_REQUEST_TIMEOUT_MS=15000;
const MOUNT_RUN_REQUEST_TIMEOUT_MS=300000;
let LINK_STATUS={ip:"",link_type:"unknown",label:"Unknown",addresses:[],ethernet_ip:"",wifi_ip:"",control_ip:"",control_link_type:"unknown",rest_via_alternate:false,rest_route_label:"",streaming_available:true,rest_timeout:8};
let UI_INTERACTIVE_DEPTH=0,INFO_IN_FLIGHT=false,DRIVES_IN_FLIGHT=false,INPUT_PROBE_TIMER=null;
function uiInteractiveStart(){UI_INTERACTIVE_DEPTH++}
function uiInteractiveEnd(){UI_INTERACTIVE_DEPTH=Math.max(0,UI_INTERACTIVE_DEPTH-1)}
function uiInteractive(){return UI_INTERACTIVE_DEPTH>0}
async function api(path,opts={}){
  const options={...opts},timeoutMs=Number(options.timeoutMs??DEVICE_REQUEST_TIMEOUT_MS);delete options.timeoutMs;
  let timer=null,controller=null;
  if(!options.signal&&timeoutMs>0){controller=new AbortController();options.signal=controller.signal;
    timer=setTimeout(()=>controller.abort(),timeoutMs)}
  try{
    const r=await fetch(path,options);
    let j=null;try{j=await r.clone().json()}catch(e){}
    if(!r.ok){const m=(j&&(j.detail||JSON.stringify(j.errors)))||r.statusText;throw new Error(m)}
    // Some successful endpoints return structured rows in an `errors` field
    // (for example Image Parse Details). Only plain strings are device errors.
    const deviceErrors=(j&&Array.isArray(j.errors))
      ?j.errors.filter(e=>typeof e==="string"&&e.trim())
      :[];
    if(deviceErrors.length)toast("Device: "+deviceErrors.join("; "),"err");
    return j;
  }catch(e){
    if(e.name==="AbortError")throw new Error(`Request timed out after ${Math.round(timeoutMs/1000)} seconds. The Ultimate may be busy; the UI remains available.`);
    throw e;
  }finally{if(timer)clearTimeout(timer)}
}
const put=(p,opts={})=>api(p,{...opts,method:"PUT"});

function showAppStopped(){
  document.body.innerHTML=`<main class="app-exit-screen"><div class="panel">
    <h1>u64deck has stopped.</h1>
    <p>The connected Ultimate is still running.</p>
    <p class="hint">You may close this window.</p>
  </div></main>`;
}
async function exitU64deck(){
  if(!confirm("Exit u64deck?\n\nThis stops the local u64deck server and closes its dedicated application window. The connected Ultimate will continue running."))return;
  const btn=$("#btnAppExit");if(btn){btn.disabled=true;btn.textContent="Exiting…"}
  try{
    const result=await api("/api/app/exit",{method:"POST",headers:{"X-U64deck-Local-Exit":"1"},timeoutMs:5000});
    showAppStopped();
    if(result?.close_window)setTimeout(()=>{try{window.close()}catch(e){}},250);
  }catch(e){
    if(btn){btn.disabled=false;btn.textContent="Exit u64deck"}
    toast(e.message,"err");
  }
}

/* ---------- global viewport-aware tooltips ---------- */
let TIP_TARGET=null;
function tooltipHide(){
  const tip=$("#globalTooltip");if(!tip)return;TIP_TARGET=null;tip.classList.remove("show");tip.setAttribute("aria-hidden","true");
}
function tooltipTarget(node){return node?.closest?.("[data-tip],[title]")||null}
function tooltipShow(target){
  const tip=$("#globalTooltip");if(!tip||!target)return;
  if(target.hasAttribute("title")){target.dataset.tip=target.getAttribute("title")||"";target.removeAttribute("title")}
  const text=(target.dataset.tip||"").trim();if(!text){tooltipHide();return}
  TIP_TARGET=target;tip.textContent=text;tip.classList.add("show");tip.setAttribute("aria-hidden","false");
  requestAnimationFrame(()=>{
    if(TIP_TARGET!==target)return;
    const r=target.getBoundingClientRect(),t=tip.getBoundingClientRect(),gap=8,pad=12;
    let left=Math.min(Math.max(pad,r.left),Math.max(pad,innerWidth-t.width-pad));
    let top=r.bottom+gap;
    if(top+t.height>innerHeight-pad)top=r.top-t.height-gap;
    top=Math.min(Math.max(pad,top),Math.max(pad,innerHeight-t.height-pad));
    tip.style.left=Math.round(left)+"px";tip.style.top=Math.round(top)+"px";
  });
}
document.addEventListener("pointerover",e=>{const t=tooltipTarget(e.target);if(t&&t!==TIP_TARGET)tooltipShow(t)});
document.addEventListener("pointerout",e=>{if(TIP_TARGET&&!TIP_TARGET.contains(e.relatedTarget))tooltipHide()});
document.addEventListener("focusin",e=>{const t=tooltipTarget(e.target);if(t)tooltipShow(t)});
document.addEventListener("focusout",e=>{if(TIP_TARGET&&!TIP_TARGET.contains(e.relatedTarget))tooltipHide()});
document.addEventListener("pointerdown",tooltipHide,true);
document.addEventListener("scroll",tooltipHide,true);
window.addEventListener("resize",tooltipHide);
document.addEventListener("keydown",e=>{if(e.key==="Escape")tooltipHide()});
function tab(name){
  tooltipHide();
  if(name!=="screen"&&document.querySelector("#tab-screen.active"))matrixReleaseAll("tab switch");
  document.querySelectorAll("nav button").forEach(b=>b.classList.toggle("active",b.dataset.tab===name));
  document.querySelectorAll("section").forEach(s=>s.classList.toggle("active",s.id==="tab-"+name));
  if(name==="home")itemsLoad();
  if(name==="settings"){if(!SET.loaded)loadCats();sidflowStatusLoad()}
  if(name==="disks"&&!FS.loaded){fsGo("/");refreshDrives()}
  if(name==="asm64")asmFormInit();
  if(name==="health")healthStart();else healthStop();
  if(name==="sid"){jkRefresh();jkPollStart();jkSidIndexInit();sidflowStatusLoad();
    // (re)browse whenever the folder list isn't populated — a failed first
    // attempt (device busy, FTP hiccup) must not leave the browser dead
    if(!document.querySelector("#jkBDirs button:not([data-retry])")&&!JK.homing){jkHome()}}
}

/* ---------- favourites / recent items ---------- */
const ITEMS={favorites:[],recents:[],loaded:false};
function itemSpec(type,label,detail,action,payload){return {type,label,detail:detail||"",action,payload:payload||{}}}
function stableObject(value){
  if(Array.isArray(value))return value.map(stableObject);
  if(value&&typeof value==="object")return Object.fromEntries(Object.keys(value).sort().map(k=>[k,stableObject(value[k])]));
  return value;
}
function itemKey(item){return [item.type,item.action,JSON.stringify(stableObject(item.payload||{}))].join("|")}
function favMatch(item){const key=itemKey(item);return ITEMS.favorites.find(x=>itemKey(x)===key)||null}
function itemArg(item){return jsq(JSON.stringify(item))}
function starButton(item,title="Toggle favourite"){
  const on=!!favMatch(item),key=esc(itemKey(item));
  return `<button class="mini star ${on?"on":""}" data-item-key="${key}" onclick="event.stopPropagation();toggleFavorite(JSON.parse('${itemArg(item)}'))" title="${title}">${on?"★":"☆"}</button>`;
}
function refreshStarButtons(){
  document.querySelectorAll("button.star[data-item-key]").forEach(btn=>{
    const on=ITEMS.favorites.some(x=>itemKey(x)===btn.dataset.itemKey);
    btn.classList.toggle("on",on);btn.textContent=on?"★":"☆";
  });
}
async function itemsLoad(){
  try{const r=await api("/api/user_items");ITEMS.favorites=r.favorites||[];ITEMS.recents=r.recents||[];ITEMS.loaded=true;itemsRender();refreshStarButtons()}
  catch(e){if($("#favList"))$("#favList").innerHTML=`<span style="color:var(--err)">${esc(e.message)}</span>`}
}
async function toggleFavorite(item){
  const existing=favMatch(item);
  try{
    if(existing)await api("/api/user_items/favorite?item_id="+encodeURIComponent(existing.id),{method:"DELETE"});
    else await api("/api/user_items/favorite",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(item)});
    await itemsLoad();
    // Update stars in place: never rebuild a long search result or jump its scroll position.
    refreshStarButtons();
    toast(existing?"Removed from favourites":"Added to favourites ✓","ok");
  }catch(e){toast(e.message,"err")}
}
async function rememberRecent(item){
  try{
    const r=await api("/api/user_items/recent",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(item)});
    const saved=r.recent||item;ITEMS.recents=[saved,...ITEMS.recents.filter(x=>itemKey(x)!==itemKey(saved))].slice(0,60);
    if(document.querySelector("#tab-home.active"))itemsRender();
  }catch(e){}
}
const ITEM_ICONS={folder:"📁",disk:"💾",disk_entry:"▤",program:"▶",assembly64:"A64",sid:"♪",sid_folder:"♫",library:"⚡"};
function itemTypeLabel(type){return ({folder:"Folder",disk:"Disk image",disk_entry:"File in disk",program:"Program",assembly64:"Assembly64",sid:"SID tune",sid_folder:"SID folder",library:"Quick Launch"})[type]||type}
function itemActionLabel(action){return ({fs_browse:"Open Folder",disk_run:`Mount & Run · ${mountModeShort()}`,disk_entry_run:"Mount & Load",disk_entry_dma:"Run Instantly",disk_entry_open:"Open Image",program_run:"Run",assembly_open:"Release Files",sid_play:"Play",sid_folder:"Play Folder",library_run:"Launch"})[action]||"Open"}
function itemCard(item,favouriteList=false){
  const encoded=itemArg(item);
  const star=favouriteList
    ?`<button class="mini star on" onclick="event.stopPropagation();toggleFavorite(JSON.parse('${encoded}'))" title="Remove from favourites">★</button>`
    :starButton(item,"Add/remove favourite");
  const sidQueue=(item.type==="sid"&&item.action==="sid_play")
    ?`<button class="mini" onclick="event.stopPropagation();queueSavedSid(JSON.parse('${encoded}'))" title="Add to the SID play queue without interrupting playback">＋</button>`:"";
  return `<div class="item-card">
    <div class="item-icon">${ITEM_ICONS[item.type]||"•"}</div>
    <div class="item-main" title="${esc(item.detail||item.label)}">
      <div class="item-label"><b>${esc(item.label)}</b> <span class="hint">${esc(itemTypeLabel(item.type))}</span></div>
      <div class="item-detail hint">${esc(item.detail||"")}</div>
    </div>
    ${sidQueue}<button class="mini primary" onclick="runSavedItem(JSON.parse('${encoded}'))">${itemActionLabel(item.action)}</button>${star}</div>`;
}
function itemsRender(){
  if(!$("#favList"))return;
  const type=$("#itemTypeFilter")?.value||"";
  const favs=ITEMS.favorites.filter(x=>!type||x.type===type);
  const recents=ITEMS.recents.filter(x=>!type||x.type===type);
  $("#favCount").textContent=`(${favs.length})`;$("#recentCount").textContent=`(${recents.length})`;
  $("#favList").innerHTML=favs.length?favs.map(x=>itemCard(x,true)).join(""):'<span class="hint">No favourites yet — use ☆ beside an item.</span>';
  $("#recentList").innerHTML=recents.length?recents.map(x=>itemCard(x,false)).join(""):'<span class="hint">Nothing used yet.</span>';
}
async function clearRecents(){
  if(!ITEMS.recents.length||!confirm("Clear the recently used list?"))return;
  try{await api("/api/user_items/recent",{method:"DELETE"});ITEMS.recents=[];itemsRender()}catch(e){toast(e.message,"err")}
}
async function queueSavedSid(item){
  const p=item?.payload||{};
  if(!p.name){toast("This saved SID no longer has a queueable filename","err");return}
  return jkAdd(p.folder||"/",p.name);
}
async function runSavedItem(item){
  await rememberRecent(item);
  const p=item.payload||{};
  if(item.action==="fs_browse"){tab("disks");return fsGo(p.path||"/")}
  if(item.action==="disk_run")return mountRunDevice(p.path);
  if(item.action==="disk_entry_run"||item.action==="disk_entry_dma"||item.action==="disk_entry_open"){
    tab("disks");const opened=await imgOpenDevice(p.path);if(!opened)return;
    const file=(opened.files||[]).find(f=>f.name===p.name&&f.type===p.file_type)||(opened.files||[]).find(f=>f.index===p.index);
    if(!file){toast("The saved file is no longer present in that image","err");return}
    if(item.action==="disk_entry_run"&&file.type==="PRG")return inspMountLoad(file.index);
    if(item.action==="disk_entry_dma"&&file.type==="PRG")return inspRun(file.index);
    const row=document.querySelector(`#inspFiles tr[data-index="${file.index}"]`);if(row){row.classList.add("sel");row.scrollIntoView({block:"center"})}
    return;
  }
  if(item.action==="program_run")return runDevice(p.path);
  if(item.action==="assembly_open"){
    tab("asm64");await asmFormInit();ASM.results=[p.entry];
    $("#asmResTitle").textContent=`FAVOURITE — ${p.entry?.name||item.label}`;
    asmRenderResults();return asmFiles(0);
  }
  if(item.action==="sid_play"){tab("sid");return jkPlayFrom(p.folder,p.name)}
  if(item.action==="sid_folder"){tab("sid");return jukeFolder(p.path)}
  if(item.action==="library_run")return qlRun(p.name);
  toast("This saved item type is not supported by this build","err");
}

/* ---------- help and diagnostics ---------- */
let HELP_ACTIVE="start";
function openHelp(topic="start"){
  HELP_ACTIVE=topic||HELP_ACTIVE;$("#helpOverlay").style.display="block";$("#helpSearch").value="";
  $("#helpVersion").textContent=$("#ver")?.textContent||"";renderHelp();setTimeout(()=>$("#helpSearch").focus(),0);
}
function closeHelp(){$("#helpOverlay").style.display="none";$("#helpBtn")?.focus()}
function renderHelp(){
  const all=window.HELP_SECTIONS||[],q=($("#helpSearch")?.value||"").trim().toLowerCase();
  const filtered=q?all.filter(s=>(s.title+" "+s.body.replace(/<[^>]+>/g," ")).toLowerCase().includes(q)):all;
  if(filtered.length&&!filtered.some(s=>s.id===HELP_ACTIVE))HELP_ACTIVE=filtered[0].id;
  $("#helpNav").innerHTML=filtered.map(s=>`<button class="${s.id===HELP_ACTIVE?"active":""}" onclick="HELP_ACTIVE='${s.id}';renderHelp()">${esc(s.title)}</button>`).join("");
  const active=filtered.find(s=>s.id===HELP_ACTIVE);
  $("#helpContent").innerHTML=active?`<h2>${esc(active.title)}</h2>${active.body}`:'<div class="help-empty">No help topics match that search.</div>';
}
document.addEventListener("keydown",e=>{if(e.key==="Escape"&&$("#helpOverlay")?.style.display!=="none"){e.preventDefault();closeHelp()}});
function diagnosticsSuggestedName(){
  const d=new Date(),pad=n=>String(n).padStart(2,"0");
  return `u64deck-diagnostics-${d.getFullYear()}${pad(d.getMonth()+1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}.zip`;
}
async function downloadDiagnostics(){
  let handle=null,writable=null;
  try{
    // Ask for the destination while this function still has the original click
    // activation. The writable stream is explicitly committed and closed after
    // the diagnostics ZIP has been received.
    if(window.showSaveFilePicker){
      try{handle=await showSaveFilePicker({suggestedName:diagnosticsSuggestedName(),types:[{description:"ZIP archive",accept:{"application/zip":[".zip"]}}]})}
      catch(e){if(e.name==="AbortError")return;throw e}
    }
    toast("Building sanitised diagnostics…","ok");
    const payload={browser:{userAgent:navigator.userAgent,language:navigator.language,platform:navigator.platform,
      hardwareConcurrency:navigator.hardwareConcurrency||"",mediaRecorder:typeof MediaRecorder!=="undefined",
      showSaveFilePicker:typeof showSaveFilePicker!=="undefined",location:location.origin,recording:recSettings(),
      recordingSupport:{mp4:!!chooseRecordingFormat("combined","mp4"),webm:!!chooseRecordingFormat("combined","webm"),
        selected:chooseRecordingFormat(recSettings().mode,recSettings().format)?.mime||"unsupported"}}};
    const r=await fetch("/api/diagnostics/export",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    if(!r.ok){let j={};try{j=await r.json()}catch(e){}throw new Error(j.detail||r.statusText)}
    const blob=await r.blob(),cd=r.headers.get("content-disposition")||"",m=cd.match(/filename="([^"]+)"/),name=m?m[1]:diagnosticsSuggestedName();
    if(handle){
      try{writable=await handle.createWritable();await writable.write(blob);await writable.close();writable=null}
      catch(e){
        if(writable){try{if(typeof writable.abort==="function")await writable.abort();else await writable.close()}catch(closeError){}writable=null}
        throw e;
      }
    }else{
      const url=URL.createObjectURL(blob),a=document.createElement("a");a.href=url;a.download=name;
      document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
    }
    toast("Diagnostics exported ✓","ok");
  }catch(e){
    if(writable){try{if(typeof writable.abort==="function")await writable.abort();else await writable.close()}catch(closeError){}}
    toast(e.message,"err");
  }
}

/* ---------- system health ---------- */
const HEALTH={timer:null,busy:false,last:null};
function healthBytes(value){
  const n=Number(value);if(!Number.isFinite(n)||n<0)return"—";
  const units=["B","KiB","MiB","GiB","TiB"];let v=n,u=0;
  while(v>=1024&&u<units.length-1){v/=1024;u++}
  return (u===0?Math.round(v):v.toFixed(v>=100?0:v>=10?1:2))+" "+units[u];
}
function healthDuration(value){
  let s=Math.max(0,Math.round(Number(value)||0));const d=Math.floor(s/86400);s%=86400;
  const h=Math.floor(s/3600);s%=3600;const m=Math.floor(s/60);s%=60;
  return [d?d+"d":"",h?h+"h":"",m?m+"m":"",(!d&&!h||s)?s+"s":""].filter(Boolean).join(" ");
}
function healthAge(value){
  if(value==null||!Number.isFinite(Number(value)))return"Never";
  const n=Number(value);return n<1?"Now":healthDuration(n)+" ago";
}
function healthRate(value){
  const n=Number(value);if(!Number.isFinite(n))return"—";
  if(n>=1000000)return(n/1000000).toFixed(2)+" Mbit/s";
  if(n>=1000)return(n/1000).toFixed(1)+" kbit/s";
  return Math.round(n)+" bit/s";
}
function healthText(id,value){const el=$(id);if(el)el.textContent=value==null?"—":String(value)}
function healthStatusLabel(value){
  const acronyms=new Set(["EXE","CPU","RAM","REST","FPGA","I/O","WS"]);
  return String(value??"").replaceAll("_"," ").trim().split(/\s+/).filter(Boolean).map(word=>{
    const upper=word.toUpperCase();if(acronyms.has(upper))return upper;
    return word.charAt(0).toUpperCase()+word.slice(1).toLowerCase();
  }).join(" ");
}
function healthBadge(id,text,on=false,bad=false,warn=false){const el=$(id);if(!el)return;el.textContent=healthStatusLabel(text);el.className="badge"+(on?" on":"")+(bad?" bad":"")+(warn?" warn":"")}
function healthBrowserPayload(){return {
  video_render_fps:HEALTH_BROWSER.videoRenderFps,video_frames_total:HEALTH_BROWSER.videoFramesTotal,
  video_ws_connects:HEALTH_BROWSER.videoWsConnects,video_ws_disconnects:HEALTH_BROWSER.videoWsDisconnects,
  audio_ws_connects:HEALTH_BROWSER.audioWsConnects,audio_ws_disconnects:HEALTH_BROWSER.audioWsDisconnects,
  audio_reconnects:HEALTH_BROWSER.audioReconnects,audio_underruns:HEALTH_BROWSER.audioUnderruns,
  audio_dropped_ahead:HEALTH_BROWSER.audioDroppedAhead,
  audio_queue_ms:actx?Math.max(0,(nextT-actx.currentTime)*1000):0,
  audio_context_state:actx?actx.state:"unavailable",page_visible:!document.hidden};}
async function healthReportBrowser(){try{await api("/api/health/browser",{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify(healthBrowserPayload()),timeoutMs:1500})}catch(e){}}
function healthEscape(value){return String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function healthClock(value){const n=Number(value);return Number.isFinite(n)&&n>0?new Date(n*1000).toLocaleTimeString():"—"}
function healthMs(value,digits=1){const n=Number(value);return Number.isFinite(n)?n.toFixed(digits)+" ms":"—"}
function healthNumber(value){const n=Number(value);return Number.isFinite(n)?n.toLocaleString():"—"}
function healthHistory(container,rows,renderer){const el=$(container);if(!el)return;el.innerHTML=rows.length?rows.map(renderer).join(""):'<div class="hint">No events recorded yet.</div>'}
function healthRender(h){
  HEALTH.last=h;
  const u=h.ultimate||{},d=u.device||{},link=u.link||{},streams=h.streams||{},browser=streams.browser||{};
  const proc=h.u64deck?.process||{},host=proc.host||{},q=h.coordinator||{},idx=h.index||{},job=idx.job||{},cache=h.cache||{},diag=h.diagnostics||{};
  const activity=h.activity?.current||{},persist=h.persistence||{},config=persist.config||{},routes=persist.routes||[],shutdown=persist.shutdown||{};

  const summary=h.summary||{state:"neutral",label:"WAITING",reasons:[]},summaryEl=$("#healthSummary");
  if(summaryEl)summaryEl.className="health-summary state-"+(summary.state||"neutral");
  healthText("#healthSummaryBadge",healthStatusLabel(summary.label||"WAITING"));
  healthText("#healthSummaryTitle",summary.state==="healthy"?"Everything looks healthy":summary.state==="degraded"?"Some telemetry deserves attention":summary.state==="attention"?"Action may be required":"Waiting for telemetry");
  healthText("#healthSummaryReasons",(summary.reasons||[]).join(" · ")||"—");

  const fresh=u.last_success_age_seconds!=null&&u.last_success_age_seconds<65;
  const ultimateWarn=!!u.consecutive_failures||(!fresh&&u.configured);
  healthBadge("#healthUltimateState",!u.configured?"not configured":fresh?"online":"stale",fresh,!u.configured,ultimateWarn);
  healthText("#healthUltimateName",u.configured?(d.product||"Ultimate")+(d.hostname?" · "+d.hostname:""):"Not configured");
  healthText("#healthUltimateVersions",[d.firmware_version?"FW "+d.firmware_version:"",d.core_version?"Core "+d.core_version:"",d.fpga_version?"FPGA "+d.fpga_version:""].filter(Boolean).join(" · ")||"No successful status response yet");
  healthText("#healthUltimateRoutes",`${link.label||"Unknown"} / ${link.rest_route_label||link.control_ip||"—"}`);
  healthText("#healthRestLatestP95",`${healthMs(u.latest_ms)} / ${healthMs(u.p95_ms)}`);
  healthText("#healthRestRange",u.samples?`${Number(u.minimum_ms).toFixed(1)} / ${Number(u.average_ms).toFixed(1)} / ${Number(u.maximum_ms).toFixed(1)} ms`:"—");
  healthText("#healthRestSuccess",u.success_rate_percent==null?"—":`${Number(u.success_rate_percent).toFixed(2)}% (${healthNumber(u.successes)}/${healthNumber(u.attempts)})`);
  healthText("#healthRestFailures",`${healthNumber(u.failures||0)} / ${healthNumber(u.consecutive_failures||0)}`);
  healthText("#healthLastResponse",healthAge(u.last_success_age_seconds));
  healthText("#healthRestReconnects",healthNumber(u.client_replacements||0));
  healthText("#healthRestSamples",healthNumber(u.samples||0));
  const ue=$("#healthUltimateError");if(ue){ue.style.display=u.last_error?"block":"none";ue.textContent=u.last_error?`Last REST error (${healthAge(u.last_error_age_seconds)}): ${u.last_error}`:""}

  healthText("#healthStreamTransport","Transport: "+(streams.transport||"—"));
  const videoState=streams.video||{},audioState=streams.audio||{};
  const streamRows=[videoState,audioState],commanded=streamRows.filter(row=>!!row.commanded_on).length;
  const receiving=streamRows.filter(row=>row.commanded_on&&row.packet_age_seconds!=null&&row.packet_age_seconds<2).length;
  const recentGaps=streamRows.filter(row=>row.recent_gap_age_seconds!=null&&Number(row.recent_gap_age_seconds)<=60).length;
  const streamBad=commanded>0&&receiving<commanded,streamWarn=!streamBad&&recentGaps>0;
  const streamLabel=commanded===0?"off":streamBad?(receiving?"partial":"stale"):streamWarn?"recent gap":commanded===2?"both receiving":"receiving";
  healthBadge("#healthStreamsState",streamLabel,commanded>0&&!streamBad&&!streamWarn,streamBad,streamWarn);
  healthText("#healthVideoRate",healthRate(videoState.bitrate_bps));
  healthText("#healthAudioRate",healthRate(audioState.bitrate_bps));
  healthText("#healthVideoFps",`${streams.video?.frames_per_second==null?"—":Number(streams.video.frames_per_second).toFixed(1)} / ${browser.video_render_fps==null?"—":Number(browser.video_render_fps).toFixed(1)} fps`);
  healthText("#healthVideoGaps",`${healthNumber(streams.video?.dropped||0)} / ${healthNumber(streams.video?.gap_events||0)}`);
  healthText("#healthVideoLongestGap",`${healthNumber(streams.video?.longest_gap_packets||0)} pkt · ${healthMs(streams.video?.longest_inter_packet_ms)}`);
  healthText("#healthVideoAge",`${streams.video?.packet_age_seconds==null?"—":healthAge(streams.video.packet_age_seconds)} / ${streams.video?.peak_packet_age_seconds==null?"—":healthDuration(streams.video.peak_packet_age_seconds)}`);
  healthText("#healthVideoRestarts",`${healthNumber(streams.video?.start_count||0)} / ${healthNumber(streams.video?.restart_count||0)}`);
  healthText("#healthVideoWs",`${healthNumber(browser.video_ws_connects??streams.video?.websocket?.connections??0)} / ${healthNumber(browser.video_ws_disconnects??streams.video?.websocket?.disconnects??0)}`);
  healthText("#healthVideoFrames",healthNumber(browser.video_frames_total??streams.video?.frames??0));
  healthText("#healthAudioPps",streams.audio?.packets_per_second==null?"—":Number(streams.audio.packets_per_second).toFixed(1)+" pkt/s");
  healthText("#healthAudioGaps",`${healthNumber(streams.audio?.dropped||0)} / ${healthNumber(streams.audio?.gap_events||0)}`);
  healthText("#healthAudioLongestGap",`${healthNumber(streams.audio?.longest_gap_packets||0)} pkt · ${healthMs(streams.audio?.longest_inter_packet_ms)}`);
  healthText("#healthAudioAge",`${streams.audio?.packet_age_seconds==null?"—":healthAge(streams.audio.packet_age_seconds)} / ${streams.audio?.peak_packet_age_seconds==null?"—":healthDuration(streams.audio.peak_packet_age_seconds)}`);
  healthText("#healthAudioQueue",`${browser.audio_queue_ms==null?"—":Number(browser.audio_queue_ms).toFixed(0)+" ms"} / ${healthNumber(browser.audio_underruns||0)} underrun`+(Number(browser.audio_underruns||0)===1?"":"s"));
  healthText("#healthAudioReconnects",`${healthNumber(browser.audio_reconnects||0)} / ${healthNumber(browser.audio_ws_disconnects??streams.audio?.websocket?.disconnects??0)}`);
  healthText("#healthAudioContext",browser.audio_context_state?healthStatusLabel(browser.audio_context_state):"—");

  const active=!!q.active_priority,waiters=(q.waiting_interactive||0)+(q.waiting_status||0)+(q.waiting_background||0),task=activity.name||"";
  healthText("#healthActivityName",task||"Idle");
  healthText("#healthActivityDetail",task?(activity.detail||"Operation in progress."):"No operation in progress.");
  healthText("#healthActivityPhase",activity.phase||"—");
  healthText("#healthActivityElapsed",activity.elapsed_seconds==null?"—":healthDuration(activity.elapsed_seconds));
  healthText("#healthActiveOperation",active?`${q.active_reason||q.active_priority}${q.active_seconds?` · ${Number(q.active_seconds).toFixed(1)}s`:""}`:"Idle");
  healthText("#healthWaiters",`${q.waiting_interactive||0} / ${q.waiting_status||0} / ${q.waiting_background||0}`);
  healthText("#healthQueueWaitStats",`${Number(q.average_wait_seconds||0).toFixed(3)} / ${Number(q.p95_wait_seconds||0).toFixed(3)} / ${Number(q.longest_wait_seconds||0).toFixed(3)} s`);
  const by=q.completed_by_priority||{};healthText("#healthCompletedByPriority",`${by.interactive||0} / ${by.status||0} / ${by.background||0}`);
  healthText("#healthCancelled",`${healthNumber(q.cancelled||0)} / ${healthNumber(q.expired||0)}`);
  healthText("#healthQueueWait",Number(q.total_wait_seconds||0).toFixed(2)+" s");

  const indexRunning=!!job.running,indexBad=!!job.error||Number(job.scan_errors||0)>0;
  healthText("#healthIndexCurrent",indexRunning?(job.mode==="local"?"Local Volume Index":"Ultimate Storage Index"):"Indexer Idle");
  healthText("#healthIndexDetail",indexRunning?(job.current||job.root||"Scanning"):(job.error||"No indexing operation is active."));
  healthText("#healthIndexCounts",`${healthNumber(idx.images)} / ${healthNumber(idx.sid_metadata)}`);
  healthText("#healthIndexDatabase",`${healthBytes(idx.disk_bytes)} / ${healthNumber(idx.parse_failures)}`);
  healthText("#healthIndexRates",`${Number(job.files_per_sec||0).toFixed(2)} / ${Number(job.dirs_per_sec||0).toFixed(2)}`);
  healthText("#healthIndexEta",`${Number(job.images_per_sec||0).toFixed(2)} img/s / ${job.eta_secs==null?"—":healthDuration(job.eta_secs)}`);
  healthText("#healthIndexChanges",`${healthNumber(job.images_new||0)} / ${healthNumber(job.images_changed||0)} / ${healthNumber(job.scan_errors||0)}`);
  healthText("#healthImageCache",`${healthNumber(cache.image_entries||0)}/${healthNumber(cache.image_capacity||0)} · ${healthBytes(cache.image_bytes)}`);
  healthText("#healthImageCacheHits",cache.image_hit_rate_percent==null?`${healthNumber(cache.image_hits||0)} hit / ${healthNumber(cache.image_misses||0)} miss`:`${Number(cache.image_hit_rate_percent).toFixed(1)}% · ${healthNumber(cache.image_evictions||0)} evicted`);
  healthText("#healthSidCacheHits",cache.sid_hit_rate_percent==null?`${healthNumber(cache.sid_materialise_hits||0)} hit / ${healthNumber(cache.sid_materialise_misses||0)} fetch`:`${Number(cache.sid_hit_rate_percent).toFixed(1)}% · ${healthNumber(cache.sid_materialise_misses||0)} fetch`);
  const sf=idx.sidflow||{};
  healthText("#healthSidflowModel",sf.available?`${sf.release||sf.supported_release||"—"} · ${sf.hvsc_version||"HVSC ?"} · ${sf.similarity_metric||"—"} · ${sf.vector_dimensions||"—"}D`:(sf.needs_update?"Update Required":"Not Installed"));
  healthText("#healthSidflowCounts",sf.available?`${healthNumber(sf.tracks||0)} / ${healthNumber(sf.neighbors||0)}`:"—");

  healthBadge("#healthFrozen",h.u64deck?.frozen?"EXE":"source",true,false);
  healthText("#healthProcessLoad",`${proc.cpu_percent==null?"—":Number(proc.cpu_percent).toFixed(1)+"%"} / ${healthBytes(proc.memory_rss_bytes)}`);
  healthText("#healthProcessShape",`${proc.threads==null?"—":proc.threads} / ${proc.handles==null?"—":proc.handles}`);
  healthText("#healthProcessUptime",healthDuration(proc.uptime_seconds));
  healthText("#healthHostLoad",host.cpu_percent==null?"—":`${Number(host.cpu_percent).toFixed(1)}% / ${Number(host.memory_percent).toFixed(1)}%`);
  healthText("#healthDiskFree",healthBytes(host.disk_free_bytes));
  healthText("#healthProcessIo",`${healthBytes(proc.read_bytes)} / ${healthBytes(proc.write_bytes)}`);
  healthText("#healthBuild",h.u64deck?.build||"—");
  healthText("#healthDiagCounts",`${healthNumber(diag.warnings||0)} / ${healthNumber(diag.errors||0)}`);
  healthText("#healthDiagActive",`${healthNumber(diag.active_warnings||0)} / ${healthNumber(diag.active_errors||0)}`);
  healthText("#healthLastErrorAge",healthAge(diag.last_error_age_seconds));

  const lastConfig=config.last||{},lastRoute=routes.length?routes[routes.length-1]:null;
  const latestConfigFailed=!!lastConfig&&lastConfig.success===false;
  healthBadge("#healthPersistenceState",latestConfigFailed?"save failed":"ready",!latestConfigFailed,latestConfigFailed);
  healthText("#healthConfigSaves",`${healthNumber(config.successes||0)} / ${healthNumber(config.failures||0)}`);
  healthText("#healthConfigLast",lastConfig.time?`${healthClock(lastConfig.time)} · ${healthMs(lastConfig.elapsed_ms,2)}`:"—");
  healthText("#healthRouteChanges",healthNumber(routes.length));
  healthText("#healthLastRoute",lastRoute?`${lastRoute.selected_host||"—"} → ${lastRoute.control_host||"—"}`:"—");
  healthText("#healthShutdownState",healthStatusLabel(shutdown.state||"not requested"));
  healthText("#healthShutdownTime",shutdown.total_ms==null?"—":healthMs(shutdown.total_ms,2));
  healthText("#healthDiagRetained",healthNumber(diag.retained||0));
  healthText("#healthLastEvent",diag.last?`${healthStatusLabel(diag.last.level)}: ${diag.last.message}`:"—");

  const diagnosticEvents=diag.events||[];healthText("#healthDiagnosticHistoryCount",diagnosticEvents.length);
  healthHistory("#healthDiagnosticHistory",[...diagnosticEvents].reverse(),row=>{const level=row.level==="error"?"bad":"warn",symbol=row.level==="error"?"×":"!",state=row.recovered?"recovered":"active";return `<div class="health-history-row"><span class="health-history-time">${healthClock(row.time_epoch)}</span><span class="health-outcome ${level}" title="${healthEscape(healthStatusLabel(row.level))}">${symbol}</span><b>${healthEscape(row.message)}</b><span class="health-event-state ${state}">${row.recovered?"Recovered / Historical":"Recent / Unresolved"} · ${healthAge(row.age_seconds)}</span></div>`});

  const ops=q.recent_operations||[];healthText("#healthOpHistoryCount",ops.length);
  healthHistory("#healthOperationHistory",[...ops].reverse(),row=>{const intentional=row.outcome==="cancelled"||row.outcome==="expired",state=row.outcome==="ok"?"ok":intentional?"warn":"bad",symbol=row.outcome==="ok"?"✓":intentional?"!":"×";return `<div class="health-history-row"><span class="health-history-time">${healthClock(row.finished_at)}</span><span class="health-outcome ${state}" title="${healthEscape(healthStatusLabel(row.outcome))}">${symbol}</span><b>${healthEscape(row.reason)}</b><span>${healthEscape(healthStatusLabel(row.priority))} · Wait ${Number(row.wait_seconds||0).toFixed(3)}s · Run ${Number(row.duration_seconds||0).toFixed(3)}s</span></div>`});
  const streamHistory=streams.history||[];healthText("#healthStreamHistoryCount",streamHistory.length);
  healthHistory("#healthStreamHistory",[...streamHistory].reverse(),row=>`<div class="health-history-row"><span class="health-history-time">${healthClock(row.time)}</span><span class="badge ${row.ok===false?"bad":row.action?.includes("close")?"warn":"on"}">${healthEscape(healthStatusLabel(row.stream))}</span><b>${healthEscape(healthStatusLabel(row.action))}</b><span>${healthEscape(row.via||row.transport||row.destination||"")}</span></div>`);
  const life=[...(h.activity?.history||[]).map(row=>({...row,_kind:"activity",time:row.finished_at||row.started_at})),...(config.history||[]).map(row=>({...row,_kind:"config"})),...routes.map(row=>({...row,_kind:"route"}))].sort((a,b)=>(b.time||0)-(a.time||0)).slice(0,30);
  healthText("#healthLifecycleHistoryCount",life.length);
  healthHistory("#healthLifecycleHistory",life,row=>{let title="",detail="",state="on";if(row._kind==="route"){title="Route";detail=`${row.selected_host||"—"} → ${row.control_host||"—"} · ${row.reason||""}`}else if(row._kind==="config"){title="Config save";detail=`${row.success?"Success":"Failed"} · ${healthMs(row.elapsed_ms,2)}`;state=row.success?"on":"bad"}else{title=row.name||"Activity";detail=`${healthStatusLabel(row.phase||"")} · ${healthStatusLabel(row.outcome||"complete")} · ${Number(row.elapsed_seconds||0).toFixed(2)}s`;state=row.outcome==="error"?"bad":"on"}return `<div class="health-history-row"><span class="health-history-time">${healthClock(row.time)}</span><span class="badge ${state}">${healthEscape(healthStatusLabel(row._kind))}</span><b>${healthEscape(title)}</b><span>${healthEscape(detail)}</span></div>`});
  healthText("#healthUpdated","Auto-updated "+new Date((h.generated_at||Date.now()/1000)*1000).toLocaleTimeString()+" · every 2 s");
}

async function healthLoad(force=false){
  if(HEALTH.busy)return;if(!force&&(!document.querySelector("#tab-health.active")||document.hidden))return;
  HEALTH.busy=true;try{await healthReportBrowser();healthRender(await api("/api/health",{timeoutMs:5000}))}
  catch(e){healthText("#healthUpdated","Health unavailable: "+e.message)}finally{HEALTH.busy=false}
}
function healthStart(){healthLoad(true);if(!HEALTH.timer)HEALTH.timer=setInterval(healthLoad,2000)}
function healthStop(){if(HEALTH.timer){clearInterval(HEALTH.timer);HEALTH.timer=null}}
document.addEventListener("visibilitychange",()=>{if(document.querySelector("#tab-health.active")){if(document.hidden)healthStop();else healthStart()}});

/* ---------- discovery ---------- */
let DISCOVERY_SCAN_ACTIVE=false,DISCOVERY_DIALOG_OPEN=false;
const DISCOVERY_SELECTION={};
function closeDiscover(resumePolling=true){
  DISCOVERY_DIALOG_OPEN=false;const ov=$("#discOverlay");if(ov)ov.remove();
  if(resumePolling&&!DISCOVERY_SCAN_ACTIVE)setTimeout(()=>{loadInfo();if(DRIVE_STATUS_READY)refreshDrives()},0);
}
function openDiscover(){
  DISCOVERY_DIALOG_OPEN=true;let ov=$("#discOverlay");
  if(!ov){
    ov=document.createElement("div");ov.id="discOverlay";
    ov.style.cssText="position:fixed;inset:0;background:rgba(0,0,0,.72);display:flex;align-items:flex-start;justify-content:center;padding-top:9vh;z-index:50";
    ov.innerHTML=`<div class="panel" style="width:min(620px,92vw)">
      <h2>FIND ULTIMATE DEVICES</h2>
      <div id="discBody" class="hint">Includes previously verified addresses at the front of the same bounded
        concurrent <code>/v1/info</code> pass as the local /24, so stale history cannot delay the fresh scan.
        Ethernet and Wi-Fi interfaces are grouped into one physical device. Only interfaces verified during this scan are shown.</div>
      <div class="row" style="margin-top:10px">
        <button class="primary" id="discGo" onclick="runDiscover()">Scan network</button>
        <input id="discSubnet" placeholder="extra subnet e.g. 192.168.50." style="flex:1">
        <button onclick="closeDiscover()">Close</button>
      </div>
      <div class="row" style="margin-top:10px">
        <input id="discManual" placeholder="…or type an IP manually" style="flex:1"
          onkeydown="if(event.key==='Enter')connectTo($('#discManual').value)">
        <button onclick="connectTo($('#discManual').value)">Connect</button>
      </div>
      <div class="row" style="margin-top:12px;padding-top:10px;border-top:1px solid var(--line)">
        <button class="danger" id="discClear" onclick="clearDiscoveredDevices()">Clear discovered devices</button>
        <span class="hint">Recovery option: clears remembered hosts only, then performs a fresh scan.</span>
      </div></div>`;
    ov.addEventListener("click",e=>{if(e.target===ov)closeDiscover()});
    document.body.appendChild(ov);
  }
}
function selectDiscoveryAddress(group,ip,button){
  DISCOVERY_SELECTION[group]=ip;
  const root=button?.closest?.(".disc-device");
  if(root)root.querySelectorAll(".disc-choice").forEach(btn=>btn.classList.toggle("selected",btn.dataset.ip===ip));
}
function useSelectedDiscoveryAddress(group){
  const host=DISCOVERY_SELECTION[group]||"";if(host)connectTo(host);
}
function renderDiscoveryResults(r){
  const body=$("#discBody");
  if(!r.devices.length){
    body.innerHTML=`No Ultimate devices found on ${esc(r.subnets.join(", ")||"any local subnet")}.<br>
      Check the device is on, on the same network, and that <b>Web Remote Control</b> is enabled
      in its Network Settings — or enter its IP below.`;
    return;
  }
  body.innerHTML=r.devices.map((d,index)=>{
    const addresses=(d.addresses||[]),preferred=d.preferred_ip||(addresses[0]?.ip||"");
    const group="device-"+index;DISCOVERY_SELECTION[group]=preferred;
    const choices=addresses.map(a=>{
      const label=a.link_type==="ethernet"?"Ethernet":a.link_type==="wifi"?"Wi-Fi":"Unknown link";
      const recommended=a.ip===preferred&&a.link_type==="ethernet"?" · recommended":"";
      return `<button class="mini disc-choice${a.ip===preferred?" selected":""}" data-ip="${esc(a.ip)}" onclick="selectDiscoveryAddress('${group}','${jsq(a.ip)}',this)">${label} · ${esc(a.ip)}${recommended}</button>`;
    }).join("");
    return `<div class="disc-device">
      <div class="disc-device-main"><div><b>${esc(d.product)}</b>${d.firmware?" · fw "+esc(d.firmware):""}${d.hostname?" · "+esc(d.hostname):(d.unique_id?" · "+esc(d.unique_id):"")}
      <div class="disc-address-line">Select the address to use:</div><div class="disc-choice-row">${choices}</div></div>
      <button class="primary" onclick="useSelectedDiscoveryAddress('${group}')">Use selected address</button></div></div>`;
  }).join("");
}

async function runDiscover(){
  const body=$("#discBody"),btn=$("#discGo"),clear=$("#discClear");
  btn.disabled=true;if(clear)clear.disabled=true;DISCOVERY_SCAN_ACTIVE=true;
  body.innerHTML="Scanning remembered and local-/24 addresses together… <span class='cursor'></span>";
  try{
    const sub=$("#discSubnet").value.trim();
    const r=await api("/api/discover"+(sub?"?subnet="+encodeURIComponent(sub):""),{timeoutMs:30000});
    renderDiscoveryResults(r);
  }catch(e){body.innerHTML=`<span style="color:var(--err)">${esc(e.message)}</span>`}
  finally{
    DISCOVERY_SCAN_ACTIVE=false;btn.disabled=false;if(clear)clear.disabled=false;
    if(!DISCOVERY_DIALOG_OPEN)setTimeout(()=>{loadInfo();if(DRIVE_STATUS_READY)refreshDrives()},0);
  }
}
async function clearDiscoveredDevices(){
  const question="Clear all remembered Ultimate devices and addresses?\n\nThe current connection will be removed and a fresh network scan will begin. Other settings will not be changed.";
  if(!confirm(question))return;
  const body=$("#discBody"),btn=$("#discGo"),clear=$("#discClear");
  if(btn)btn.disabled=true;if(clear)clear.disabled=true;DISCOVERY_SCAN_ACTIVE=true;
  body.innerHTML="Clearing discovery history and scanning… <span class='cursor'></span>";
  try{
    const sub=$("#discSubnet").value.trim();
    const r=await api("/api/discover/clear"+(sub?"?subnet="+encodeURIComponent(sub):""),{method:"POST",timeoutMs:30000});
    closeLocalStreamsForLinkChange();
    clearStandaloneScreenNotice();
    LINK_STATUS={ip:"",link_type:"unknown",label:"Unknown",addresses:[],ethernet_ip:"",wifi_ip:"",control_ip:"",control_link_type:"unknown",rest_via_alternate:false,rest_route_label:"",streaming_available:true,rest_timeout:8};
    LAST_DEVICE_INFO=null;DRIVE_STATUS_READY=false;INPUT_STATUS={available:false,mode:"buffer",label:"Legacy KERNAL buffer",status:0};
    applyLinkStatus(LINK_STATUS);matrixClearLocalState();renderInputMode();
    $("#devinfo").innerHTML='<span style="color:var(--err)">No device configured — choose a verified result below</span>';
    toast("Discovery history cleared — scanning for Ultimate devices.","ok");
    renderDiscoveryResults(r);
  }catch(e){body.innerHTML=`<span style="color:var(--err)">${esc(e.message)}</span>`}
  finally{DISCOVERY_SCAN_ACTIVE=false;if(btn)btn.disabled=false;if(clear)clear.disabled=false;
    if(!DISCOVERY_DIALOG_OPEN)setTimeout(()=>{loadInfo();if(DRIVE_STATUS_READY)refreshDrives()},0)}
}
function scheduleInputProbe(delay=2500){
  clearTimeout(INPUT_PROBE_TIMER);INPUT_PROBE_TIMER=setTimeout(()=>{
    INPUT_PROBE_TIMER=null;
    if(uiInteractive()){scheduleInputProbe(1000);return}
    loadInputStatus(true);
  },delay);
}
async function connectTo(host){
  host=(host||"").trim();if(!host||uiInteractive())return;
  clearStandaloneScreenNotice();
  const previousDriveReady=DRIVE_STATUS_READY;DRIVE_STATUS_READY=false;
  const connectStarted=performance.now(),body=$("#discBody");
  if(body)body.innerHTML=`Connecting to <b>${esc(host)}</b>… <span class='cursor'></span>`;
  toast("Connecting to "+host+"…","ok");
  uiInteractiveStart();
  try{
    const r=await api("/api/connect",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({host}),timeoutMs:30000});
    if(r.connected){
      const elapsed=Math.round(Number(r.connect_timing?.total_ms)||performance.now()-connectStarted);
      toast("Connected to "+host+(r.rest_via_alternate?" · REST control via "+r.control_host:"")+` · ${elapsed} ms`,"ok");
      if(r.input){INPUT_STATUS=r.input;renderInputMode()}
      if(r.link)applyLinkStatus(r.link);
      if(r.info){LAST_DEVICE_INFO=r.info;INFO_FAILURES=0;DRIVE_STATUS_READY=true;renderDeviceInfo(r.info)}
      loadBootOptions();
      matrixClearLocalState();closeDiscover(false);
      if(inputStatusPending())scheduleInputProbe();
      if(DRIVE_STATUS_READY)setTimeout(refreshDrives,0);
    }else{
      DRIVE_STATUS_READY=previousDriveReady;
      if(body)body.innerHTML=`<span style="color:var(--err)">Could not connect to ${esc(host)}: ${esc(r.error||"unknown error")}</span>`;
      toast("Could not connect to "+host+": "+r.error,"err");
    }
  }catch(e){DRIVE_STATUS_READY=previousDriveReady;if(body)body.innerHTML=`<span style="color:var(--err)">${esc(e.message)}</span>`;toast(e.message,"err")}
  finally{uiInteractiveEnd()}
}


/* ---------- stream quality ---------- */
const QK="u64deck.quality";
function loadQuality(){
  try{const q=JSON.parse(localStorage.getItem(QK)||"{}");
    if(q.mode)$("#qMode").value=q.mode;
    if(q.render)$("#qRender").value=q.render;
    if(q.scale)$("#qScale").value=q.scale;
  }catch(e){}
  renderChanged(false);scaleChanged(false);
}
function saveQuality(){
  localStorage.setItem(QK,JSON.stringify({
    mode:$("#qMode").value,render:$("#qRender").value,scale:$("#qScale").value}));
}
function qualityChanged(){saveQuality();
  if(videoOn){ // reconnect WS with new buffer depth, stream itself keeps running
    if(wsV){wsV.onclose=null;wsV.close()}
    openVideoWS();
  }
}
function renderChanged(save=true){
  screenEl.style.imageRendering=$("#qRender").value;
  if(save)saveQuality();
}
function scaleChanged(save=true){
  const v=$("#qScale").value;
  if(v==="fit"){screenEl.style.width="100%";screenEl.style.height="auto"}
  else{screenEl.style.width=(384*+v)+"px";screenEl.style.height=(272*+v)+"px"}
  if(save)saveQuality();
}

async function transportChanged(){
  try{
    const r=await api("/api/stream/transport",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({transport:$("#qTransport").value})});
    toast(r.transport==="multicast"
      ?"Multicast: sharing "+r.multicast_video+" — VLC etc. can watch too"
      :"Direct stream to this PC","ok");
  }catch(e){toast(e.message,"err")}
}
async function loadTransport(){
  try{const r=await api("/api/stream/transport");$("#qTransport").value=r.transport}
  catch(e){}
}

async function loadIfaces(){
  try{
    const r=await api("/api/interfaces");
    const sel=$("#qIface");
    sel.innerHTML=`<option value="">AUTO${r.auto?" ("+esc(r.auto)+")":""}</option>`+
      r.interfaces.map(i=>`<option value="${esc(i.ip)}">${esc(i.name)} — ${esc(i.ip)}</option>`).join("");
    sel.value=r.selected||"";
  }catch(e){}
}
async function ifaceChanged(){
  try{
    const r=await api("/api/interfaces",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({ip:$("#qIface").value})});
    toast("Streams bound to "+(r.selected||("AUTO: "+r.auto)),"ok");
  }catch(e){toast(e.message,"err")}
}

/* ---------- device info / machine ---------- */
function linkName(type){return type==="ethernet"?"Ethernet":type==="wifi"?"Wi-Fi":"Unknown"}
function linkWarning(){return "Connected over Wi-Fi. Video and audio streaming are not supported over Wi-Fi, and the API is slower. Switch to the Ethernet address if available."}
function linkBadgeHtml(){
  if(LINK_STATUS.link_type==="ethernet"){
    const split=LINK_STATUS.rest_via_alternate&&LINK_STATUS.control_link_type==="wifi";
    const title=split?`Ethernet selected for command socket, FTP and streaming; REST control is routed through verified Wi-Fi ${LINK_STATUS.control_ip||""} to avoid the Ultimate dual-interface wired REST delay`:`Connected through the Ultimate wired interface`;
    return ` <span class="link-mode ethernet" title="${esc(title)}">· Ethernet${split?" · REST via Wi-Fi":""}</span>`;
  }
  if(LINK_STATUS.link_type==="wifi"){const clickable=!!LINK_STATUS.ethernet_ip;return ` <button class="link-mode wifi${clickable?" switchable":""}" data-tip="${esc(linkWarning())}" ${clickable?`onclick="switchToEthernet()"`:"disabled"}>· Wi-Fi${clickable?" · Switch to Ethernet":""}</button>`}
  return "";
}
function closeLocalStreamsForLinkChange(){
  clearTimeout(VIDEO_NO_FRAME_TIMER);VIDEO_NO_FRAME_TIMER=null;
  videoWanted=false;videoOn=false;if(wsV){const old=wsV;wsV=null;old.onclose=null;old.close()}
  audioWanted=false;audioOn=false;clearTimeout(audioReconnectTimer);audioReconnectTimer=null;flushBrowserAudio();if(wsA){const old=wsA;wsA=null;old.onclose=null;old.close()}
  $("#btnVideo").textContent="Start video";$("#btnVideo").classList.add("primary");setAudioState("off",0);
}
function renderLinkState(){
  const wifi=LINK_STATUS.link_type==="wifi",known=LINK_STATUS.link_type!=="unknown";
  const reason="Streaming is not available over Wi-Fi. Connect using the Ultimate's Ethernet address.";
  for(const id of ["btnVideo","btnAudio","btnRecord","btnFullscreen"]){const el=$("#"+id);if(!el)continue;el.disabled=wifi;if(wifi)el.dataset.tip=reason;else if(el.dataset.wifiTip){el.dataset.tip=el.dataset.wifiTip}}
  const badge=$("#streamLinkStatus");if(badge){const split=!wifi&&LINK_STATUS.rest_via_alternate;badge.textContent=wifi?"Wi-Fi · streaming unavailable":known?(split?"Ethernet · REST via Wi-Fi":"Ethernet"):"Link unknown";badge.className="badge "+(wifi?"bad":known?"on":"")}
  const switcher=$("#wifiSwitchInline");if(switcher){switcher.style.display=wifi&&LINK_STATUS.ethernet_ip?"inline-flex":"none"}
  const hint=$("#streamHint");if(hint&&wifi){hint.style.display="none"}
  if(wifi){closeLocalStreamsForLinkChange();bezelReset();drawVideoPlaceholder("STREAMING NOT AVAILABLE OVER WI-FI\nCONNECT VIA ETHERNET TO STREAM")}
  else if(!videoHasFrame&&videoPlaceholderMessage.includes("WI-FI"))drawVideoPlaceholder("VIDEO NOT CONNECTED");
}
function applyLinkStatus(status){
  LINK_STATUS={...LINK_STATUS,...(status||{})};
  DEVICE_REQUEST_TIMEOUT_MS=LINK_STATUS.link_type==="wifi"?45000:15000;
  renderLinkState();if(LAST_DEVICE_INFO)renderDeviceInfo(LAST_DEVICE_INFO);
}
async function loadLinkStatus(refresh=false){try{const r=await api("/api/link/status"+(refresh?"?refresh=true":""),{timeoutMs:refresh?50000:15000});applyLinkStatus(r);return r}catch(e){return LINK_STATUS}}
async function switchToEthernet(){if(!LINK_STATUS.ethernet_ip)return;await connectTo(LINK_STATUS.ethernet_ip)}
let VER_SHOWN=false,LAST_DEVICE_INFO=null,INFO_FAILURES=0,INFO_RETRY_TIMER=null;
let MOUNT_RUN_STATUS_WATCH=null,MOUNT_RUN_BUSY=false;
let INPUT_STATUS={available:null,pending:true,mode:"unknown",label:"Input capability pending",status:0};
function inputModeKind(status=INPUT_STATUS){
  const mode=String(status?.mode||"").trim().toLowerCase();
  if(mode==="matrix"||mode==="cia1"||mode==="cia1_matrix")return "matrix";
  if(mode==="buffer"||mode==="legacy"||mode==="legacy_buffer"||mode==="kernal_buffer"||mode==="legacy kernal buffer")return "legacy";
  if(status?.pending===true)return "pending";
  if(status?.available===null||status?.available===undefined)return "pending";
  if(typeof status.available==="string"){
    const value=status.available.trim().toLowerCase();
    if(value==="true"||value==="1"||value==="yes")return "matrix";
    if(value==="false"||value==="0"||value==="no")return "legacy";
  }
  return status.available?"matrix":"legacy";
}
function inputStatusPending(status=INPUT_STATUS){return inputModeKind(status)==="pending"}
function isMatrixInput(status=INPUT_STATUS){return inputModeKind(status)==="matrix"}
function isLegacyInput(status=INPUT_STATUS){return inputModeKind(status)==="legacy"}
let MOUNT_RUN_PROMPT_KEY="",MOUNT_RUN_PROMPT_STATE=null,STANDALONE_SCREEN_NOTICE=null;
let STANDALONE_READY_TIMER=null,STANDALONE_READY_CONSECUTIVE=0,STANDALONE_NOTICE_GENERATION=0;
function inputModeSuffix(){
  if(inputStatusPending())return ` <span class="input-mode buffer" title="${esc(INPUT_STATUS.label||"")}">· Input: Checking</span>`;
  const matrix=isMatrixInput();
  return ` <span class="input-mode ${matrix?"matrix":"buffer"}" title="${esc(INPUT_STATUS.label||"")}">· Input: ${matrix?"CIA1":"Legacy KERNAL buffer"}</span>`;
}
function renderInputMode(){
  const badge=$("#inputModeBadge"),help=$("#kbhelp");if(!badge||!help)return;
  if(inputStatusPending()){
    badge.textContent="Checking input…";badge.className="badge";
    help.innerHTML="Keyboard capability is being checked in the background. Other device controls remain available.";
  }else if(isMatrixInput()){
    clearStandaloneScreenNotice();
    badge.textContent="CIA1 matrix";badge.className="badge matrix";
    help.innerHTML="Esc = RUN/STOP · F1–F8 · cursor keys · Backspace = DEL · Home / Shift-Home = CLR. <b>CIA1 matrix input is active</b>: held keys, chords, cracktros, games and the Ultimate menu are supported.";
  }else{
    badge.textContent="Legacy buffer";badge.className="badge buffer";
    help.innerHTML="Esc = RUN/STOP · F1–F6/F8 · cursor keys · Backspace = DEL · Home / Shift-Home = CLR. This Ultimate is using the <b>legacy KERNAL keyboard buffer</b>. Retro Replay F7 requires the physical C64 keyboard; matrix-scanning software and the Ultimate menu may not see remote keys.";
  }
  renderAutoFastloadState();
}
function renderDeviceInfo(i,suffix=""){
  $("#devinfo").innerHTML=`<b>${esc(i.product||"?")}</b> · FW ${esc(i.firmware_version||"?")}`+
    (i.core_version?` · Core ${esc(i.core_version)}`:"")+(i.hostname?` · ${esc(i.hostname)}`:"")+linkBadgeHtml()+inputModeSuffix()+suffix;
}
function stopMountRunStatusWatch(){
  if(MOUNT_RUN_STATUS_WATCH){clearInterval(MOUNT_RUN_STATUS_WATCH);MOUNT_RUN_STATUS_WATCH=null}
}
function renderScreenOperationOverlay(){
  const overlay=$("#legacyF7Overlay");if(!overlay)return;
  const prompt=MOUNT_RUN_PROMPT_STATE?.active?MOUNT_RUN_PROMPT_STATE:STANDALONE_SCREEN_NOTICE;
  if(!prompt?.active){overlay.style.display="none";overlay.className="screen-operation-overlay";MOUNT_RUN_PROMPT_KEY="";return}
  const informational=prompt.informational===true;
  const key=(informational?"info:":String(prompt.generation||"")+":")+String(prompt.phase||prompt.kind||"");
  $("#legacyF7Title").textContent=prompt.title||"Physical F7 required";
  $("#legacyF7Message").textContent=prompt.message||"Press F7 on the C64 keyboard to continue.";
  const cancel=$("#legacyF7Cancel"),cont=$("#legacyF7Continue"),dismiss=$("#legacyF7Dismiss");
  cancel.style.display=!informational&&prompt.cancel_available!==false?"inline-flex":"none";
  cancel.disabled=prompt.phase==="cancelling";
  cont.style.display=!informational&&prompt.phase==="manual_continue"?"inline-flex":"none";
  cont.disabled=false;
  dismiss.style.display=informational?"inline-flex":"none";
  dismiss.textContent=prompt.dismiss_label||"Dismiss";
  overlay.className="screen-operation-overlay"+(prompt.phase==="detected"?" detected":"")+(informational?" informational":"");
  overlay.style.display="flex";
  if(key!==MOUNT_RUN_PROMPT_KEY){
    MOUNT_RUN_PROMPT_KEY=key;
    if(!document.querySelector("#tab-screen.active")){
      if(informational&&prompt.phase!=="detected")toast("Physical F7 required — open Screen or press F7 on the C64","err");
      else if(!informational&&prompt.phase==="physical_f7")toast("Physical F7 required — open Screen or press F7 on the C64","err");
      else if(prompt.phase==="cartridge_attention")toast("Cartridge startup requires attention — open Screen","err");
    }
  }
}
function renderMountRunPrompt(prompt){
  MOUNT_RUN_PROMPT_STATE=prompt?.active?prompt:null;
  renderScreenOperationOverlay();
}
function stopStandaloneReadyWatch(){
  if(STANDALONE_READY_TIMER){clearTimeout(STANDALONE_READY_TIMER);STANDALONE_READY_TIMER=null}
  STANDALONE_READY_CONSECUTIVE=0;
}
function scheduleStandaloneReadyWatch(generation,delay=700){
  stopStandaloneReadyWatch();
  STANDALONE_READY_TIMER=setTimeout(()=>pollStandaloneBasicReady(generation),delay);
}
async function pollStandaloneBasicReady(generation){
  STANDALONE_READY_TIMER=null;
  if(generation!==STANDALONE_NOTICE_GENERATION||!STANDALONE_SCREEN_NOTICE?.active)return;
  try{
    const r=await api("/api/machine/basic-ready",{timeoutMs:2500});
    if(generation!==STANDALONE_NOTICE_GENERATION||!STANDALONE_SCREEN_NOTICE?.active)return;
    if(r?.supported===false){stopStandaloneReadyWatch();return}
    if(!r?.busy&&r?.ready===true)STANDALONE_READY_CONSECUTIVE++;
    else if(!r?.busy)STANDALONE_READY_CONSECUTIVE=0;
    if(STANDALONE_READY_CONSECUTIVE>=2){
      stopStandaloneReadyWatch();
      STANDALONE_SCREEN_NOTICE={
        ...STANDALONE_SCREEN_NOTICE,
        kind:"standalone_ready",phase:"detected",
        title:"Fastload detected — ready",
        message:"BASIC is ready.",monitor_basic_ready:false,
      };
      renderScreenOperationOverlay();
      setTimeout(()=>{
        if(generation===STANDALONE_NOTICE_GENERATION&&STANDALONE_SCREEN_NOTICE?.kind==="standalone_ready")clearStandaloneScreenNotice();
      },900);
      return;
    }
  }catch(e){
    // A short status probe may lose a race with another device operation.
    // Keep the guidance visible and try again without surfacing a toast.
  }
  if(generation===STANDALONE_NOTICE_GENERATION&&STANDALONE_SCREEN_NOTICE?.active){
    STANDALONE_READY_TIMER=setTimeout(()=>pollStandaloneBasicReady(generation),700);
  }
}
function showStandaloneScreenNotice(notice){
  if(!notice)return;
  stopStandaloneReadyWatch();
  const generation=++STANDALONE_NOTICE_GENERATION;
  STANDALONE_SCREEN_NOTICE={...notice,active:true,informational:true,phase:notice.kind||"standalone_f7"};
  renderScreenOperationOverlay();
  if(notice.monitor_basic_ready===true)scheduleStandaloneReadyWatch(generation);
}
function clearStandaloneScreenNotice(){
  ++STANDALONE_NOTICE_GENERATION;
  stopStandaloneReadyWatch();
  STANDALONE_SCREEN_NOTICE=null;
  renderScreenOperationOverlay();
}
function dismissStandaloneScreenNotice(){clearStandaloneScreenNotice()}

async function cancelLegacyMountRun(){
  const btn=$("#legacyF7Cancel");if(btn)btn.disabled=true;
  try{
    await api("/api/mount/run/cancel",{method:"POST"});
    renderMountRunPrompt({active:true,phase:"cancelling",title:"Cancelling Mount & Run…",message:"The current wait is being released.",cancel_available:false});
  }catch(e){toast(e.message,"err");if(btn)btn.disabled=false}
}
async function continueLegacyMountRun(){
  const btn=$("#legacyF7Continue");if(btn)btn.disabled=true;
  try{await api("/api/mount/run/continue",{method:"POST"})}
  catch(e){toast(e.message,"err");if(btn)btn.disabled=false}
}
function beginMountRunStatusWatch(){
  clearStandaloneScreenNotice();
  MOUNT_RUN_BUSY=true;stopMountRunStatusWatch();
  setTimeout(loadInfo,150);
  // The mount request is synchronous at the API level, so poll the local busy
  // snapshot until the server confirms the mount and begins the load phase.
  MOUNT_RUN_STATUS_WATCH=setInterval(loadInfo,750);
}
function finishMountRunStatusWatch(){
  stopMountRunStatusWatch();MOUNT_RUN_BUSY=false;renderMountRunPrompt(null);loadInfo();
}
let INPUT_STATUS_IN_FLIGHT=false;
async function loadInputStatus(refresh=false){
  if(INPUT_STATUS_IN_FLIGHT||uiInteractive())return INPUT_STATUS;
  INPUT_STATUS_IN_FLIGHT=true;
  try{
    const r=await api("/api/input/status"+(refresh?"?refresh=true":""),{timeoutMs:15000});
    const wasMatrix=isMatrixInput();INPUT_STATUS=r||INPUT_STATUS;
    if(wasMatrix&&!isMatrixInput())matrixClearLocalState();
    renderInputMode();if(LAST_DEVICE_INFO)renderDeviceInfo(LAST_DEVICE_INFO);
    return INPUT_STATUS;
  }catch(e){
    INPUT_STATUS={available:false,pending:false,mode:"buffer",label:"Legacy KERNAL buffer",status:0,detail:e.message};
    matrixClearLocalState();renderInputMode();if(LAST_DEVICE_INFO)renderDeviceInfo(LAST_DEVICE_INFO);
    return INPUT_STATUS;
  }finally{INPUT_STATUS_IN_FLIGHT=false}
}
async function loadInfo(){
  if(DISCOVERY_SCAN_ACTIVE||DISCOVERY_DIALOG_OPEN||uiInteractive()||INFO_IN_FLIGHT)return;
  INFO_IN_FLIGHT=true;
  try{
    if(!VER_SHOWN){try{const c=await api("/api/app_config");
      if(c.version){$("#ver").textContent="v"+c.version+(c.release_label?" · "+c.release_label:"")+(c.build?" · "+c.build:"");
        $("#ver").title="Version "+c.version+(c.release_label?" ("+c.release_label+")":"")+", build "+(c.build||"?")+" — quote this in bug reports";
        const exitBtn=$("#btnAppExit");if(exitBtn)exitBtn.style.display=c.local_exit_available!==false?"":"none";
        VER_SHOWN=true}}catch(e){}}
    const previousFailures=INFO_FAILURES,i=await api("/api/info");
    if(i?.u64deck_discovery_busy||i?.u64deck_operation_busy){
      if(INFO_RETRY_TIMER)clearTimeout(INFO_RETRY_TIMER);
      const retry=Math.max(500,Number(i.u64deck_retry_ms)||1000);
      INFO_RETRY_TIMER=setTimeout(()=>{INFO_RETRY_TIMER=null;loadInfo()},retry);
      return;
    }
    if(i?.u64deck_busy){
      INFO_FAILURES=0;MOUNT_RUN_BUSY=true;stopMountRunStatusWatch();
      applyBusyMountSnapshot(i.u64deck_mounts);
      renderMountRunPrompt(i.u64deck_mount_run_prompt);
      const label=esc(i.u64deck_busy_label||"BUSY — loading program…");
      if(LAST_DEVICE_INFO)renderDeviceInfo(LAST_DEVICE_INFO,` <span class="device-busy">· ${label}</span>`);
      else $("#devinfo").innerHTML=`<span class="device-busy">${label}</span>`;
      if(INFO_RETRY_TIMER)clearTimeout(INFO_RETRY_TIMER);
      const retry=Math.max(500,Number(i.u64deck_retry_ms)||2000);
      INFO_RETRY_TIMER=setTimeout(()=>{INFO_RETRY_TIMER=null;loadInfo()},retry);
      return;
    }
    LAST_DEVICE_INFO=i;INFO_FAILURES=0;MOUNT_RUN_BUSY=false;renderMountRunPrompt(null);
    if(INFO_RETRY_TIMER){clearTimeout(INFO_RETRY_TIMER);INFO_RETRY_TIMER=null}
    if(i.u64deck_link)applyLinkStatus(i.u64deck_link);else await loadLinkStatus(previousFailures>0);
    if(i.u64deck_input){INPUT_STATUS=i.u64deck_input;renderInputMode();
      if(inputStatusPending())scheduleInputProbe()}
    renderDeviceInfo(i);
    const firstDriveReady=!DRIVE_STATUS_READY;DRIVE_STATUS_READY=true;
    if(firstDriveReady)setTimeout(refreshDrives,0);
    if(document.querySelector("#tab-settings.active")&&!SET.loaded)settingsRetry(0);
  }catch(e){
    const unconfig=/No device configured/.test(e.message);
    if(!unconfig&&LAST_DEVICE_INFO&&INFO_FAILURES===0){
      INFO_FAILURES=1;
      renderDeviceInfo(LAST_DEVICE_INFO,` <span class="reconnecting">· Reconnecting…</span>`);
      if(INFO_RETRY_TIMER)clearTimeout(INFO_RETRY_TIMER);
      INFO_RETRY_TIMER=setTimeout(()=>{INFO_RETRY_TIMER=null;loadInfo()},2000);
      return;
    }
    INFO_FAILURES++;
    $("#devinfo").innerHTML=`<span style="color:var(--err)">${unconfig
      ?"No device configured — click Select Ultimate… →":esc("Offline: "+e.message)}</span>`;
  }finally{INFO_IN_FLIGHT=false}
}

let AUTO_FASTLOAD=false,EFFECTIVE_AUTO_FASTLOAD=false,BOOT_OPTIONS_SAVING=false;
let BOOT_CARTRIDGE={known:false,classification:"unknown",label:"Unknown"};
function renderAutoFastloadState(){
  const box=$("#autoFastload"),hint=$("#autoFastloadHint"),label=$("#autoFastloadLabel");
  if(!box)return;
  const legacy=isLegacyInput();
  const retro=BOOT_CARTRIDGE?.classification==="retro_replay";
  EFFECTIVE_AUTO_FASTLOAD=AUTO_FASTLOAD&&!legacy&&retro;
  box.checked=AUTO_FASTLOAD;
  box.disabled=BOOT_OPTIONS_SAVING||legacy;
  if(hint){
    hint.style.display=legacy?"inline-flex":"none";
    hint.textContent=retro?"Legacy: physical F7 for Retro Replay"
      :BOOT_CARTRIDGE?.classification==="other"?"Legacy: manual cartridge startup"
      :BOOT_CARTRIDGE?.classification==="none"?"Legacy: Auto F7 unavailable"
      :"Legacy: cartridge checked at launch";
  }
  if(label)label.title=legacy
    ?"Retro Replay is configured, but automatic F7 is disabled with Legacy KERNAL-buffer input because an emulated F7 can open the Freeze Menu. Press physical F7 on the C64 instead. Your saved Auto F7 preference is retained for CIA1-capable devices."
    :"Automatically presses F7 after Reset, Reboot and Mount & Run when Retro Replay is configured. Disable this option to handle the Retro Replay startup menu manually.";
}
async function loadBootOptions(){
  try{
    const r=await api("/api/boot_options");
    AUTO_FASTLOAD=!!r.auto_fastload;
    BOOT_CARTRIDGE=r.cartridge||BOOT_CARTRIDGE;
    EFFECTIVE_AUTO_FASTLOAD=!!r.effective_auto_fastload;
    renderAutoFastloadState();
  }catch(e){}
}
async function autoFastloadChanged(){
  const box=$("#autoFastload"),wanted=box.checked;
  if(isLegacyInput()){
    box.checked=AUTO_FASTLOAD;renderAutoFastloadState();
    toast("Legacy input detected — physical F7 is required on the C64","err");return
  }
  BOOT_OPTIONS_SAVING=true;renderAutoFastloadState();
  try{
    const r=await api("/api/boot_options",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({auto_fastload:wanted})});
    AUTO_FASTLOAD=!!r.auto_fastload;
    BOOT_CARTRIDGE=r.cartridge||BOOT_CARTRIDGE;
    EFFECTIVE_AUTO_FASTLOAD=!!r.effective_auto_fastload;
    toast(AUTO_FASTLOAD
      ?"Retro Replay Fastload: F7 will be pressed after u64deck resets"
      :"Automatic Retro Replay F7 disabled","ok");
  }catch(e){toast(e.message,"err")}
  finally{BOOT_OPTIONS_SAVING=false;renderAutoFastloadState()}
}
async function machine(a){
  if(MOUNT_RUN_BUSY&&(a==="reset"||a==="reboot")){
    toast("Mount & Run is in progress — "+a+" was not queued","err");return
  }
  const resetLike=a==="reset"||a==="reboot";
  if(resetLike)clearStandaloneScreenNotice();
  try{
    const r=await put("/api/machine/"+a);
    if(r?.u64deck_cartridge){BOOT_CARTRIDGE=r.u64deck_cartridge;renderAutoFastloadState()}
    if(r?.u64deck_screen_notice)showStandaloneScreenNotice(r.u64deck_screen_notice);
    const retro=BOOT_CARTRIDGE?.classification==="retro_replay";
    const legacy=isLegacyInput();
    if(resetLike&&AUTO_FASTLOAD&&retro&&legacy){
      toast(healthStatusLabel(a)+" ✓ — physical F7 required for Retro Replay Fastload","ok");
    }else if(resetLike&&AUTO_FASTLOAD&&!retro){
      toast(healthStatusLabel(a)+" ✓ — Auto F7 skipped (Retro Replay not configured)","ok");
    }else{
      const fast=(EFFECTIVE_AUTO_FASTLOAD&&resetLike)?" + F7 Fastload":"";
      toast(healthStatusLabel(a)+fast+" ✓","ok");
    }
  }catch(e){toast(e.message,"err")}}


/* ---------- keyboard ---------- */
const P={ // KeyboardEvent.key -> PETSCII for the legacy KERNAL buffer
  Enter:13,Backspace:20,Delete:20,Escape:3,
  ArrowDown:17,ArrowUp:145,ArrowRight:29,ArrowLeft:157,
  Home:19,Insert:148,F1:133,F2:137,F3:134,F4:138,F5:135,F6:139,F7:136,F8:140};
function chToPet(ch){const o=ch.charCodeAt(0);
  if(ch>="a"&&ch<="z")return o-32;
  if(ch>="A"&&ch<="Z")return o+128;
  if(o>=0x20&&o<=0x5D)return o;
  if(ch==="£")return 0x5C;
  return null}
function legacyCodeForEvent(ev){
  if(ev.key==="Home"&&ev.shiftKey)return 147;               // CLR
  if(P[ev.key]!==undefined)return P[ev.key];
  if(ev.key.length===1&&!ev.ctrlKey&&!ev.metaKey)return chToPet(ev.key);
  return null;
}
let keyq=[],keyTimer=null,keySending=false,sentKeys=0,legacyBusyToastAt=0;
function queueKeys(codes){
  if(MOUNT_RUN_BUSY){
    const now=Date.now();
    if(now-legacyBusyToastAt>1500){
      legacyBusyToastAt=now;
      toast("Mount & Run is in progress — Legacy keys were not queued","err");
    }
    return;
  }
  keyq.push(...codes);
  // A tiny debounce combines normal typing into one ordered command without
  // adding the noticeable 25 ms delay used by older builds.
  if(!keyTimer&&!keySending)keyTimer=setTimeout(flushKeys,6);
}
async function flushKeys(){
  keyTimer=null;
  if(keySending||!keyq.length)return;
  keySending=true;
  try{
    // Keep batches below the C64 KERNAL keyboard buffer size. More queued
    // characters are sent only after this request has completed, preventing
    // concurrent HTTP requests from turning RUN into RNU.
    while(keyq.length){
      const batch=keyq.splice(0,8);
      await api("/api/keys",{method:"POST",headers:{"Content-Type":"application/json",
        "X-U64deck-Key-Origin":"screen-mirror"},body:JSON.stringify({petscii:batch})});
      sentKeys+=batch.length;updateKeyboardStatus();
    }
  }catch(e){toast("Keys: "+e.message,"err")}
  finally{
    keySending=false;
    if(keyq.length&&!keyTimer)keyTimer=setTimeout(flushKeys,6);
  }
}

// CIA1 matrix input -----------------------------------------------------
// Browser keys are translated to firmware input names. A held-set suppresses
// browser key-repeat and preserves press/release ordering for games and menus.
const MATRIX_HELD=new Map();
let MATRIX_QUEUE=[],MATRIX_SENDING=false;
const MATRIX_DIRECT_CODE={
  ShiftLeft:["left_shift"],ShiftRight:["right_shift"],
  ControlLeft:["ctrl"],ControlRight:["ctrl"],
  AltLeft:["commodore"],AltRight:["commodore"],
};
const MATRIX_CHAR={
  " ":["space"],"+":["plus"],"-":["minus"],".":["period"],
  ":":["colon"],"@":["at"],",":["comma"],"£":["pound"],
  "*":["star"],";":["semicolon"],"=":["equals"],"/":["slash"],
  "↑":["arrow_up"],"←":["arrow_left"],
  "!":["left_shift","1"],'"':["left_shift","2"],
  "#":["left_shift","3"],"$":["left_shift","4"],
  "%":["left_shift","5"],"&":["left_shift","6"],
  "'":["left_shift","7"],"(":["left_shift","8"],
  ")":["left_shift","9"],"<":["left_shift","comma"],
  ">":["left_shift","period"],"?":["left_shift","slash"],
};
function physicalModifierRecord(name,activeOnly=false){
  for(const record of MATRIX_HELD.values()){
    if(record.directModifier===name&&(!activeOnly||record.active))return record;
  }
  return null;
}
function matrixChord(inputs){
  let chord=[...new Set(inputs)];
  // Browser F2/uppercase mappings use left_shift as the canonical synthetic
  // shift. Honour a physically-held right Shift instead so the same event is
  // still an atomic C64 chord without inventing a second held modifier.
  if(chord.includes("left_shift")&&!physicalModifierRecord("left_shift",true)
      &&physicalModifierRecord("right_shift",true)){
    chord=chord.map(x=>x==="left_shift"?"right_shift":x);
  }
  return chord;
}
function matrixMapping(ev){
  if(MATRIX_DIRECT_CODE[ev.code])return MATRIX_DIRECT_CODE[ev.code];
  const key=ev.key;
  const special={
    Enter:["return"],Backspace:["inst_del"],Delete:["inst_del"],
    Escape:["run_stop"],ArrowRight:["cursor_left_right"],
    ArrowLeft:["left_shift","cursor_left_right"],
    ArrowDown:["cursor_up_down"],ArrowUp:["left_shift","cursor_up_down"],
    Home:ev.shiftKey?["left_shift","clr_home"]:["clr_home"],
    Insert:["left_shift","inst_del"],
    F1:["f1"],F2:["left_shift","f1"],F3:["f3"],F4:["left_shift","f3"],
    F5:["f5"],F6:["left_shift","f5"],F7:["f7"],F8:["left_shift","f7"],
    Pause:["restore"],
  };
  if(special[key])return special[key];
  if(key.length===1){
    if(key>="a"&&key<="z")return [key];
    if(key>="A"&&key<="Z")return ["left_shift",key.toLowerCase()];
    if(key>="0"&&key<="9")return [key];
    if(MATRIX_CHAR[key])return MATRIX_CHAR[key];
  }
  return null;
}
function matrixClearLocalState(){MATRIX_HELD.clear();MATRIX_QUEUE=[]}
function queueMatrixEvent(event){
  MATRIX_QUEUE.push(event);
  if(!MATRIX_SENDING)flushMatrixEvents();
}
async function flushMatrixEvents(){
  if(MATRIX_SENDING||!MATRIX_QUEUE.length||!isMatrixInput())return;
  MATRIX_SENDING=true;
  try{
    while(MATRIX_QUEUE.length&&isMatrixInput()){
      const batch=MATRIX_QUEUE.splice(0,64);
      await api("/api/input/events",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({events:batch})});
      sentKeys+=batch.filter(e=>e.kind==="keyboard"&&e.transition!=="release").length;
      updateKeyboardStatus();
    }
  }catch(e){
    matrixClearLocalState();toast("Matrix input: "+e.message,"err");await loadInputStatus(true);
  }finally{MATRIX_SENDING=false;if(MATRIX_QUEUE.length&&isMatrixInput())flushMatrixEvents()}
}
function matrixReleaseAll(reason="",keepalive=false){
  const hadState=MATRIX_HELD.size>0||MATRIX_QUEUE.length>0||MATRIX_SENDING;
  MATRIX_HELD.clear();MATRIX_QUEUE=[];
  if(!hadState||!isMatrixInput())return;
  if(keepalive){
    fetch("/api/input/release_all",{method:"POST",keepalive:true}).catch(()=>{});return;
  }
  // If a press request is already in flight, queue release_all behind it so a
  // late press cannot overtake cleanup and leave a real key stuck.
  queueMatrixEvent({kind:"release_all"});
}
function updateKeyboardStatus(){
  const stat=$("#kbstat");if(!stat)return;
  const mode=isMatrixInput()?"CIA1 matrix":"legacy buffer";
  stat.innerHTML=`Keyboard captured — <b>${sentKeys}</b> key events sent via ${mode}. Esc = RUN/STOP.`;
}
async function quickC64Key(name){
  if(isMatrixInput()){
    queueMatrixEvent({kind:"keyboard",inputs:[name],transition:"tap"});
    screenEl.focus({preventScroll:true});return;
  }
  const legacy={space:32,run_stop:3}[name];
  if(legacy!==undefined){queueKeys([legacy]);screenEl.focus({preventScroll:true});return}
  toast("RESTORE requires CIA1 matrix input on supported Ultimate 64 firmware","err");
}

const screenEl=$("#screen");
function screenshot(){
  const ts=new Date().toISOString().replace(/[:.]/g,"-").slice(0,19);
  screenEl.toBlob(b=>{
    const a=document.createElement("a");
    a.href=URL.createObjectURL(b);a.download="u64-"+ts+".png";a.click();
    setTimeout(()=>URL.revokeObjectURL(a.href),1000);
    toast("Screenshot saved ✓","ok")},"image/png")}
function toggleFullscreen(){
  if(LINK_STATUS.link_type==="wifi"){toast("Streaming is not available over Wi-Fi; switch to Ethernet for Full Screen","err");return}
  const crt=screenEl.closest(".crt");
  if(document.fullscreenElement)document.exitFullscreen();
  else crt.requestFullscreen().then(()=>screenEl.focus()).catch(e=>toast(e.message,"err"));
}
screenEl.addEventListener("dblclick",toggleFullscreen);
// Explicitly reclaim focus on pointer input. This keeps Space/Enter aimed at
// the C64 canvas rather than re-activating whichever UI button was used last.
screenEl.addEventListener("pointerdown",()=>screenEl.focus({preventScroll:true}));
screenEl.addEventListener("keydown",ev=>{
  // In fullscreen the browser reserves Esc for exiting — don't also send RUN/STOP.
  if(ev.key==="Escape"&&document.fullscreenElement)return;
  if(isMatrixInput()){
    const mapped=matrixMapping(ev);if(!mapped)return;
    ev.preventDefault();
    const id=ev.code||ev.key;
    if(ev.repeat||MATRIX_HELD.has(id))return;
    const pressInputs=matrixChord(mapped);if(!pressInputs.length)return;
    const directModifier=(MATRIX_DIRECT_CODE[ev.code]||[])[0]||"";
    const suspended=[];
    // A PC layout may require Shift to type a C64 key that is unshifted on the
    // C64 (for example @ on a UK keyboard). Temporarily release only that
    // physical Shift, then restore it when this key is released.
    for(const shift of ["left_shift","right_shift"]){
      const rec=physicalModifierRecord(shift,true);
      if(rec&&!pressInputs.includes(shift)){
        queueMatrixEvent({kind:"keyboard",inputs:[shift],transition:"release"});
        rec.active=false;suspended.push(shift);
      }
    }
    const releaseInputs=pressInputs.filter(input=>{
      if(input!=="left_shift"&&input!=="right_shift")return true;
      return !physicalModifierRecord(input,true);
    });
    const record={pressInputs,releaseInputs,directModifier,
      active:!!directModifier,suspended};
    MATRIX_HELD.set(id,record);
    queueMatrixEvent({kind:"keyboard",inputs:pressInputs,transition:"press"});
    return;
  }
  const code=legacyCodeForEvent(ev);
  if(code===136){
    ev.preventDefault();
    toast("Physical F7 is required on Legacy KERNAL-buffer input","err");
    return;
  }
  if(code!==null){ev.preventDefault();queueKeys([code])}
});
screenEl.addEventListener("keyup",ev=>{
  if(!isMatrixInput())return;
  const id=ev.code||ev.key,record=MATRIX_HELD.get(id);if(!record)return;
  ev.preventDefault();MATRIX_HELD.delete(id);
  if(record.directModifier){
    if(record.active)queueMatrixEvent({kind:"keyboard",inputs:record.releaseInputs,transition:"release"});
  }else if(record.releaseInputs.length){
    queueMatrixEvent({kind:"keyboard",inputs:record.releaseInputs,transition:"release"});
  }
  for(const modifier of record.suspended||[]){
    const physical=physicalModifierRecord(modifier,false);
    if(physical&&!physical.active){
      queueMatrixEvent({kind:"keyboard",inputs:[modifier],transition:"press"});
      physical.active=true;
    }
  }
});
screenEl.addEventListener("focus",()=>{
  $("#kbstat").innerHTML=isMatrixInput()
    ?"Keyboard captured — CIA1 matrix input active. Held keys and chords go directly to the C64."
    :"Keyboard captured — legacy KERNAL buffer active. Esc = RUN/STOP; use physical C64 F7 for cartridge Fastload.";
});
screenEl.addEventListener("blur",()=>{
  matrixReleaseAll("screen blur");
  $("#kbstat").textContent="Click the screen to capture the keyboard.";
});
document.addEventListener("visibilitychange",()=>{if(document.hidden)matrixReleaseAll("page hidden")});
window.addEventListener("pagehide",()=>matrixReleaseAll("page hide",true));
window.addEventListener("beforeunload",()=>matrixReleaseAll("page unload",true));
async function sendLine(){const t=$("#typeline").value;if(!t)return;
  if(MOUNT_RUN_BUSY){toast("Mount & Run is in progress — text was not queued","err");return}
  try{await api("/api/keys",{method:"POST",headers:{"Content-Type":"application/json",
      "X-U64deck-Key-Origin":"type-line"},body:JSON.stringify({text:t+"\n"})});
    $("#typeline").value="";toast("Typed to C64 ✓","ok")}
  catch(e){toast(e.message,"err")}}

/* ---------- video ---------- */
const PALETTE=[0x000000,0xFFFFFF,0x68372B,0x70A4B2,0x6F3D86,0x588D43,0x352879,0xB8C76F,
               0x6F4F25,0x433900,0x9A6759,0x444444,0x6C6C6C,0x9AD284,0x6C5EB5,0x959595];
const PAL32=new Uint32Array(16);
for(let i=0;i<16;i++){const c=PALETTE[i];
  PAL32[i]=0xFF000000|((c&0xFF)<<16)|(c&0xFF00)|((c>>16)&0xFF)}   // ABGR
const ctx2d=screenEl.getContext("2d");
const imgData=ctx2d.createImageData(384,272);
const px32=new Uint32Array(imgData.data.buffer);
let wsV=null,frames=0,videoOn=false,videoWanted=false,videoHasFrame=false,videoPlaceholderMessage="VIDEO NOT CONNECTED",achunks=0;
const HEALTH_BROWSER={videoFramesTotal:0,videoRenderFps:0,videoWsConnects:0,videoWsDisconnects:0,
  audioWsConnects:0,audioWsDisconnects:0,audioReconnects:0,audioUnderruns:0,audioDroppedAhead:0};
let VIDEO_NO_FRAME_TIMER=null;
let wsA=null,actx=null,nextT=0,audioOn=false,audioWanted=false,audioState="off",audioRate=0;
let audioReconnectTimer=null;
const AUDIO_SOURCES=new Set(),AUDIO_MAX_AHEAD=0.32,AUDIO_START_LEAD=0.04;
let AUDIO_JUKE_STOP_MUTED=false,AUDIO_OUTPUT_GAIN=null,AUDIO_RECORD_DEST=null;
let JUKE_FADE_TIMER=null,JUKE_FADE_TRANSITION_TIMER=null,JUKE_FADE_TOKEN="",JUKE_FADE_STATE=null,JUKE_FADE_HELD=false;

setInterval(()=>{
  HEALTH_BROWSER.videoRenderFps=frames;
  $("#fps").textContent=frames+" fps";
  $("#fps").classList.toggle("on",frames>0);frames=0;
  audioRate=achunks;achunks=0;
  if(audioState==="live")setAudioState("live",audioRate);
},1000);
function drawFrame(buf){
  const b=new Uint8Array(buf);
  for(let i=0,p=0;i<b.length;i++){const v=b[i];
    px32[p++]=PAL32[v&15];px32[p++]=PAL32[v>>4]}
  ctx2d.putImageData(imgData,0,0);frames++;HEALTH_BROWSER.videoFramesTotal++;videoHasFrame=true;
  clearTimeout(VIDEO_NO_FRAME_TIMER);VIDEO_NO_FRAME_TIMER=null;const streamHint=$("#streamHint");if(streamHint)streamHint.style.display="none";
  screenEl.setAttribute("aria-label","Live C64 screen — click to focus, then keys are sent to the machine");
  // dynamic bezel: the stream includes the VIC border — sample it and let
  // the CSS frame follow (black for demos, strobing for loaders...)
  const bo=(4*384+4)*4,d=imgData.data,
        rgb=(d[bo]<<16)|(d[bo+1]<<8)|d[bo+2];
  if(rgb!==BEZEL.last){BEZEL.last=rgb;
    document.documentElement.style.setProperty("--bezel",
      "#"+rgb.toString(16).padStart(6,"0"))}}
const BEZEL={last:-1};
function bezelReset(){BEZEL.last=-1;
  document.documentElement.style.removeProperty("--bezel")}
function drawVideoPlaceholder(message="VIDEO NOT CONNECTED"){
  videoPlaceholderMessage=message;
  const w=screenEl.width,h=screenEl.height;
  ctx2d.save();ctx2d.imageSmoothingEnabled=false;
  ctx2d.fillStyle="#090b12";ctx2d.fillRect(0,0,w,h);

  // A deliberately simple C64-style disconnected display. Drawing it onto
  // the canvas means screenshots and video-only recordings reflect the real
  // stopped state rather than retaining a misleading frozen frame.
  ctx2d.fillStyle="#25205f";ctx2d.fillRect(55,42,274,171);
  ctx2d.strokeStyle="#7769cf";ctx2d.lineWidth=4;ctx2d.strokeRect(57,44,270,167);
  ctx2d.strokeStyle="#b7abff";ctx2d.lineWidth=3;
  ctx2d.strokeRect(163,72,58,45);                 // display icon
  ctx2d.beginPath();ctx2d.moveTo(174,125);ctx2d.lineTo(210,125);
  ctx2d.moveTo(192,117);ctx2d.lineTo(192,125);ctx2d.stroke();
  ctx2d.beginPath();ctx2d.moveTo(174,84);ctx2d.lineTo(210,105);
  ctx2d.moveTo(210,84);ctx2d.lineTo(174,105);ctx2d.stroke();
  ctx2d.textAlign="center";ctx2d.textBaseline="middle";
  const lines=String(message).split("\n");
  ctx2d.font=`bold ${lines.length>1?14:18}px monospace`;ctx2d.fillStyle="#ffffff";
  if(lines.length>1){ctx2d.fillText(lines[0],192,145);ctx2d.fillText(lines[1],192,164)}else ctx2d.fillText(lines[0],192,151);
  ctx2d.font="12px monospace";ctx2d.fillStyle="#b7abff";
  let audioLine="AUDIO OFF";
  if(audioState==="live")audioLine=`AUDIO STILL CONNECTED · ${audioRate}/S`;
  else if(audioState==="connecting")audioLine="AUDIO CONNECTING";
  else if(audioState==="reconnecting")audioLine="AUDIO RECONNECTING";
  else if(audioState==="error")audioLine="AUDIO ERROR";
  if(lines.length===1)ctx2d.fillText(audioLine,192,178);
  ctx2d.font="10px monospace";ctx2d.fillStyle="#7770a8";
  if(lines.length===1)ctx2d.fillText("PRESS START VIDEO TO CONNECT",192,199);
  ctx2d.restore();
  videoHasFrame=false;frames=0;$("#fps").textContent="0 fps";$("#fps").classList.remove("on");
  screenEl.setAttribute("aria-label",message.toLowerCase()+" — press Start video to connect");
}
function clearVideoCanvas(){drawVideoPlaceholder("VIDEO NOT CONNECTED")}
function openVideoWS(){
  const buf=$("#qMode").value||"1";
  wsV=new WebSocket((location.protocol==="https:"?"wss":"ws")+"://"+location.host
    +"/ws/video?buffer="+buf);
  wsV.binaryType="arraybuffer";
  wsV.onopen=()=>{HEALTH_BROWSER.videoWsConnects++};
  wsV.onmessage=ev=>drawFrame(ev.data);
  wsV.onerror=()=>{};
  wsV.onclose=()=>{
    HEALTH_BROWSER.videoWsDisconnects++;
    // The backend video disconnect handler performs the single coalesced
    // hardware release. Clear only browser-side held/queued state here so the
    // same socket close cannot create a second matrix REST request.
    matrixClearLocalState();
    const unexpected=videoWanted;
    videoOn=false;videoWanted=false;wsV=null;
    $("#btnVideo").textContent="Start video";$("#btnVideo").classList.add("primary");
    bezelReset();drawVideoPlaceholder(unexpected?"VIDEO DISCONNECTED":"VIDEO NOT CONNECTED");
  };
}
async function toggleVideo(){
  if(LINK_STATUS.link_type==="wifi"){toast("Streaming is not available over Wi-Fi. Switch to Ethernet to start video.","err");return}
  if(REC.active&&REC.mode!=="audio"&&videoOn){toast("Stop recording before stopping video","err");return}
  if(videoOn||videoWanted){videoWanted=false;uiInteractiveStart();try{await put("/api/stream/video/stop")}catch(e){}finally{uiInteractiveEnd()}
    if(wsV){const old=wsV;wsV=null;old.close()}videoOn=false;$("#btnVideo").textContent="Start video";
    bezelReset();drawVideoPlaceholder("VIDEO NOT CONNECTED");
    $("#btnVideo").classList.add("primary");return}
  videoWanted=true;drawVideoPlaceholder("VIDEO CONNECTING");
  uiInteractiveStart();
  try{await put("/api/stream/video/start")}catch(e){videoWanted=false;drawVideoPlaceholder("VIDEO ERROR");toast(e.message,"err");return}
  finally{uiInteractiveEnd()}
  openVideoWS();
  videoOn=true;$("#btnVideo").textContent="Stop video";$("#btnVideo").classList.remove("primary");
  clearTimeout(VIDEO_NO_FRAME_TIMER);VIDEO_NO_FRAME_TIMER=setTimeout(()=>{
    if(videoWanted&&!videoHasFrame&&LINK_STATUS.link_type==="unknown"){const hint=$("#streamHint");if(hint){hint.textContent="No video frames received. If this Ultimate is connected over Wi-Fi, streaming is not supported — use its Ethernet address.";hint.style.display="block"}}
  },5000);
  screenEl.focus()}

/* ---------- audio ---------- */
function ensureAudioOutputGain(){
  if(!actx)return null;
  if(!AUDIO_OUTPUT_GAIN){AUDIO_OUTPUT_GAIN=actx.createGain();AUDIO_OUTPUT_GAIN.gain.value=1;AUDIO_OUTPUT_GAIN.connect(actx.destination)}
  return AUDIO_OUTPUT_GAIN;
}
function audioRecordDestinationSet(dest){
  if(!AUDIO_OUTPUT_GAIN)ensureAudioOutputGain();
  if(AUDIO_RECORD_DEST&&AUDIO_OUTPUT_GAIN){try{AUDIO_OUTPUT_GAIN.disconnect(AUDIO_RECORD_DEST)}catch(e){}}
  AUDIO_RECORD_DEST=dest||null;if(AUDIO_RECORD_DEST&&AUDIO_OUTPUT_GAIN)AUDIO_OUTPUT_GAIN.connect(AUDIO_RECORD_DEST);
}
function jukeFadeGainImmediate(value=1){
  if(!actx)return;const node=ensureAudioOutputGain(),g=node.gain,now=actx.currentTime;
  try{g.cancelScheduledValues(now)}catch(e){}g.setValueAtTime(Math.max(0,Math.min(1,value)),now);
}
function jukeFadeCancel(restore=true){
  clearTimeout(JUKE_FADE_TIMER);clearTimeout(JUKE_FADE_TRANSITION_TIMER);JUKE_FADE_TIMER=JUKE_FADE_TRANSITION_TIMER=null;
  JUKE_FADE_TOKEN="";JUKE_FADE_STATE=null;JUKE_FADE_HELD=false;
  if(restore)jukeFadeGainImmediate(1);
}
async function jukeFadeAwaitReplacement(token){
  if(!JUKE_FADE_HELD||token!==JUKE_FADE_TOKEN)return;
  try{
    const s=await api("/api/juke",{timeoutMs:1500});
    const current=String(s?.playback_id||s?.active_browser_fade?.playback_id||"");
    if(!s?.playing||!current||current!==token){
      flushBrowserAudio();JUKE_FADE_HELD=false;jkRender(s);return;
    }
  }catch(e){}
  if(JUKE_FADE_HELD&&token===JUKE_FADE_TOKEN){
    JUKE_FADE_TRANSITION_TIMER=setTimeout(()=>{JUKE_FADE_TRANSITION_TIMER=null;jukeFadeAwaitReplacement(token)},125);
  }
}
function jukeFadeHoldSilence(token=JUKE_FADE_TOKEN){
  if(!actx||token!==JUKE_FADE_TOKEN)return;
  jukeFadeGainImmediate(0);JUKE_FADE_HELD=true;
  clearTimeout(JUKE_FADE_TRANSITION_TIMER);
  JUKE_FADE_TRANSITION_TIMER=setTimeout(()=>{JUKE_FADE_TRANSITION_TIMER=null;jukeFadeAwaitReplacement(token)},50);
}
function jukeFadeRamp(duration,initial=1,token=JUKE_FADE_TOKEN){
  if(!actx||!audioWanted)return;const node=ensureAudioOutputGain(),g=node.gain,now=actx.currentTime,d=Math.max(0.02,Number(duration)||0);
  try{g.cancelScheduledValues(now)}catch(e){}g.setValueAtTime(Math.max(0,Math.min(1,initial)),now);g.linearRampToValueAtTime(0,now+d);
  JUKE_FADE_HELD=false;clearTimeout(JUKE_FADE_TRANSITION_TIMER);
  JUKE_FADE_TRANSITION_TIMER=setTimeout(()=>{JUKE_FADE_TRANSITION_TIMER=null;if(token===JUKE_FADE_TOKEN)jukeFadeHoldSilence(token)},d*1000+20);
}
function jukeFadeSync(s,force=false){
  const f=s?.active_browser_fade||{},enabled=!!(s?.playing&&f.enabled&&Number(f.duration_secs)>0);
  const stateToken=String(s?.playback_id||f.playback_id||"");
  const changed=!!(JUKE_FADE_TOKEN&&stateToken&&stateToken!==JUKE_FADE_TOKEN);
  if(changed&&JUKE_FADE_HELD)flushBrowserAudio();
  JUKE_FADE_STATE=f;if(!enabled){jukeFadeCancel(true);return}
  const token=String(f.playback_id||stateToken||"");
  clearTimeout(JUKE_FADE_TIMER);clearTimeout(JUKE_FADE_TRANSITION_TIMER);JUKE_FADE_TIMER=JUKE_FADE_TRANSITION_TIMER=null;JUKE_FADE_TOKEN=token;JUKE_FADE_HELD=false;
  if(!actx||!audioWanted)return;
  const duration=Math.max(0.05,Number(f.duration_secs)||0),starts=Number(f.starts_in_secs),remaining=Math.max(0,Number(f.remaining_secs)||0);
  if(remaining<=0){jukeFadeHoldSilence(token);return}
  if(starts>0){jukeFadeGainImmediate(1);JUKE_FADE_TIMER=setTimeout(()=>{JUKE_FADE_TIMER=null;jukeFadeRamp(duration,1,token)},starts*1000);return}
  const gain=Math.max(0,Math.min(1,remaining/duration));jukeFadeRamp(remaining,gain,token);
}
function jukeFadePrepareReplacement(){
  jukeFadeCancel(false);jukeFadeGainImmediate(0);flushBrowserAudio();
}
function flushBrowserAudio(){
  for(const src of [...AUDIO_SOURCES]){try{src.onended=null;src.stop()}catch(e){}try{src.disconnect()}catch(e){}}
  AUDIO_SOURCES.clear();nextT=actx?actx.currentTime:0;achunks=0;
}
function playChunk(ab){if(!actx||!audioWanted||AUDIO_JUKE_STOP_MUTED||JUKE_FADE_HELD)return;const i16=new Int16Array(ab);const n=i16.length>>1;if(!n)return;
  const now=actx.currentTime;if(nextT&&nextT<now-0.015)HEALTH_BROWSER.audioUnderruns++;
  if(nextT>now+AUDIO_MAX_AHEAD){HEALTH_BROWSER.audioDroppedAhead++;return}
  const buf=actx.createBuffer(2,n,47983);
  const L=buf.getChannelData(0),R=buf.getChannelData(1);
  for(let i=0;i<n;i++){L[i]=i16[2*i]/32768;R[i]=i16[2*i+1]/32768}
  const src=actx.createBufferSource();src.buffer=buf;src.connect(ensureAudioOutputGain());
  const t=Math.max(now+AUDIO_START_LEAD,nextT);if(t>now+AUDIO_MAX_AHEAD){HEALTH_BROWSER.audioDroppedAhead++;try{src.disconnect()}catch(e){}return}
  AUDIO_SOURCES.add(src);src.onended=()=>{AUDIO_SOURCES.delete(src);try{src.disconnect()}catch(e){}};
  src.start(t);nextT=t+n/47983}
function setAudioState(state,rate=audioRate){
  audioState=state;audioRate=rate;
  const badge=$("#aud"),button=$("#btnAudio");
  const text={off:"Audio off",connecting:"Audio connecting…",reconnecting:"Audio reconnecting…",error:"Audio error"};
  badge.textContent=state==="live"?`Audio live · ${rate}/s`:(text[state]||"Audio off");
  badge.classList.toggle("on",state==="live");
  badge.classList.toggle("warn",state==="connecting"||state==="reconnecting");
  badge.classList.toggle("bad",state==="error");
  badge.title=state==="live"?"Audio WebSocket chunks received per second":"Ultimate audio stream status";
  button.textContent=audioWanted?"Stop audio":"Start audio";
  if(!videoHasFrame)drawVideoPlaceholder(videoPlaceholderMessage);
}
function openAudioWS(reconnecting=false){
  clearTimeout(audioReconnectTimer);audioReconnectTimer=null;if(reconnecting)HEALTH_BROWSER.audioReconnects++;
  setAudioState(reconnecting?"reconnecting":"connecting");
  wsA=new WebSocket((location.protocol==="https:"?"wss":"ws")+"://"+location.host+"/ws/audio");
  wsA.binaryType="arraybuffer";
  wsA.onopen=()=>{audioOn=true;HEALTH_BROWSER.audioWsConnects++};
  wsA.onmessage=ev=>{audioOn=true;achunks++;if(audioState!=="live")setAudioState("live",Math.max(1,audioRate));playChunk(ev.data)};
  wsA.onerror=()=>{};
  wsA.onclose=()=>{
    HEALTH_BROWSER.audioWsDisconnects++;
    // Audio has no relationship to keyboard capture; never release CIA1 input
    // merely because the audio stream reconnects or stops.
    wsA=null;
    if(!audioWanted){audioOn=false;setAudioState("off",0);return}
    audioOn=false;setAudioState("reconnecting",0);
    audioReconnectTimer=setTimeout(()=>{if(audioWanted)openAudioWS(true)},1200);
  };
}
async function toggleAudio(){
  if(LINK_STATUS.link_type==="wifi"){toast("Streaming is not available over Wi-Fi. Switch to Ethernet to start audio.","err");return}
  if(REC.active&&REC.mode!=="video"&&audioWanted){toast("Stop recording before stopping audio","err");return}
  if(audioWanted){audioWanted=false;clearTimeout(audioReconnectTimer);audioReconnectTimer=null;flushBrowserAudio();
    uiInteractiveStart();try{await put("/api/stream/audio/stop")}catch(e){}finally{uiInteractiveEnd()}
    if(wsA){const old=wsA;wsA=null;old.close()}audioOn=false;setAudioState("off",0);return}
  audioWanted=true;setAudioState("connecting",0);
  try{
    const AudioCtor=window.AudioContext||window.webkitAudioContext;
    if(!AudioCtor)throw new Error("This browser does not support Web Audio");
    if(!actx)actx=new AudioCtor();
    await actx.resume();ensureAudioOutputGain();flushBrowserAudio();jukeFadeSync(JK.state,true);
  }catch(e){audioWanted=false;audioOn=false;setAudioState("error",0);toast(e.message,"err");return}
  if(actx.state!=="running")
    toast("Browser blocked audio playback (AudioContext: "+actx.state+")","err");
  uiInteractiveStart();
  try{await put("/api/stream/audio/start")}catch(e){audioWanted=false;audioOn=false;setAudioState("error",0);toast(e.message,"err");return}
  finally{uiInteractiveEnd()}
  audioOn=true;openAudioWS(false)}

/* ---------- flexible video / audio recording ---------- */
const REC={active:false,recorder:null,chunks:[],started:0,timer:null,stopTimer:null,stream:null,
  audioDest:null,mime:"",ext:"webm",format:"webm",mode:"combined",fileHandle:null,copyTimer:null,captureCanvas:null,
  statsBase:null,statsLast:null,statsPoll:0,quality:null};
const REC_QUALITY={compact:{video:1500000,audio:96000},standard:{video:4500000,audio:128000},high:{video:9000000,audio:192000}};
const REC_LABELS={combined:"Combined",video:"Video only",audio:"Audio only",compact:"Compact",standard:"Standard",high:"High",native:"Native",2:"Pixel-perfect 2×",auto:"Auto",mp4:"MP4",webm:"WebM"};
function recTime(){
  const sec=Math.max(0,Math.floor((Date.now()-REC.started)/1000));
  return String(Math.floor(sec/60)).padStart(2,"0")+":"+String(sec%60).padStart(2,"0");
}
function recSettings(){return {
  mode:$("#recMode")?.value||"combined",duration:+($("#recDuration")?.value||0),
  quality:$("#recQuality")?.value||"standard",format:$("#recFormat")?.value||"auto",
  scale:$("#recScale")?.value||"native",filename:$("#recFilename")?.value||"u64deck-{date}-{time}-{mode}",
  choose:!!$("#recChooseLocation")?.checked,open:!!$("#recordOptions")?.open};}
function recordingCandidates(mode,format="auto"){
  const mp4=mode==="audio"
    ? [{mime:"audio/mp4;codecs=mp4a.40.2",ext:"mp4",format:"mp4"},{mime:"audio/mp4",ext:"m4a",format:"mp4"}]
    : mode==="video"
      ? [{mime:"video/mp4;codecs=avc1.42E01E",ext:"mp4",format:"mp4"},{mime:"video/mp4",ext:"mp4",format:"mp4"}]
      : [{mime:"video/mp4;codecs=avc1.42E01E,mp4a.40.2",ext:"mp4",format:"mp4"},{mime:"video/mp4",ext:"mp4",format:"mp4"}];
  const webm=mode==="audio"
    ? [{mime:"audio/webm;codecs=opus",ext:"webm",format:"webm"},{mime:"audio/webm",ext:"webm",format:"webm"}]
    : mode==="video"
      ? [{mime:"video/webm;codecs=vp9",ext:"webm",format:"webm"},{mime:"video/webm;codecs=vp8",ext:"webm",format:"webm"},{mime:"video/webm",ext:"webm",format:"webm"}]
      : [{mime:"video/webm;codecs=vp9,opus",ext:"webm",format:"webm"},{mime:"video/webm;codecs=vp8,opus",ext:"webm",format:"webm"},{mime:"video/webm",ext:"webm",format:"webm"}];
  return format==="mp4"?mp4:format==="webm"?webm:[...mp4,...webm];
}
function chooseRecordingFormat(mode,format="auto"){
  if(typeof MediaRecorder==="undefined")return null;
  const supported=m=>!MediaRecorder.isTypeSupported||MediaRecorder.isTypeSupported(m);
  return recordingCandidates(mode,format).find(x=>supported(x.mime))||null;
}
function recResolution(cfg=recSettings()){
  if(cfg.mode==="audio")return "audio only";
  return cfg.scale==="2"?"768×544":"384×272";
}
function recBitrate(cfg=recSettings()){
  const q=REC_QUALITY[cfg.quality]||REC_QUALITY.standard;
  let bits=0;if(cfg.mode!=="audio")bits+=q.video;if(cfg.mode!=="video")bits+=q.audio;
  return {bits,label:bits>=1000000?(bits/1000000).toFixed(1)+" Mb/s":Math.round(bits/1000)+" kb/s"};
}
function updateRecSummary(){
  const cfg=recSettings(),choice=chooseRecordingFormat(cfg.mode,cfg.format),summary=$("#recSummary"),cap=$("#recCapability"),mp4Opt=$("#recFormat option[value='mp4']");
  const mp4Supported=!!chooseRecordingFormat(cfg.mode,"mp4");
  if(mp4Opt){mp4Opt.disabled=!mp4Supported;mp4Opt.textContent=mp4Supported?"MP4":"MP4 (unsupported in this browser)"}
  if(cfg.format==="mp4"&&!mp4Supported){$("#recFormat").value="auto";cfg.format="auto"}
  const actual=choice?choice.format.toUpperCase():REC_LABELS[cfg.format];
  if(summary)summary.textContent=`${REC_LABELS[cfg.mode]} · ${REC_LABELS[cfg.quality]} · ${actual}`;
  if(cap){
    if(typeof MediaRecorder==="undefined")cap.textContent="This browser does not provide MediaRecorder.";
    else if(!choice)cap.textContent=`${REC_LABELS[cfg.format]} is not supported for ${REC_LABELS[cfg.mode].toLowerCase()} recording in this browser.`;
    else cap.textContent=`Will record ${choice.format.toUpperCase()} · ${recResolution(cfg)} · target ${recBitrate(cfg).label} · ${choice.mime}`;
  }
}
function recUi(){
  const b=$("#btnRecord"),st=$("#recStatus"),label=$("#recButtonLabel"),dot=$("#recordDot");if(!b||!st)return;
  if(label)label.textContent=REC.active?"Stop recording":"Record";
  b.classList.toggle("recording-control",REC.active);dot?.classList.toggle("active",REC.active);
  const display=REC.active?"inline-block":"none";if(st.style.display!==display)st.style.display=display;
  if(REC.active){const sec=Math.max(0,(Date.now()-REC.started)/1000),miB=(sec*(REC.quality?.bits||0)/8/1048576).toFixed(1),text=`${recTime()} · ~${miB} MiB`;if(st.textContent!==text)st.textContent=text;}
}
function saveRecSettings(){try{localStorage.setItem("u64deck.recording",JSON.stringify(recSettings()))}catch(e){}updateRecSummary()}
function loadRecSettings(){
  let v={};try{v=JSON.parse(localStorage.getItem("u64deck.recording")||"{}")}catch(e){}
  for(const [id,key] of [["recMode","mode"],["recDuration","duration"],["recQuality","quality"],["recFormat","format"],["recScale","scale"],["recFilename","filename"]]){
    const el=$("#"+id);if(el&&v[key]!=null)el.value=String(v[key])}
  if($("#recChooseLocation"))$("#recChooseLocation").checked=!!v.choose;
  if($("#recordOptions"))$("#recordOptions").open=!!v.open;
  document.querySelectorAll("#recordOptions select,#recordOptions input").forEach(el=>{el.addEventListener("change",saveRecSettings);el.addEventListener("input",updateRecSummary)});
  $("#recordOptions")?.addEventListener("toggle",saveRecSettings);updateRecSummary();
}
function recordingFilename(mode=REC.mode,ext=REC.ext||"webm"){
  const d=new Date(),pad=n=>String(n).padStart(2,"0"),date=`${d.getFullYear()}${pad(d.getMonth()+1)}${pad(d.getDate())}`,
    time=`${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
  let name=(recSettings().filename||"u64deck-{date}-{time}-{mode}")
    .replaceAll("{date}",date).replaceAll("{time}",time).replaceAll("{mode}",mode);
  name=name.replace(/[<>:"/\\|?*\x00-\x1f]/g,"-").replace(/[. ]+$/g,"").slice(0,180)||`u64deck-${date}-${time}`;
  name=name.replace(/\.(webm|mp4|m4a)$/i,"");return `${name}.${ext}`;
}
function recordingVideoStream(scale){
  if(scale!=="2")return screenEl.captureStream(50);
  const c=document.createElement("canvas");c.width=768;c.height=544;const x=c.getContext("2d");x.imageSmoothingEnabled=false;
  const copy=()=>x.drawImage(screenEl,0,0,c.width,c.height);copy();REC.copyTimer=setInterval(copy,20);REC.captureCanvas=c;
  return c.captureStream(50);
}
async function updateRecDrops(force=false){
  if(!REC.active&&!force)return;const now=Date.now();if(!force&&now-REC.statsPoll<1000)return;REC.statsPoll=now;
  try{REC.statsLast=await api("/api/stream/stats");const b=REC.statsBase||REC.statsLast,
    vd=Math.max(0,(REC.statsLast.video?.dropped||0)-(b.video?.dropped||0)),
    ad=Math.max(0,(REC.statsLast.audio?.dropped||0)-(b.audio?.dropped||0));
    $("#recDrops").textContent=`Drops: video ${vd} · audio ${ad}`;
  }catch(e){}
}
function ebmlReadVint(bytes,offset,id=false){
  if(offset>=bytes.length)return null;let mask=0x80,len=1,first=bytes[offset];
  while(len<=8&&!(first&mask)){mask>>=1;len++}
  if(len>8||offset+len>bytes.length)return null;let value=id?first:(first&(mask-1));
  for(let i=1;i<len;i++)value=value*256+bytes[offset+i];
  const max=id?null:Math.pow(2,7*len)-1;return {len,value,unknown:!id&&value===max};
}
function ebmlEncodeVint(value,len){
  const max=Math.pow(2,7*len)-2;if(!Number.isFinite(value)||value<0||value>max)return null;
  const out=new Uint8Array(len);let n=value;for(let i=len-1;i>=0;i--){out[i]=n&255;n=Math.floor(n/256)}out[0]|=1<<(8-len);return out;
}
function ebmlElement(bytes,offset,end=bytes.length){
  const id=ebmlReadVint(bytes,offset,true);if(!id)return null;const sizeStart=offset+id.len,size=ebmlReadVint(bytes,sizeStart,false);if(!size)return null;
  const dataStart=sizeStart+size.len,dataEnd=size.unknown?end:Math.min(end,dataStart+size.value);
  if(dataStart>end||dataEnd<dataStart)return null;return {id:id.value,idStart:offset,sizeStart,sizeLen:size.len,sizeValue:size.value,sizeUnknown:size.unknown,dataStart,dataEnd};
}
function ebmlFind(bytes,start,end,target){
  let off=start;while(off<end){const el=ebmlElement(bytes,off,end);if(!el)return null;if(el.id===target)return el;if(el.dataEnd<=off)return null;off=el.dataEnd}return null;
}
function ebmlUInt(bytes,start,end){let v=0;for(let i=start;i<end;i++)v=v*256+bytes[i];return v}
async function fixWebmDuration(blob,durationMs){
  if(!blob?.type?.includes("webm")||!Number.isFinite(durationMs)||durationMs<=0)return blob;
  try{
    const bytes=new Uint8Array(await blob.arrayBuffer()),segment=ebmlFind(bytes,0,bytes.length,0x18538067);if(!segment)return blob;
    const info=ebmlFind(bytes,segment.dataStart,segment.dataEnd,0x1549A966);if(!info||info.sizeUnknown)return blob;
    const scaleEl=ebmlFind(bytes,info.dataStart,info.dataEnd,0x2ad7b1),scale=scaleEl?ebmlUInt(bytes,scaleEl.dataStart,scaleEl.dataEnd):1000000;
    const ticks=durationMs*1000000/Math.max(1,scale),duration=ebmlFind(bytes,info.dataStart,info.dataEnd,0x4489);
    if(duration&&(duration.dataEnd-duration.dataStart===8||duration.dataEnd-duration.dataStart===4)){
      const out=bytes.slice(),view=new DataView(out.buffer);if(duration.dataEnd-duration.dataStart===8)view.setFloat64(duration.dataStart,ticks,false);else view.setFloat32(duration.dataStart,ticks,false);
      return new Blob([out],{type:blob.type});
    }
    const payload=new Uint8Array(11);payload.set([0x44,0x89,0x88],0);new DataView(payload.buffer).setFloat64(3,ticks,false);
    const newInfoSize=ebmlEncodeVint(info.sizeValue+payload.length,info.sizeLen);if(!newInfoSize)return blob;
    if(!segment.sizeUnknown&&!ebmlEncodeVint(segment.sizeValue+payload.length,segment.sizeLen))return blob;
    const out=new Uint8Array(bytes.length+payload.length);out.set(bytes.subarray(0,info.sizeStart),0);out.set(newInfoSize,info.sizeStart);
    out.set(bytes.subarray(info.dataStart,info.dataEnd),info.dataStart);out.set(payload,info.dataEnd);out.set(bytes.subarray(info.dataEnd),info.dataEnd+payload.length);
    if(!segment.sizeUnknown)out.set(ebmlEncodeVint(segment.sizeValue+payload.length,segment.sizeLen),segment.sizeStart);
    return new Blob([out],{type:blob.type});
  }catch(e){console.warn("WebM duration repair skipped",e);return blob}
}

async function saveRecordingBlob(blob){
  const filename=recordingFilename(REC.mode,REC.ext);
  if(REC.fileHandle){try{const w=await REC.fileHandle.createWritable();await w.write(blob);await w.close();return filename}catch(e){toast("Chosen location failed; using browser download: "+e.message,"err")}}
  const url=URL.createObjectURL(blob),a=document.createElement("a");a.href=url;a.download=filename;
  document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);return filename;
}
async function startRecording(){
  if(LINK_STATUS.link_type==="wifi"){toast("Recording requires video or audio streaming and is not available over Wi-Fi","err");return}
  const cfg=recSettings();saveRecSettings();REC.mode=cfg.mode;
  if(typeof MediaRecorder==="undefined"){toast("This browser cannot record media","err");return}
  if(cfg.mode!=="audio"&&!screenEl.captureStream){toast("This browser cannot record canvas video","err");return}
  const choice=chooseRecordingFormat(cfg.mode,cfg.format);
  if(!choice){toast(`${REC_LABELS[cfg.format]} recording is not supported for this mode; choose Auto or another format`,"err");$("#recordOptions").open=true;return}
  REC.mime=choice.mime;REC.ext=choice.ext;REC.format=choice.format;REC.fileHandle=null;
  if(cfg.choose&&window.showSaveFilePicker){
    const major=choice.mime.split(";")[0],accept={};accept[major]=["."+choice.ext];
    try{REC.fileHandle=await showSaveFilePicker({suggestedName:recordingFilename(cfg.mode,choice.ext),types:[{description:`${choice.format.toUpperCase()} media`,accept}]})}
    catch(e){if(e.name==="AbortError")return;toast("Save location unavailable; using Downloads","err")}
  }else if(cfg.choose&&!window.showSaveFilePicker)toast("This browser cannot choose a save location; using Downloads","err");
  if(cfg.mode!=="audio"&&!videoOn)await toggleVideo();if(cfg.mode!=="video"&&!audioOn)await toggleAudio();
  if((cfg.mode!=="audio"&&!videoOn)||(cfg.mode!=="video"&&(!audioOn||!actx))){toast("Required stream could not be started","err");return}
  try{
    const tracks=[];if(cfg.mode!=="audio")tracks.push(...recordingVideoStream(cfg.scale).getVideoTracks());
    if(cfg.mode!=="video"){REC.audioDest=actx.createMediaStreamDestination();audioRecordDestinationSet(REC.audioDest);tracks.push(...REC.audioDest.stream.getAudioTracks())}
    REC.stream=new MediaStream(tracks);REC.chunks=[];const q=REC_QUALITY[cfg.quality]||REC_QUALITY.standard,opts={mimeType:REC.mime};
    if(cfg.mode!=="audio")opts.videoBitsPerSecond=q.video;if(cfg.mode!=="video")opts.audioBitsPerSecond=q.audio;
    REC.quality={...q,bits:recBitrate(cfg).bits};
    try{REC.recorder=new MediaRecorder(REC.stream,opts)}
    catch(first){REC.recorder=new MediaRecorder(REC.stream,{mimeType:REC.mime});toast("Browser ignored the requested bitrate preset","err")}
    REC.mime=REC.recorder.mimeType||REC.mime;REC.ext=REC.mime.includes("mp4")?(cfg.mode==="audio"?"m4a":"mp4"):"webm";
    REC.recorder.ondataavailable=e=>{if(e.data&&e.data.size)REC.chunks.push(e.data)};
    REC.recorder.onerror=e=>toast("Recording error: "+(e.error?.message||e.message||"unknown"),"err");
    REC.recorder.onstop=async()=>{
      const elapsed=Math.max(0,(Date.now()-REC.started)/1000);await updateRecDrops(true);
      const fallbackMime=REC.mode==="audio"?"audio/webm":"video/webm";let blob=new Blob(REC.chunks,{type:REC.mime||fallbackMime});
      if(REC.ext==="webm")blob=await fixWebmDuration(blob,elapsed*1000);
      const name=await saveRecordingBlob(blob);
      REC.stream?.getTracks().forEach(t=>t.stop());REC.stream=null;audioRecordDestinationSet(null);REC.audioDest=null;REC.chunks=[];REC.fileHandle=null;
      clearInterval(REC.copyTimer);REC.copyTimer=null;REC.captureCanvas=null;
      toast(`${name} saved · ${Math.round(elapsed)} sec · ${(blob.size/1048576).toFixed(1)} MiB ✓`,"ok");updateRecSummary();
    };
    REC.statsBase=await api("/api/stream/stats").catch(()=>null);REC.statsLast=REC.statsBase;REC.statsPoll=0;
    REC.recorder.start(1000);REC.active=true;REC.started=Date.now();REC.timer=setInterval(()=>{recUi();updateRecDrops()},250);recUi();updateRecSummary();
    if(cfg.duration>0)REC.stopTimer=setTimeout(stopRecording,cfg.duration*1000);
    if(!localStorage.getItem("u64deck.recording.seen")){localStorage.setItem("u64deck.recording.seen","1");$("#recordOptions").open=true}
    toast(`Recording ${cfg.mode.replace("combined","video + audio")} as ${REC.format.toUpperCase()}…`,"ok");
  }catch(e){audioRecordDestinationSet(null);REC.audioDest=null;REC.stream?.getTracks().forEach(t=>t.stop());REC.stream=null;clearInterval(REC.copyTimer);toast(e.message,"err")}
}
function stopRecording(){
  if(!REC.active)return;REC.active=false;clearInterval(REC.timer);clearTimeout(REC.stopTimer);REC.timer=REC.stopTimer=null;recUi();updateRecSummary();
  try{if(REC.recorder&&REC.recorder.state!=="inactive")REC.recorder.stop()}catch(e){toast(e.message,"err")}
}
function toggleRecording(){return REC.active?stopRecording():startRecording()}
window.addEventListener("beforeunload",e=>{if(REC.active){e.preventDefault();e.returnValue="A u64deck recording is still in progress."}});

/* ---------- device file system ---------- */
let DEFAULT_MOUNT_MODE="unlinked";
let DRIVE_STATE={a:{},b:{}};
let DRIVE_STATUS_RETRY_TIMER=null;
let DRIVE_STATUS_READY=false;
function mountMode(){return $("#mountModeDefault")?.value||DEFAULT_MOUNT_MODE||"unlinked"}
function mountModeShort(mode=mountMode()){return mode==="readonly"?"RO":mode==="readwrite"?"RW":"UNLINKED"}
function mountModeLong(mode=mountMode()){return mode==="readonly"?"Read-only":mode==="readwrite"?"Read/write":"Unlinked — temporary writes"}
function updateMountModeLabels(){
  document.querySelectorAll(".mount-mode-action[data-mount-base]").forEach(btn=>{
    btn.textContent=`${btn.dataset.mountBase} · ${mountModeShort()}`;
    btn.dataset.tip=`${btn.dataset.mountBase} using ${mountModeLong()}. `+
      (mountMode()==="readonly"?"The image is protected from drive writes.":mountMode()==="readwrite"?"Drive writes are saved to the original image.":"Drive writes are temporary and the original image is unchanged.");
  });
}
function syncMountMode(mode){
  DEFAULT_MOUNT_MODE=mode;
  for(const id of ["mountModeDefault","localmode","inspMode"]){const el=$("#"+id);if(el)el.value=mode}
  updateMountModeLabels();
}
async function loadMountOptions(){
  try{const r=await api("/api/mount/options");syncMountMode(r.default_mode||"unlinked")}catch(e){syncMountMode("unlinked")}
}
async function mountModeChanged(source){
  const mode=(source?.value)||$("#mountModeDefault")?.value||"unlinked";syncMountMode(mode);
  try{await api("/api/mount/options",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({default_mode:mode})});
    toast(`Default mount mode: ${mountModeLong(mode)} ✓`,"ok")}
  catch(e){toast(e.message,"err")}
}
const FS={loaded:false,path:"/"};
const IMG_EXT=[".d64",".d71",".d81"];
const isImage=n=>IMG_EXT.some(e=>n.toLowerCase().endsWith(e));
const isRunnable=n=>[".prg",".crt",".sid"].some(e=>n.toLowerCase().endsWith(e));
function joinPath(a,b){return (a==="/"?"":a)+"/"+b}
function parentPath(p){
  if(!p||p==="/")return "/";
  const cut=p.replace(/\/+$/,"").split("/").slice(0,-1).join("/");
  return cut||"/";
}
function fsUp(){if(FS.path!=="/")fsGo(parentPath(FS.path))}
async function fsGo(path){
  FS.loaded=true;FS.path=path;
  $("#fslist").textContent="Loading…";
  renderCrumbs(path);
  const atVirtualRoot=(path==="/");
  const up=$("#btnUp");if(up)up.disabled=atVirtualRoot;
  const nd=$("#newDiskBtn");
  if(nd){
    nd.disabled=atVirtualRoot;
    nd.title=atVirtualRoot
      ? "Open USB0, SD, Flash, Temp or another storage folder first"
      : "Create a blank disk image in "+path;
  }
  const ndCreate=$("#newDiskCreateBtn");if(ndCreate)ndCreate.disabled=atVirtualRoot;
  const ndHint=$("#newDiskHint");
  if(ndHint)ndHint.textContent=atVirtualRoot
    ? "The Ultimate's top-level / is a virtual device list and cannot contain files. Open a storage device or folder first."
    : "Formatted blank image, created by the firmware in "+path+". (G64 isn't exposed by the device API — use the Ultimate menu for that one.)";
  if(atVirtualRoot)$("#newDiskForm").style.display="none";
  try{
    const r=await api("/api/fs?path="+encodeURIComponent(path));
    const parentRow=path==="/"?"":`<tr class="fsentry" onclick="fsUp()">
      <td><span class="dirmark">▴</span> ..</td><td></td><td class="hint" style="text-align:right">Up</td></tr>`;
    const rows=parentRow+r.entries.map(e=>{
      const full=joinPath(path,e.name);
      if(e.dir){
        const fav=itemSpec("folder",e.name,full,"fs_browse",{path:full});
        return `<tr class="fsentry" onclick="fsGo('${jsq(full)}')">
          <td><span class="dirmark">▸</span> ${esc(e.name)}/</td><td></td><td style="text-align:right">${starButton(fav)}</td></tr>`;
      }
      let acts="";
      let rowClick="";
      if(isImage(e.name)){
        rowClick=` onclick="rowBrowse(event,'${jsq(full)}')" style="cursor:pointer" title="Click to browse inside"`;
        const fav=itemSpec("disk",e.name,full,"disk_run",{path:full});
        acts=starButton(fav)+` <button class="mini" onclick="queueAdd('${jsq(full)}')" title="Add to disk swap queue">Add to Swap Queue</button>
        <button class="mini primary tip mount-mode-action" data-mount-base="Mount & Run" data-tip="Mount with the selected safety mode, reset the C64, then load and run the first program." onclick="mountRunDevice('${jsq(full)}')">Mount & Run · ${mountModeShort()}</button>
        <button class="mini" onclick="imgOpenDevice('${jsq(full)}')">Open Image</button>
        <button class="mini tip mount-mode-action" data-mount-base="Mount to A" data-tip="Mount to drive A without resetting the C64." onclick="mountDevice('${jsq(full)}','a')">Mount to A · ${mountModeShort()}</button>
        <button class="mini tip mount-mode-action" data-mount-base="Mount to B" data-tip="Mount to drive B without resetting the C64." onclick="mountDevice('${jsq(full)}','b')">Mount to B · ${mountModeShort()}</button>
        <button class="mini tip" data-tip="Create a timestamped sibling copy on Ultimate storage." onclick="duplicateImage('${jsq(full)}')">Duplicate Image</button>
        <button class="mini tip" data-tip="Create a timestamped backup, then mount the original read/write on drive A." onclick="backupMountRW('${jsq(full)}','a')">Back Up & Mount RW</button>`;
      }
      else if(isRunnable(e.name)){
        const fav=itemSpec("program",e.name,full,"program_run",{path:full});
        acts=starButton(fav)+` <button class="mini" title="DMA run — loads straight into RAM and runs, no drive involved" onclick="runDevice('${jsq(full)}')">Run</button>`;
      }
      acts+=` <a class="mini" style="color:var(--dim)" href="/api/fs/download?path=${encodeURIComponent(full)}">⤓</a>`;
      return `<tr${rowClick}><td>${esc(e.name)}</td><td class="hint">${fmtSize(e.size)}</td><td style="text-align:right">${acts}</td></tr>`;
    }).join("");
    $("#fslist").innerHTML=rows?`<table><tbody>${rows}</tbody></table>`:'<span class="hint">Empty directory</span>';
  }catch(e){$("#fslist").innerHTML=`<span style="color:var(--err)">${esc(e.message)}</span>`}
  idxButtonRefresh();
}
function renderCrumbs(path){
  const parts=path.split("/").filter(Boolean);let acc="";
  let html=`<a onclick="fsGo('/')">/</a>`;
  for(const p of parts){acc+="/"+p;html+=` <a onclick="fsGo('${jsq(acc)}')">${esc(p)}</a> /`}
  $("#crumbs").innerHTML=html}
const esc=s=>String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;")
  .replace(/>/g,"&gt;").replace(/'/g,"&#39;").replace(/"/g,"&quot;");
// For values embedded in single-quoted JavaScript inside a double-quoted HTML
// attribute: escape JavaScript first, then the HTML attribute delimiters.
const jsq=s=>String(s).replace(/\\/g,"\\\\").replace(/'/g,"\\'")
  .replace(/\r/g,"\\r").replace(/\n/g,"\\n").replace(/</g,"\\x3c")
  .replace(/>/g,"\\x3e").replace(/&/g,"&amp;").replace(/"/g,"&quot;");
const fmtSize=n=>!n?"":n>1048576?(n/1048576).toFixed(1)+"M":n>1024?(n/1024|0)+"K":n+"B";
function rowBrowse(ev,path){
  if(ev.target.closest("button,a"))return;   // buttons keep their own actions
  imgOpenDevice(path);
}
let FSRCH={running:false};
let IDX={poll:null,last:null};
let LOCAL_VOLUMES=[];
const fmtDuration=s=>{
  if(s==null||!isFinite(s))return "";
  s=Math.max(0,Math.round(s));
  if(s<60)return s+"s";
  const m=Math.floor(s/60),sec=s%60;
  if(m<60)return m+"m "+String(sec).padStart(2,"0")+"s";
  const h=Math.floor(m/60),min=m%60;
  return h+"h "+String(min).padStart(2,"0")+"m";
};
function indexLocalCover(path,state=IDX.last){
  path=(path||"/").replace(/\/+$/,"")||"/";
  return (state?.local_imports||[]).some(item=>{
    const root=(item.root||"/").replace(/\/+$/,"")||"/";
    return root==="/"||path===root||path.startsWith(root+"/");
  });
}
function idxButtonRefresh(){
  const btn=$("#idxBtn");if(!btn||IDX.last?.running)return;
  const verify=indexLocalCover(FS.path||"/",IDX.last);
  btn.textContent=verify?"🔎 Verify Index":"🗂 Build Index";
  btn.title=verify
    ?"Optional full FTP verification of the local USB import; normal browsing already refreshes individual folders"
    :"Walk this subtree once and store folders plus disk-image directories in SQLite";
}
async function localVolumesLoad(){
  const sel=$("#localVolume");
  if(!sel)return;
  try{
    const r=await api("/api/local/volumes");
    LOCAL_VOLUMES=r.volumes||[];
    sel.innerHTML='<option value="">Select a detected drive…</option>'+LOCAL_VOLUMES.map((v,i)=>{
      const label=(v.label?`${v.label} · `:"")+v.path+` · ${v.type}`;
      return `<option value="${i}">${esc(label)}</option>`;
    }).join("")+ '<option value="manual">Manual path…</option>';
    const first=LOCAL_VOLUMES.findIndex(v=>v.removable);
    if(first>=0){sel.value=String(first);localVolumePicked()}
  }catch(e){sel.innerHTML='<option value="">Drive detection unavailable</option>'}
}
function localVolumePicked(){
  const value=$("#localVolume").value;
  if(value==="manual"){$("#localIndexSource").focus();return}
  const v=LOCAL_VOLUMES[+value];
  if(v)$("#localIndexSource").value=v.path;
}
async function localIndexToggle(){
  try{
    const state=await api("/api/fs/index/status");
    if(state.running){await api("/api/fs/index/stop",{method:"POST"});return}
    const source=$("#localIndexSource").value.trim();
    const root=$("#localIndexRoot").value.trim()||"/USB0";
    if(!source){toast("Choose a local USB drive or folder","err");return}
    const selected=LOCAL_VOLUMES[+$("#localVolume").value];
    if(selected&&!selected.removable&&!confirm(
      `${selected.path} is reported as a ${selected.type} drive, not removable storage.\n\n`+
      `Index it as ${root}? u64deck only reads files, but make sure this is the USB content intended for the Ultimate.`))return;
    if(!confirm(`Build the SQLite index from:\n\n${source}\n\nas ${root}?\n\nThe scan is read-only and does not copy or change files.`))return;
    await api("/api/fs/index/local",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({source,root})});
    toast(`Indexing ${source} locally as ${root}…`,"ok");
    idxPollStart();
  }catch(e){toast(e.message,"err")}}

async function idxToggle(){
  try{
    const state=await api("/api/fs/index/status");
    if(state.running){await api("/api/fs/index/stop",{method:"POST"});return}
    let confirmRoot=false;
    if(FS.path==="/"){
      confirmRoot=confirm("Index every attached storage device?\n\nA scan from / can take a very long time. It is usually better to open USB0 or a specific collection folder first.");
      if(!confirmRoot)return;
    }
    const expectedVerify=indexLocalCover(FS.path,state);
    if(expectedVerify&&!confirm(
      `The local USB import already covers ${FS.path}.\n\n`+
      `A verification scan walks the entire subtree over the Ultimate's FTP service and may take a long time. Normal folder browsing already performs lightweight incremental refreshes.\n\nContinue?`))return;
    const started=await api("/api/fs/index",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({root:FS.path,confirm_root:confirmRoot})});
    toast(started.verification
      ?"verifying "+FS.path+" from the Ultimate — the local index remains available…"
      :"indexing "+FS.path+" in the background…","ok");
    idxPollStart();
  }catch(e){toast(e.message,"err")}}
async function idxPauseToggle(){
  const s=IDX.last||await api("/api/fs/index/status");
  if(!s.running)return;
  const paused=!s.manual_paused;
  try{
    await api("/api/fs/index/pause",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({paused})});
    toast(paused?"Index paused":"Index resumed","ok");
    idxPollTick();
  }catch(e){toast(e.message,"err")}}
function idxPollStart(){
  if(IDX.poll)return;
  IDX.poll=setInterval(idxPollTick,1000);idxPollTick();
}
async function idxPollTick(){
  try{
    const s=await api("/api/fs/index/status");IDX.last=s;
    const mainEl=$("#idxStatus"),localEl=$("#localIdxStatus");
    const mainBtn=$("#idxBtn"),localBtn=$("#localIdxBtn");
    const pauseButtons=[$("#idxPauseBtn"),$("#localIdxPauseBtn")].filter(Boolean);
    if(s.running){
      const local=s.mode==="local";
      if(mainEl)mainEl.style.display="block";
      if(localEl)localEl.style.display="block";
      pauseButtons.forEach(p=>{p.style.display="inline-block";p.textContent=s.manual_paused?"▶ Resume":"⏸ Pause"});
      const rates=local
        ? `${s.dirs_per_sec||0} folders/s · ${s.files_per_sec||0} files/s · ${s.images_per_sec||0} images/s`
        : `${s.dirs_per_sec||0} folders/s · ${s.images_per_sec||0} images/s`;
      const eta=s.eta_secs!=null?` · ~${fmtDuration(s.eta_secs)} remaining`:" · estimating…";
      const bytes=local&&s.bytes_read?` · ${fmtSize(s.bytes_read)} image data`:"";
      const errors=s.scan_errors?` · ⚠ ${s.scan_errors} images not parsed`:"";
      const directoryDetail=!local&&s.verification
        ?` (${s.dirs_unchanged||0} unchanged · ${s.dirs_new||0} new · ${s.dirs_changed||0} changed)`
        :"";
      const imageDetail=!local&&s.verification
        ?` (${s.images_cached||0} unchanged · ${s.images_new||0} new · ${s.images_changed||0} changed)`
        :(s.images_cached?` (+${s.images_cached} cached)`:"");
      const progress=`${s.dirs} ${s.verification&&!local?"folders checked":"folders"}${directoryDetail} · ${s.files||0} files · ${s.images} images read${imageDetail}`+
        bytes+errors+` · ${fmtDuration(s.elapsed)} · ${rates}${eta}`;
      const heading=local
        ? `💻 local index ${s.source} → ${s.root}`
        : (s.verification?`🔎 verifying ${s.root} from Ultimate`:`🗂 indexing ${s.root}`);
      const text=s.paused
        ? `⏸ indexing paused — ${heading} · ${s.pause_reason||"waiting"} · ${progress}`
        : `${heading} — ${progress} · ${s.current}`;
      if(mainEl)mainEl.textContent=text;
      if(localEl)localEl.textContent=text;
      if(mainBtn)mainBtn.textContent="⏹ Stop index";
      if(localBtn)localBtn.textContent="⏹ Stop index";
    }else{
      if(mainBtn)mainBtn.textContent=indexLocalCover(FS.path||"/",s)?"🔎 Verify Index":"🗂 Build Index";
      if(localBtn)localBtn.textContent="Build local index";
      pauseButtons.forEach(p=>p.style.display="none");
      const m=s.indexed_roots&&s.indexed_roots[s.root];
      let text;
      if(s.error)text="index error: "+s.error;
      else if(m)text=`🗂 indexed ${s.root}: ${m.dirs} folders · ${m.images} images · ${fmtDuration(m.secs)} · ${m.completed} ✓`;
      else text="index stopped — partial results remain available in SQLite";
      for(const el of [mainEl,localEl])if(el&&el.style.display!=="none")el.textContent=text;
      clearInterval(IDX.poll);IDX.poll=null;
      setTimeout(()=>{for(const el of [mainEl,localEl])if(el)el.style.display="none"},15000);
      if(typeof cacheStatsLoad==="function")cacheStatsLoad();
      idxButtonRefresh();
    }
  }catch(e){}
}
function fsHitRow(h){
  const dir=h.path.slice(0,h.path.lastIndexOf("/"))||"/";
  if(h.kind==="dir")
    return `<div class="hitrow" onclick="fsGo('${jsq(h.path)}')">📁 <b>${esc(h.name)}</b>
      <span class="hint" style="flex:1">${esc(h.path)}</span>
      <button class="mini" onclick="fsGo('${jsq(h.path)}')">Open Folder</button></div>`;
  if(h.kind==="in-image"){
    const fav=itemSpec("disk_entry",h.name,`${h.path} · ${h.file_type}`,h.file_type==="PRG"?"disk_entry_dma":"disk_entry_open",{path:h.path,index:h.index,name:h.name,file_type:h.file_type});
    return `<div class="hitrow" style="cursor:default">💾 <b>${esc(h.name)}</b>
      <span class="badge">${esc(h.file_type)}</span>
      <span class="hint" style="flex:1">inside ${esc(h.path)}</span>${starButton(fav)}
      ${h.file_type==="PRG"?`<button class="mini" onclick="searchRunInImage('${jsq(h.path)}',${h.index})" title="DMA run this file">Run</button>`:""}
      <button class="mini" onclick="imgOpenDevice('${jsq(h.path)}')">Open Image</button></div>`;}
  const runnable=isRunnable(h.name),image=isImage(h.name);
  return `<div class="hitrow" style="cursor:default">${image?"🖴":"·"} <b>${esc(h.name)}</b>
    <span class="hint" style="flex:1">${esc(h.path)}</span>
    ${image?`<button class="mini primary tip mount-mode-action" data-mount-base="Mount & Run" data-tip="Mount with the selected safety mode, reset the C64, then load and run the first program." onclick="mountRunDevice('${jsq(h.path)}')">Mount & Run · ${mountModeShort()}</button>
      <button class="mini" onclick="imgOpenDevice('${jsq(h.path)}')">Open Image</button>`:""}
    ${(!image&&runnable)?`<button class="mini" onclick="runDevice('${jsq(h.path)}')">Run</button>`:""}
    <button class="mini" onclick="fsGo('${jsq(dir)}')">Open Folder</button></div>`;
}
async function fsSearch(){
  if(FSRCH.running){                       // Go doubles as Stop mid-search
    if(FSRCH.abort)FSRCH.abort.abort();
    return;
  }
  const q=$("#fsQuery").value.trim();
  if(q.length<2){toast("Type at least 2 characters","err");return}
  FSRCH.running=true;FSRCH.abort=new AbortController();
  $("#fsGo").textContent="⏹ Stop";
  $("#fsSearchOut").style.display="block";
  $("#fsSearchList").innerHTML="";
  let hits=0;
  const title=(p,extra)=>{$("#fsSearchTitle").textContent=
    `"${q}" — ${hits} hit${hits===1?"":"s"} · ${p.dirs||0} folders`+
    (p.dirs_cached?` (${p.dirs_cached} indexed)`:"")+` · ${p.images||0} images`+
    (p.images_cached?` (+${p.images_cached} cached)`:"")+` · ${p.elapsed||0}s`+
    (p.scanning?` · ${p.scanning}`:"")+(extra||"")};
  title({});
  try{
    const resp=await fetch("/api/fs/search/stream",{method:"POST",
      headers:{"Content-Type":"application/json"},signal:FSRCH.abort.signal,
      body:JSON.stringify({root:FS.path,query:q,inside_images:$("#fsInside").checked,
        budget_secs:+($("#fsBudget").value||60)})});
    if(!resp.ok)throw new Error((await resp.json()).detail||resp.statusText);
    const reader=resp.body.getReader(),dec=new TextDecoder();
    let buf="";
    for(;;){
      const {value,done}=await reader.read();
      if(done)break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split("\n");buf=lines.pop();
      for(const line of lines){
        if(!line.trim())continue;
        const ev=JSON.parse(line);
        if(ev.type==="hit"){hits++;
          $("#fsSearchList").insertAdjacentHTML("beforeend",fsHitRow(ev))}
        else if(ev.type==="progress")title(ev);
        else if(ev.type==="done"){
          title(ev,(ev.sqlite?" · SQLite":"")+(ev.truncated?` · ⚠ ${ev.truncated}`:" · done"));
          if(!hits)$("#fsSearchList").innerHTML='<span class="hint">no matches</span>'}
      }
    }
  }catch(e){
    if(e.name==="AbortError")
      $("#fsSearchTitle").textContent+=" · stopped by you";
    else $("#fsSearchList").innerHTML=`<span style="color:var(--err)">${esc(e.message)}</span>`}
  finally{FSRCH.running=false;$("#fsGo").textContent="Search"}}
async function searchRunInImage(path,index){
  try{
    toast("Opening image + running…","ok");
    await imgOpenDevice(path);       // loads the image, sets INSP.token
    await inspRun(index);            // DMA-run that entry
    tab("screen");screenEl.focus();
  }catch(e){toast(e.message,"err")}}
(function(){const b=localStorage.getItem("u64deck.search.budget");
  if(b)setTimeout(()=>{const el=$("#fsBudget");if(el)el.value=b},0)})();
function newDiskToggle(){
  if(FS.path==="/"){
    toast("Open USB0, SD, Flash, Temp or another storage folder first — / is only the Ultimate's virtual device list.","err");
    return;
  }
  const f=$("#newDiskForm");
  f.style.display=f.style.display==="none"?"block":"none";
  if(f.style.display==="block")$("#ndName").focus();
}
function newDiskKind(){
  const k=$("#ndKind").value,t=$("#ndTracks");
  if(k==="d64"){t.style.display="";t.innerHTML="<option>35</option><option>40</option>"}
  else if(k==="dnp"){t.style.display="";t.innerHTML=Array.from({length:8},(_,i)=>`<option>${(i+1)*32}</option>`).join("")}
  else t.style.display="none";
}
async function newDiskCreate(){
  if(FS.path==="/"){
    toast("Open a storage device or folder before creating a disk image.","err");
    return;
  }
  const name=$("#ndName").value.trim();
  if(!name){toast("Give the disk a file name","err");return}
  if(!allowReplaceDrive("a"))return;
  const kind=$("#ndKind").value;
  const body={kind,folder:FS.path,name,diskname:$("#ndLabel").value.trim()||undefined};
  if($("#ndTracks").style.display!=="none")body.tracks=+$("#ndTracks").value;
  const btn=$("#newDiskCreateBtn"),oldText=btn.textContent;
  btn.disabled=true;btn.textContent=IDX.poll?"Pausing index…":"Creating…";
  try{
    const r=await api("/api/fs/create_disk",{method:"POST",timeoutMs:40000,
      headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const createdPath=r.path||joinPath(FS.path,name.toLowerCase().endsWith("."+kind)?name:name+"."+kind);
    btn.textContent="Mounting RW…";
    try{
      await put(`/api/mount/device?drive=a&mode=readwrite&image=${encodeURIComponent(createdPath)}`,{timeoutMs:20000});
      toast(`Created ${createdPath} · mounted read/write on drive A ✓`,"ok");
      refreshDrives();swapRefresh();
    }catch(mountError){
      toast(`Created ${createdPath}, but it could not be mounted read/write: ${mountError.message}`,"err");
    }
    $("#ndName").value="";fsGo(FS.path);
  }catch(e){toast(e.message,"err")}
  finally{btn.disabled=false;btn.textContent=oldText}}
function allowReplaceDrive(drive){
  const state=DRIVE_STATE[drive]||{};
  return state.mode!=="unlinked"||confirm(`Drive ${drive.toUpperCase()} currently has an UNLINKED image. Replacing it discards temporary writes. Continue?`);
}
async function duplicateImage(path){
  if(!confirm(`Create a timestamped copy of ${path.split("/").pop()} on Ultimate storage?`))return;
  toast("Copying image…","ok");
  try{const r=await api("/api/fs/duplicate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({path})});
    toast(`Created ${r.destination} ✓`,"ok");fsGo(FS.path)}catch(e){toast(e.message,"err")}
}
async function backupMountRW(path,drive="a"){
  if(!allowReplaceDrive(drive))return;
  if(!confirm(`Create a timestamped backup, then mount the original READ/WRITE on drive ${drive.toUpperCase()}?`))return;
  toast("Backing up image before read/write mount…","ok");
  try{const r=await api("/api/mount/backup_then_rw",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({path,drive})});
    toast(`Backup ${r.backup.destination.split("/").pop()} created · mounted RW ✓`,"ok");
    showSwapDecision(r.swap_decision);refreshDrives()}catch(e){toast(e.message,"err")}
}
async function mountRunDevice(path){
  if(!allowReplaceDrive("a"))return;
  const mode=mountMode();
  toast("Mounting + booting…","ok");
  beginMountRunStatusWatch();
  try{const r=await put(`/api/mount/run/device?drive=a&mode=${mode}&image=${encodeURIComponent(path)}`,{timeoutMs:MOUNT_RUN_REQUEST_TIMEOUT_MS});
    toast((r.typed||"Booted")+" ✓","ok");
    showSwapDecision(r.swap_decision);
    rememberRecent(itemSpec("disk",path.split("/").pop()||path,path,"disk_run",{path}));
    refreshDrives();tab("screen");screenEl.focus()}
  catch(e){toast(e.message,"err")}
  finally{finishMountRunStatusWatch()}}
async function localMountRun(){
  if(!allowReplaceDrive("a"))return;
  const f=$("#localfile").files[0];if(!f){toast("Choose a file first","err");return}
  if(!isImage(f.name)){toast("Mount & Run is for disk images (.d64/.d71/.d81)","err");return}
  const fd=new FormData();fd.append("file",f);fd.append("drive","a");
  fd.append("mode",$("#localmode").value);
  toast("Uploading + booting…","ok");
  beginMountRunStatusWatch();
  try{const r=await api("/api/mount/run/upload",{method:"POST",body:fd,timeoutMs:MOUNT_RUN_REQUEST_TIMEOUT_MS});
    toast((r.typed||"Booted")+" ✓","ok");refreshDrives();tab("screen");screenEl.focus()}
  catch(e){toast(e.message,"err")}
  finally{finishMountRunStatusWatch()}}
async function mountDevice(path,drive){
  if(!allowReplaceDrive(drive))return;
  const mode=mountMode();
  try{const r=await put(`/api/mount/device?drive=${drive}&mode=${mode}&image=${encodeURIComponent(path)}`);
    toast(`Mounted on ${drive.toUpperCase()} ✓`,"ok");
    showSwapDecision(r.swap_decision);
    rememberRecent(itemSpec("disk",path.split("/").pop()||path,path,"disk_run",{path}));
    refreshDrives()}catch(e){toast(e.message,"err")}}
async function runDevice(path){
  if(String(path||"").toLowerCase().endsWith(".crt"))clearStandaloneScreenNotice();
  try{await put("/api/run/device?path="+encodeURIComponent(path));toast("Running ✓","ok");
    rememberRecent(itemSpec("program",path.split("/").pop()||path,path,"program_run",{path}))}
  catch(e){toast(e.message,"err")}}
function driveImageName(state){
  const raw=state.image_file||state.path||state.name||"";
  return raw?String(raw).split("/").pop():"";
}
function driveStatusLine(k,state){
  const name=driveImageName(state),mode=state.mode||state.reported_mode||"";
  const type=state.enabled?(state.type?`${state.type}${state.bus_id!=null?" #"+state.bus_id:""}`:"Mounted"):"Off";
  const badge=mode?` <span class="mount-badge ${esc(mode)}">${mountModeShort(mode)}</span> <span class="hint">${esc(mountModeLong(mode))}</span>`:"";
  return `<b>Drive ${k.toUpperCase()}</b> · ${esc(type)}${badge}${name?" · "+esc(name):""}`;
}
function renderDrivePanel(note=""){
  const lines=["a","b"].map(k=>driveStatusLine(k,DRIVE_STATE[k]||{}));
  if(note)lines.push(`<span class="drive-busy-note">${esc(note)}</span>`);
  $("#drivestat").innerHTML=lines.join("<br>");
  renderDriveSummaries();
}
function applyBusyMountSnapshot(mounts){
  if(!mounts||typeof mounts!=="object")return;
  let changed=false;
  for(const k of ["a","b"]){
    const mount=mounts[k];
    if(!mount||typeof mount!=="object"||!Object.keys(mount).length)continue;
    const previous=DRIVE_STATE[k]||{};
    DRIVE_STATE[k]={...previous,...mount,enabled:true,
      image_file:mount.path||mount.name||previous.image_file||"",provisional:true};
    changed=true;
  }
  if(changed)renderDrivePanel("Loading program — device status will refresh when complete.");
}
function renderDriveSummaries(){
  const parts=["a","b"].map(k=>{
    const state=DRIVE_STATE[k]||{},name=driveImageName(state);
    const label=name||(!state.enabled?"Off":"Empty");
    const mode=state.mode||state.reported_mode||"";
    const badge=name&&mode?` <span class="mount-badge ${esc(mode)}">${mountModeShort(mode)}</span>`:"";
    return `<span class="drive-summary-item" title="Drive ${k.toUpperCase()}: ${esc(state.image_file||state.path||label)}"><b>Drive ${k.toUpperCase()}</b>: ${esc(label)}${badge}</span>`;
  }).join("");
  document.querySelectorAll(".drive-summary-content").forEach(el=>el.innerHTML=parts);
}
function jumpToMountedDrives(){
  tab("disks");
  requestAnimationFrame(()=>$("#mountedDrivesPanel")?.scrollIntoView({behavior:"smooth",block:"start"}));
}
async function refreshDrives(){
  if(DISCOVERY_SCAN_ACTIVE||DISCOVERY_DIALOG_OPEN||uiInteractive()||DRIVES_IN_FLIGHT)return;
  if(!DRIVE_STATUS_READY){
    renderDrivePanel("Waiting for the Ultimate connection before reading drive status…");
    if(!INFO_IN_FLIGHT)loadInfo();
    return;
  }
  DRIVES_IN_FLIGHT=true;
  try{const r=await api("/api/drives");
    if(r?.u64deck_discovery_busy||r?.u64deck_operation_busy){
      const retry=Math.max(500,Number(r.u64deck_retry_ms)||1000);
      if(DRIVE_STATUS_RETRY_TIMER)clearTimeout(DRIVE_STATUS_RETRY_TIMER);
      DRIVE_STATUS_RETRY_TIMER=setTimeout(()=>{DRIVE_STATUS_RETRY_TIMER=null;refreshDrives()},retry);
      return;
    }
    if(DRIVE_STATUS_RETRY_TIMER){clearTimeout(DRIVE_STATUS_RETRY_TIMER);DRIVE_STATUS_RETRY_TIMER=null}
    if(r?.u64deck_busy){
      MOUNT_RUN_BUSY=true;applyBusyMountSnapshot(r.u64deck_mounts);
      return;
    }
    if(r?.u64deck_drives_unavailable){
      MOUNT_RUN_BUSY=false;applyBusyMountSnapshot(r.u64deck_mounts);
      renderDrivePanel(r.u64deck_drives_message||"Drive status temporarily unavailable — retrying…");
      const retry=Number(r.u64deck_retry_ms)||0;
      if(!r.u64deck_drives_error&&retry>0){
        DRIVE_STATUS_RETRY_TIMER=setTimeout(()=>{DRIVE_STATUS_RETRY_TIMER=null;refreshDrives()},Math.max(500,retry));
      }
      return;
    }
    DRIVE_STATUS_READY=true;
    MOUNT_RUN_BUSY=false;DRIVE_STATE={a:{enabled:false},b:{enabled:false}};
    for(const d of (r.drives||[])){for(const k of ["a","b"]){if(d[k]){
      const v=d[k],m=v.u64deck_mount||{},mode=m.mode||v.mode||v.mount_mode||"";
      DRIVE_STATE[k]={...m,enabled:!!v.enabled,type:v.type||"",bus_id:v.bus_id,
        image_file:v.image_file||v.image_path||"",reported_mode:v.mode||v.mount_mode||"",mode};
      }}}
    renderDrivePanel();
    await swapRefresh();
    if(r.swap_reconstructed)showSwapDecision(r.swap_decision,true);
  }catch(e){
    if(MOUNT_RUN_BUSY){
      renderDrivePanel("Loading program — device status will refresh when complete.");
      return;
    }
    $("#drivestat").textContent=e.message;
    document.querySelectorAll(".drive-summary-content").forEach(el=>el.textContent="Drive status unavailable: "+e.message);
  }finally{DRIVES_IN_FLIGHT=false}
}
async function driveAct(d,a){
  if(a==="remove"&&DRIVE_STATE[d]?.mode==="unlinked"&&!confirm(`Drive ${d.toUpperCase()} is mounted UNLINKED. Removing it discards temporary writes. Continue?`))return;
  try{await put(`/api/drives/${d}/${a}`);refreshDrives()}catch(e){toast(e.message,"err")}
}

/* ---------- image inspector ---------- */
let INSP=null;   // {token, image_name, device_path?}
async function imgOpenDevice(path){
  try{const r=await api("/api/image/open/device?path="+encodeURIComponent(path));
    r.device_path=path;showInspector(r);return r}catch(e){toast(e.message,"err");return null}}
async function localOpen(){
  const f=$("#localfile").files[0];if(!f){toast("Choose a file first","err");return}
  if(!isImage(f.name)){toast("That's not a disk image — use Run PRG/CRT","err");return}
  const fd=new FormData();fd.append("file",f);
  try{const r=await api("/api/image/open/upload",{method:"POST",body:fd});showInspector(r)}
  catch(e){toast(e.message,"err")}}
async function localMount(drive){
  if(!allowReplaceDrive(drive))return;
  const f=$("#localfile").files[0];if(!f){toast("Choose a file first","err");return}
  const fd=new FormData();fd.append("file",f);fd.append("drive",drive);
  fd.append("mode",$("#localmode").value);
  try{await api("/api/mount/upload",{method:"POST",body:fd});
    toast(`Mounted on ${drive.toUpperCase()} ✓`,"ok");refreshDrives();swapRefresh()}catch(e){toast(e.message,"err")}}
async function localRun(){
  const f=$("#localfile").files[0];if(!f){toast("Choose a file first","err");return}
  if(String(f.name||"").toLowerCase().endsWith(".crt"))clearStandaloneScreenNotice();
  const fd=new FormData();fd.append("file",f);
  try{await api("/api/run/upload",{method:"POST",body:fd});toast("Running ✓","ok")}
  catch(e){toast(e.message,"err")}}
function diskEntryItem(f,action){
  if(!INSP?.device_path)return null;
  const parent=INSP.device_path.split("/").pop()||INSP.image_name||"disk image";
  return itemSpec("disk_entry",f.name,`${parent} · ${INSP.device_path}`,action,{
    path:INSP.device_path,index:f.index,name:f.name,file_type:f.type});
}
function showInspector(r){
  INSP=r;
  $("#inspEmpty").style.display="none";$("#inspector").style.display="block";
  $("#inspTitle").textContent=`"${r.disk_name||"?"}" ${r.disk_id||""} · ${r.format.toUpperCase()} · ${r.image_name||"?"}`;
  $("#inspMeta").textContent=`${r.files.length} files · ${r.tracks} tracks · ${fmtSize(r.size)}`;
  $("#inspFiles").innerHTML=r.files.map(f=>{
    const runnable=f.type==="PRG"&&f.closed;
    const fav=diskEntryItem(f,runnable?"disk_entry_run":"disk_entry_open");
    return `<tr data-index="${f.index}">
      <td>${esc(f.name)||"<i>(no name)</i>"}</td>
      <td class="filetype ${f.type}">${f.type}${f.locked?"&lt;":""}${f.closed?"":"*"}</td>
      <td>${f.blocks}</td><td class="hint">${f.load_address||""}</td>
      <td style="text-align:right">
        ${fav?starButton(fav,"favourite this file inside the disk image"):""}
        ${runnable?`<button class="mini primary" onclick="inspRun(${f.index})">Run Instantly</button>
        <button class="mini" onclick="inspLoad(${f.index})">Load to BASIC</button>
        <button class="mini tip mount-mode-action" data-mount-base="Mount & Load" data-tip="Mount the parent image, reset, then load this exact PRG so multi-load software keeps its disk." onclick="inspMountLoad(${f.index})">Mount & Load · ${mountModeShort()}</button>`:""}
        <a class="mini" style="color:var(--dim)" href="/api/image/${r.token}/file?index=${f.index}">⤓</a>
      </td></tr>`}).join("");
  tab("disks");return r
}
async function inspRun(i){try{await api(`/api/image/${INSP.token}/run?index=${i}&mode=dma`,{method:"POST"});
  const f=(INSP.files||[]).find(x=>x.index===i);if(f){const it=diskEntryItem(f,"disk_entry_dma");if(it)rememberRecent(it)}
  toast("PRG running ✓","ok")}catch(e){toast(e.message,"err")}}
async function inspLoad(i){try{await api(`/api/image/${INSP.token}/run?index=${i}&mode=load`,{method:"POST"});
  const f=(INSP.files||[]).find(x=>x.index===i);if(f){const it=diskEntryItem(f,"disk_entry_dma");if(it)rememberRecent(it)}
  toast("PRG loaded (not run) ✓","ok")}catch(e){toast(e.message,"err")}}
async function inspMountLoad(i){
  const q=`index=${i}&drive=${$("#inspDrive").value}&mode=${$("#inspMode").value}`+
    (INSP.device_path?`&device_path=${encodeURIComponent(INSP.device_path)}`:"");
  toast("Mounting + typing LOAD… (takes a few seconds)");
  try{const r=await api(`/api/image/${INSP.token}/mount_load?${q}`,{method:"POST"});
    const f=(INSP.files||[]).find(x=>x.index===i);if(f){const it=diskEntryItem(f,"disk_entry_run");if(it)rememberRecent(it)}
    toast(r.typed+" ✓","ok");showSwapDecision(r.swap_decision);refreshDrives()}catch(e){toast(e.message,"err")}}
async function inspMountWhole(){
  if(INSP.device_path)return mountDevice(INSP.device_path,$("#inspDrive").value);
  toast("Re-select the file under Local files and use Mount → A/B","err")}

/* ---------- local launcher settings ---------- */
async function localSettingsLoad(){
  const sel=$("#localBrowserStartup"),status=$("#localBrowserStatus");if(!sel||!status)return;
  try{const r=await api("/api/local_settings");sel.value=r.browser_startup||"edge_app";
    status.textContent=r.edge_available
      ?`Edge detected · dedicated profile: ${r.edge_profile}`
      :"Edge was not detected; Edge app mode will fall back to the system browser.";
  }catch(e){status.textContent=e.message}
}
async function localSettingsSave(){
  const sel=$("#localBrowserStartup"),status=$("#localBrowserStatus");if(!sel)return;
  try{const r=await api("/api/local_settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({browser_startup:sel.value})});
    if(status)status.textContent=r.edge_available?"Saved · applies the next time start.bat is used":"Saved · Edge unavailable, so fallback will be used";
    toast("Local browser startup saved ✓","ok");
  }catch(e){toast(e.message,"err")}
}

/* ---------- settings ---------- */
const SET={loaded:false,loading:false,localInitialised:false,cat:null,detail:{},dirty:{},retryTimer:null,retryCount:0,maxRetries:40};
const SETW_KEY="u64deck.setitems.w";
function setitemsResizeInit(){
  const el=$("#setitems");if(!el)return;
  const w=localStorage.getItem(SETW_KEY);
  if(w)el.style.width=w+"px";
  if(typeof ResizeObserver!=="undefined"){
    let t=null;
    new ResizeObserver(()=>{clearTimeout(t);
      t=setTimeout(()=>localStorage.setItem(SETW_KEY,el.offsetWidth),300)}).observe(el);
  }
}
async function cacheStatsLoad(){
  try{
    const s=await api("/api/cache/stats");
    const db=s.database||{};
    const kb=Math.round((db.disk_bytes||0)/1024);
    $("#cacheStats").innerHTML=
      `Active index: <b>${esc(db.path||".u64deck-index.sqlite3")}</b>`+
      ((db.migration?.migrated_sources||0)>0?` · migrated and merged <b>${db.migration.migrated_sources}</b> legacy per-IP database${db.migration.migrated_sources===1?"":"s"}`:"")+
      (db.migration?.status==="failed"?` · <span style="color:var(--err)">migration failed: ${esc(db.migration.error||"unknown error")}</span>`:"")+`.<br>`+
      `SQLite storage index: <b>${db.directories||0}</b> folders / `+
      `${db.file_entries||0} files · <b>${db.images||0}</b> disk images / `+
      `${db.image_entries||0} internal files (${kb} KB on disk)`+
      (db.parse_failures?` · <b>${db.parse_failures}</b> images not parsed`:"")+`. `+
      `Updates are incremental; completed subtrees search directly in SQLite.<br>`+
      `SID metadata: <b>${db.sid_metadata||0}</b> tunes`+
      ((db.sid_index_runs||[]).length?` · last ${esc(db.sid_index_runs[0].mode)} scan ${esc(db.sid_index_runs[0].completed)}`:"")+`.<br>`+
      `Approved disk grouping: <b>${db.disk_group_rules||0}</b> reusable rules · <b>${db.disk_group_overrides||0}</b> exact sets.<br>`+
      `Songlengths: <b>${s.songlengths.entries}</b> tunes`+
      (s.songlengths.state==="loading"?" <span class='badge warn'>still loading…</span>":"")+
      (s.songlengths.state&&s.songlengths.state.startsWith("error")?` <span style="color:var(--err)">${esc(s.songlengths.state)}</span>`:"")+
      (s.songlengths.cached_on_disk?" (cached locally)":"")+
      ` · HVSC search index: <b>${s.hvsc_index.paths}</b> paths`+
      ` <span class="hint">(built from Songlengths — powers instant SID Jukebox search, no FTP per query)</span>.`+
      ((db.local_imports||[]).length?`<br>Last local USB import: <b>${esc(db.local_imports[0].source_path)}</b> → `+
        `<b>${esc(db.local_imports[0].root)}</b> · ${db.local_imports[0].dirs} folders · `+
        `${db.local_imports[0].files} files · ${db.local_imports[0].images} images · `+
        `${esc(db.local_imports[0].completed)}${db.local_imports[0].errors?` · ⚠ ${db.local_imports[0].errors} scan errors`:""}`:"");
    if(!LOCAL_VOLUMES.length)localVolumesLoad();
    if(s.songlengths.state==="loading")setTimeout(cacheStatsLoad,3000);
  }catch(e){$("#cacheStats").textContent=e.message}}
async function cacheParseErrorsToggle(){
  const panel=$("#parseErrorPanel"),list=$("#parseErrorList");
  if(panel.style.display!=="none"){panel.style.display="none";return}
  panel.style.display="block";list.innerHTML='<span class="hint">loading…</span>';
  try{
    const r=await api("/api/cache/parse_errors?limit=250");
    if(!r.errors.length){list.innerHTML='<span class="hint">No unparsed disk images are currently recorded.</span>';return}
    list.innerHTML=`<b>${r.errors.length} images not parsed</b><div style="max-height:360px;overflow:auto;margin-top:6px">`+
      r.errors.map(e=>`<div class="item-card"><div class="item-icon">⚠</div><div class="item-main">
        <div class="item-label">${esc(e.path)}</div><div class="item-detail hint">${esc(e.error)}</div></div>
        <button class="mini" onclick="tab('disks');fsGo('${jsq(parentPath(e.path))}')">folder</button></div>`).join("")+`</div>`;
  }catch(e){list.innerHTML=`<span style="color:var(--err)">${esc(e.message)}</span>`}
}

function diskNamingAmbiguousShown(){
  const out=[];
  for(const bucket of DISKNAMING.report?.ambiguous||[]){
    for(const item of bucket.examples||[])out.push({...item,pattern:bucket.pattern});
  }
  return out
}
function diskNamingExamples(items,section,allowPatternScope){
  if(!items?.length)return '<span class="hint">No examples.</span>';
  return items.map(item=>{
    const ambiguous=section==="ambiguous";
    const selector=ambiguous?`<label class="hint" style="display:flex;align-items:center;gap:4px;white-space:nowrap"><input type="checkbox" class="disk-naming-ambiguous-check" data-set-id="${esc(item.set_id||"")}"> Select</label>`:"";
    const folder=ambiguous?`<button class="mini" onclick="diskNamingApproveAmbiguousFolder(this.dataset.parent)" data-parent="${esc(item.parent||"/")}">Approve folder</button>`:"";
    return `<div class="item-card" style="align-items:flex-start">
      ${selector}<div class="item-main"><div class="item-label">${esc(item.parent||"/")}</div>
      <div class="item-detail hint">${(item.names||[]).map(esc).join(" · ")}${Number(item.total_files||0)>(item.names||[]).length?` · … ${Number(item.total_files)} files total`:""}</div></div>
      <span style="display:flex;gap:4px;flex-wrap:wrap">${allowPatternScope?`<button class="mini" onclick="diskNamingApproveRuleForFolder(this.dataset.i,this.dataset.e)" data-i="${item._item}" data-e="${item._example}">Approve for folder</button>`:""}${folder}<button class="mini" onclick="diskNamingApproveExactByIndex(this.dataset.s,this.dataset.i,this.dataset.e)" data-s="${section}" data-i="${item._item}" data-e="${item._example}">Approve this set only</button></span>
    </div>`
  }).join("");
}
function diskNamingSummarySection(title,items,kind){
  if(!items?.length)return `<details><summary>${esc(title)} · 0</summary><div class="hint" style="margin-top:6px">None found.</div></details>`;
  const totalSets=items.reduce((n,x)=>n+Number(x.sets||0),0);
  const batch=kind==="ambiguous"?`<div class="row" style="gap:6px;flex-wrap:wrap;margin:7px 0">
    <button class="mini primary" onclick="diskNamingApproveAmbiguousBatch('selected')">Approve selected examples</button>
    <button class="mini" onclick="diskNamingApproveAmbiguousBatch('all')">Approve all ambiguous (${totalSets.toLocaleString()})</button>
    <span class="hint">Selected applies to the examples displayed below. Approve all covers every current ambiguous set, not only the examples. Both create exact local sets only.</span>
  </div>`:"";
  return `<details ${kind==="candidate"?"open":""}><summary>${esc(title)} · ${totalSets.toLocaleString()} sets</summary>${batch}`+
    items.map((item,idx)=>{
      const examples=(item.examples||[]).map((x,e)=>({...x,_item:idx,_example:e}));
      const approve=kind==="candidate"?`<button class="mini primary" onclick="diskNamingApproveRule(${idx})">Approve pattern</button>`:"";
      const exampleHtml=kind==="recognised"
        ?(item.examples||[]).map(x=>`<div class="hint">${esc(x.parent||"/")}: ${(x.names||[]).map(esc).join(" · ")}</div>`).join("")
        :diskNamingExamples(examples,kind,kind==="candidate");
      return `<div class="panel" style="margin-top:7px;padding:8px"><div class="row" style="justify-content:space-between;align-items:baseline;gap:8px">
        <b>${esc(item.pattern)}</b><span class="hint">${Number(item.sets||0).toLocaleString()} sets · ${Number(item.files||0).toLocaleString()} files</span>${approve}</div>`+
        (item.reason?`<div class="hint" style="margin-top:4px">${esc(item.reason)}</div>`:"")+
        `<div style="margin-top:5px">${exampleHtml}</div></div>`;
    }).join("")+`</details>`;
}

function diskNamingRulesHtml(r){
  const rules=r.rules||[],overrides=r.overrides||[],total=rules.length+overrides.length,pageSize=50;
  const pages=Math.max(1,Math.ceil(overrides.length/pageSize));
  DISKNAMING.overridePage=Math.max(0,Math.min(DISKNAMING.overridePage,pages-1));
  const start=DISKNAMING.overridePage*pageSize,end=Math.min(overrides.length,start+pageSize),visible=overrides.slice(start,end);
  let html=`<details ${DISKNAMING.overrideOpen?'open':''} ontoggle="DISKNAMING.overrideOpen=this.open"><summary>Approved local rules and exact sets · ${total}</summary>`;
  html+='<div style="margin-top:6px">';
  if(!total)html+='<span class="hint">No local approvals have been saved.</span>';
  else html+=`<div class="row" style="gap:6px;flex-wrap:wrap;margin-bottom:7px"><button class="mini danger" onclick="diskNamingRemoveAllApprovals()">Remove all local approvals</button><span class="hint">${rules.length} reusable rules · ${overrides.length.toLocaleString()} exact sets</span></div>`;
  for(const rule of rules)html+=`<div class="item-card"><div class="item-main"><div class="item-label">${esc(rule.label||rule.pattern_key)}</div><div class="item-detail hint">${esc(rule.scope||"/")} · ${(rule.extensions||[]).join(", ")} · last approval match count ${Number(rule.last_match_count||0)}</div></div><button class="mini" onclick="diskNamingRuleToggle(${rule.id},${rule.enabled?'false':'true'})">${rule.enabled?'Disable':'Enable'}</button><button class="mini danger" onclick="diskNamingRuleRemove(${rule.id})">Remove</button></div>`;
  if(overrides.length){
    html+=`<div class="row" style="gap:6px;flex-wrap:wrap;margin:8px 0 5px"><button class="mini" onclick="diskNamingManageExactSelected('enable')">Enable selected exact sets</button><button class="mini" onclick="diskNamingManageExactSelected('disable')">Disable selected exact sets</button><button class="mini danger" onclick="diskNamingManageExactSelected('remove')">Remove selected exact sets</button><span class="hint">Selection applies to the current page.</span></div>`;
    if(pages>1)html+=`<div class="row" style="gap:6px;align-items:center;margin:5px 0"><button class="mini" onclick="diskNamingOverridePage(-1)" ${DISKNAMING.overridePage===0?'disabled':''}>← Previous</button><span class="hint">Page ${DISKNAMING.overridePage+1} of ${pages} · showing ${(start+1).toLocaleString()}–${end.toLocaleString()} of ${overrides.length.toLocaleString()}</span><button class="mini" onclick="diskNamingOverridePage(1)" ${DISKNAMING.overridePage>=pages-1?'disabled':''}>Next →</button></div>`;
  }
  for(const item of visible)html+=`<div class="item-card"><input type="checkbox" class="disk-naming-approved-exact-check" data-id="${item.id}"><div class="item-main"><div class="item-label">Exact set · ${esc(item.parent)}</div><div class="item-detail hint">${(item.names||[]).map(esc).join(" · ")}</div></div><button class="mini" onclick="diskNamingOverrideToggle(${item.id},${item.enabled?'false':'true'})">${item.enabled?'Disable':'Enable'}</button><button class="mini danger" onclick="diskNamingOverrideRemove(${item.id})">Remove</button></div>`;
  return html+'</div></details>';
}
function diskNamingOverridePage(delta){DISKNAMING.overrideOpen=true;DISKNAMING.overridePage+=Number(delta)||0;diskNamingRender(DISKNAMING.report)}
function diskNamingRender(r){
  DISKNAMING.report=r;const panel=$("#diskNamingPanel"),copy=$("#diskNamingCopyBtn");
  if(!panel)return;panel.style.display="block";if(copy)copy.disabled=!r?.report_text;
  const s=r.summary||{};
  panel.innerHTML=`<div class="hint"><b>${Number(s.indexed_disk_images||0).toLocaleString()}</b> indexed disk images in <b>${Number(s.directories||0).toLocaleString()}</b> folders · <b>${Number(s.recognised_sets||0).toLocaleString()}</b> recognised sets · <b>${Number(s.candidate_sets||0).toLocaleString()}</b> high-confidence unrecognised sets · <b>${Number(s.ambiguous_sets||0).toLocaleString()}</b> ambiguous · <b>${Number(s.rejected_sets||0).toLocaleString()}</b> rejected.</div>`+
    `<div style="display:grid;gap:7px;margin-top:8px">`+
    diskNamingSummarySection("High-confidence unrecognised patterns",r.candidates||[],"candidate")+
    diskNamingSummarySection("Recognised patterns",r.recognised||[],"recognised")+
    diskNamingSummarySection("Ambiguous candidates",r.ambiguous||[],"ambiguous")+
    diskNamingSummarySection("Rejected / protected patterns",r.rejected||[],"rejected")+
    diskNamingRulesHtml(r)+`</div>`;
}
async function diskNamingAnalyse(){
  if(DISKNAMING.busy)return;DISKNAMING.busy=true;const btn=$("#diskNamingAnalyseBtn"),panel=$("#diskNamingPanel");
  if(btn){btn.disabled=true;btn.textContent="Analysing index…"}if(panel){panel.style.display="block";panel.innerHTML='<span class="hint">Reading indexed disk-image filenames…</span>'}
  try{diskNamingRender(await api("/api/fs/index/disk-naming",{timeoutMs:60000}));toast("Disk-image naming analysis complete","ok")}
  catch(e){if(panel)panel.innerHTML=`<span style="color:var(--err)">${esc(e.message)}</span>`;toast(e.message,"err")}
  finally{DISKNAMING.busy=false;if(btn){btn.disabled=false;btn.textContent="Analyse Disk-Image Names"}}
}
async function diskNamingApproveRule(index){
  const item=DISKNAMING.report?.candidates?.[index];if(!item)return;
  const examples=(item.examples||[]).map(x=>`${x.parent}: ${(x.names||[]).join(" | ")}`).slice(0,4).join("\n");
  if(!confirm(`Approve this reusable disk-grouping pattern?\n\n${item.pattern}\n${item.sets} sets / ${item.files} files\nScope: all indexed folders\n\n${examples}\n\nThe constrained rule will persist across index rebuilds. No files will be renamed or modified.`))return;
  try{const r=await api("/api/fs/index/disk-naming/rules",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({pattern_key:item.pattern_key,scope:"/"}),timeoutMs:60000});diskNamingRender(r.analysis);toast("Disk-grouping pattern approved","ok")}
  catch(e){toast(e.message,"err")}
}
async function diskNamingApproveRuleForFolder(candidateIndex,exampleIndex){
  const candidate=DISKNAMING.report?.candidates?.[Number(candidateIndex)],item=candidate?.examples?.[Number(exampleIndex)];if(!candidate||!item)return;
  if(!confirm(`Approve this disk-grouping pattern only for this folder?\n\n${candidate.pattern}\nScope: ${item.parent}\n\n${(item.names||[]).join("\n")}\n\nThe constrained rule persists across index rebuilds but applies only inside this folder. No files are modified.`))return;
  try{const r=await api("/api/fs/index/disk-naming/rules",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({pattern_key:candidate.pattern_key,scope:item.parent}),timeoutMs:60000});diskNamingRender(r.analysis);toast("Folder-scoped disk-grouping pattern approved","ok")}
  catch(e){toast(e.message,"err")}
}
async function diskNamingApproveExactByIndex(section,itemIndex,exampleIndex){
  const map={candidate:"candidates",ambiguous:"ambiguous",rejected:"rejected"},key=map[section];
  const item=key?DISKNAMING.report?.[key]?.[Number(itemIndex)]?.examples?.[Number(exampleIndex)]:null;if(!item)return;
  const warning=section==="rejected"?"\n\nThis set was rejected by a safety rule. Exact approval overrides that protection only for these filenames.":section==="ambiguous"?"\n\nThis set is ambiguous. Confirm only if these files are genuinely swap media.":"";
  if(!confirm(`Approve this exact disk set only?\n\n${item.parent}\n${(item.names||[]).join("\n")}${warning}\n\nOnly these indexed filenames in this folder will be grouped. No reusable pattern is created and no files are modified.`))return;
  try{
    const path=section==="ambiguous"&&item.set_id?"/api/fs/index/disk-naming/overrides/batch":"/api/fs/index/disk-naming/overrides";
    const body=section==="ambiguous"&&item.set_id?{set_ids:[item.set_id]}:{parent:item.parent,names:item.names,label:`approved ${section} set from disk naming analysis`};
    const r=await api(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body),timeoutMs:60000});diskNamingRender(r.analysis);toast("Exact disk set approved","ok")
  }catch(e){toast(e.message,"err")}
}
function diskNamingAmbiguousSelection(){
  const shown=diskNamingAmbiguousShown(),byId=new Map(shown.map(item=>[item.set_id,item]));
  return [...document.querySelectorAll(".disk-naming-ambiguous-check:checked")].map(box=>byId.get(box.dataset.setId)).filter(Boolean)
}
async function diskNamingApproveAmbiguousBatch(mode){
  const all=mode==="all",items=all?diskNamingAmbiguousShown():diskNamingAmbiguousSelection();
  const summary=DISKNAMING.report?.summary||{},totalSets=all?Number(summary.ambiguous_sets||0):items.length;
  if(!totalSets){toast(all?"No ambiguous sets are available":"Select one or more ambiguous examples","err");return}
  const ids=all?[]:[...new Set(items.map(item=>item.set_id))];
  const folders=all?Number((DISKNAMING.report?.ambiguous_folders||[]).length):new Set(items.map(item=>(item.parent||"/").toLowerCase())).size;
  const files=all?(DISKNAMING.report?.ambiguous||[]).reduce((n,item)=>n+Number(item.files||0),0):items.reduce((n,item)=>n+Number(item.total_files||(item.names||[]).length),0);
  const examples=items.slice(0,5).map(item=>`${item.parent}: ${(item.names||[]).join(" | ")}`).join("\n");
  const scope=all?`every current ambiguous disk set, including sets not displayed as examples`:`the ${ids.length} selected examples`;
  if(!confirm(`Approve ${totalSets.toLocaleString()} ambiguous disk sets?\n\n${files.toLocaleString()} files in ${folders.toLocaleString()} folders\nScope: ${scope}\n\n${examples}${items.length>5||all?"\n…":""}\n\nThis creates exact local set approvals only. It does not create a general filename pattern, rename files or modify the storage index.`))return;
  if(all&&totalSets>250&&!confirm(`Final confirmation: approve all ${totalSets.toLocaleString()} current ambiguous disk sets as exact local sets?`))return;
  try{
    DISKNAMING.busy=true;
    const r=await api("/api/fs/index/disk-naming/overrides/batch",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(all?{all_ambiguous:true}:{set_ids:ids}),timeoutMs:120000});
    DISKNAMING.overridePage=0;diskNamingRender(r.analysis);toast(`${r.summary.sets.toLocaleString()} ambiguous disk sets approved`,"ok")
  }catch(e){toast(e.message,"err")}
  finally{DISKNAMING.busy=false}
}
async function diskNamingApproveAmbiguousFolder(parent){
  const stats=(DISKNAMING.report?.ambiguous_folders||[]).find(item=>(item.parent||"/").toLowerCase()===(parent||"/").toLowerCase());
  if(!stats){toast("That folder no longer has ambiguous sets in the current report","err");return}
  const examples=diskNamingAmbiguousShown().filter(item=>(item.parent||"/").toLowerCase()===(parent||"/").toLowerCase()).slice(0,4).map(item=>(item.names||[]).join(" | ")).join("\n");
  if(!confirm(`Approve every ambiguous disk set in this folder?\n\n${stats.parent}\n${stats.sets} sets / ${stats.files} files\n\n${examples}\n\nThis creates exact local set approvals only. It does not create a reusable pattern or modify files.`))return;
  try{const r=await api("/api/fs/index/disk-naming/overrides/batch",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({parent:stats.parent}),timeoutMs:60000});diskNamingRender(r.analysis);toast(`${r.summary.sets} folder sets approved`,"ok")}
  catch(e){toast(e.message,"err")}
}

async function diskNamingRuleToggle(id,enabled){try{const r=await api(`/api/fs/index/disk-naming/rules/${id}`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled})});diskNamingRender(r.analysis)}catch(e){toast(e.message,"err")}}
async function diskNamingRuleRemove(id){if(!confirm("Remove this approved disk-grouping pattern?\n\nFiles and index entries are not changed."))return;try{const r=await api(`/api/fs/index/disk-naming/rules/${id}`,{method:"DELETE"});diskNamingRender(r.analysis);toast("Disk-grouping pattern removed","ok")}catch(e){toast(e.message,"err")}}
async function diskNamingOverrideToggle(id,enabled){try{const r=await api(`/api/fs/index/disk-naming/overrides/${id}`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled})});diskNamingRender(r.analysis)}catch(e){toast(e.message,"err")}}
async function diskNamingOverrideRemove(id){if(!confirm("Remove this approved exact disk set?\n\nFiles and index entries are not changed."))return;try{const r=await api(`/api/fs/index/disk-naming/overrides/${id}`,{method:"DELETE"});diskNamingRender(r.analysis);toast("Exact disk set removed","ok")}catch(e){toast(e.message,"err")}}
async function diskNamingManageExactSelected(action){
  const ids=[...document.querySelectorAll(".disk-naming-approved-exact-check:checked")].map(box=>Number(box.dataset.id)).filter(Number.isInteger);
  if(!ids.length){toast("Select one or more approved exact sets","err");return}
  const verb={enable:"Enable",disable:"Disable",remove:"Remove"}[action];if(!verb)return;
  if(!confirm(`${verb} ${ids.length} selected exact-set approvals?\n\nFiles and index entries are not changed.`))return;
  try{const r=await api("/api/fs/index/disk-naming/overrides/manage",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action,ids}),timeoutMs:60000});diskNamingRender(r.analysis);toast(`${r.result.count} exact-set approvals ${action}d`,"ok")}
  catch(e){toast(e.message,"err")}
}
async function diskNamingRemoveAllApprovals(){
  const rules=DISKNAMING.report?.rules?.length||0,overrides=DISKNAMING.report?.overrides?.length||0,total=rules+overrides;if(!total)return;
  if(!confirm(`Remove all local disk-grouping approvals?\n\n${rules} reusable rules\n${overrides} exact sets\n\nFiles and index entries will not be changed. Built-in disk detection, including terminal -a/-b grouping, will continue to work.`))return;
  if(!confirm("Final confirmation: remove every local reusable rule and exact-set approval now?"))return;
  try{const r=await api("/api/fs/index/disk-naming/approvals",{method:"DELETE",timeoutMs:60000});diskNamingRender(r.analysis);toast(`${r.removed.total} local approvals removed`,"ok")}
  catch(e){toast(e.message,"err")}
}

async function diskNamingCopyReport(){
  const text=DISKNAMING.report?.report_text;if(!text)return;
  try{
    if(navigator.clipboard?.writeText)await navigator.clipboard.writeText(text);
    else{const box=document.createElement("textarea");box.value=text;box.style.position="fixed";box.style.opacity="0";document.body.append(box);box.select();if(!document.execCommand("copy"))throw new Error("browser copy command was rejected");box.remove()}
    toast("Disk naming analysis report copied","ok")
  }catch(e){toast("Could not copy report: "+e.message,"err")}
}

async function cacheClearImages(){
  try{const r=await api("/api/cache/clear_images",{method:"POST"});
    toast("Cleared "+r.cleared+" cached image dirs; completed roots will be refreshed","ok");cacheStatsLoad()}
  catch(e){toast(e.message,"err")}}
async function cacheClearIndex(){
  if(!confirm("Clear the entire local storage index?\n\nThis does not delete anything from the Ultimate. Approved disk-grouping rules and exact sets are preserved, but searches will use live FTP until you index again."))return;
  try{await api("/api/cache/clear_index",{method:"POST"});
    toast("Local storage index cleared","ok");cacheStatsLoad()}
  catch(e){toast(e.message,"err")}}
function settingsTransientError(message){
  return /10061|actively refused|connect(?:ion)?|client is closed|client has been closed|timed out|temporar|not ready|503|busy/i.test(String(message||""));
}
function settingsWaiting(message="Waiting for the Ultimate connection before reading firmware settings…"){
  $("#catlist").innerHTML=`<span class="hint">${esc(message)}</span>`;
  $("#setitems").innerHTML='<span class="hint">Settings will load automatically when the connection is ready.</span>';
}
function settingsRetry(delay=750){
  if(SET.retryTimer)clearTimeout(SET.retryTimer);
  SET.retryTimer=setTimeout(()=>{SET.retryTimer=null;loadCats()},Math.max(0,delay));
}
function settingsPersistentError(message){
  SET.loaded=false;
  $("#catlist").innerHTML=`<span style="color:var(--err)">${esc(message)}</span><br><button class="mini" onclick="settingsRetryNow()">Retry settings</button>`;
  $("#setitems").innerHTML='<span class="hint">No firmware settings were changed.</span>';
}
function settingsRetryNow(){SET.retryCount=0;SET.loaded=false;settingsWaiting("Retrying the Ultimate connection…");loadInfo();settingsRetry(250)}
async function loadCats(){
  if(!SET.localInitialised){
    SET.localInitialised=true;localSettingsLoad();cacheStatsLoad();setitemsResizeInit();
  }
  if(SET.loaded||SET.loading)return;
  if(!LAST_DEVICE_INFO){
    SET.retryCount++;settingsWaiting(SET.retryCount>1?"Ultimate connection not ready — retrying…":"Waiting for the Ultimate connection before reading firmware settings…");
    if(!INFO_IN_FLIGHT)loadInfo();
    if(SET.retryCount<=SET.maxRetries)settingsRetry();
    else settingsPersistentError("Ultimate settings are still unavailable after bounded retries. Check the connection, then retry.");
    return;
  }
  SET.loading=true;
  try{
    const r=await api("/api/configs");
    SET.loaded=true;SET.retryCount=0;
    if(SET.retryTimer){clearTimeout(SET.retryTimer);SET.retryTimer=null}
    $("#catlist").innerHTML=(r.categories||[]).map(c=>
      `<button onclick="loadCat('${jsq(c)}',this)">${esc(c)}</button>`).join("")||'<span class="hint">No firmware categories returned.</span>';
    $("#setitems").innerHTML='<span class="hint">Pick a category.</span>';
  }catch(e){
    SET.loaded=false;
    if(settingsTransientError(e.message)&&SET.retryCount<SET.maxRetries){
      SET.retryCount++;settingsWaiting("Ultimate settings are not ready — retrying automatically…");
      loadInfo();settingsRetry();
    }else settingsPersistentError(e.message);
  }finally{SET.loading=false}
}
async function loadCat(cat,btn,attempt=0){
  document.querySelectorAll("#catlist button").forEach(b=>b.classList.toggle("active",b===btn));
  SET.cat=cat;SET.dirty={};updApply();
  $("#setitems").innerHTML='<span class="hint">loading…</span>';
  try{
    const det=await api("/api/configs/"+encodeURIComponent(cat)+"?detail=true");
    const items=det[Object.keys(det).find(k=>k!=="errors")]||{};
    SET.detail=items;
    $("#setitems").innerHTML=Object.entries(items).map(([name,d])=>{
      const cur=(d&&typeof d==="object"&&"current" in d)?d.current:d;
      return `<div class="setrow" data-item="${esc(name)}">
        <label title="${esc(name)}">${esc(name)}</label>
        <span class="val">${editorFor(name,d,cur)}</span></div>`}).join("")
      ||'<span class="hint">no items</span>';
  }catch(e){
    if(settingsTransientError(e.message)&&attempt<3){
      $("#setitems").innerHTML='<span class="hint">Ultimate connection changed — retrying this category…</span>';
      loadInfo();setTimeout(()=>loadCat(cat,btn,attempt+1),750);
    }else $("#setitems").innerHTML=`<span style="color:var(--err)">${esc(e.message)}</span>`;
  }}
function editorFor(name,d,cur){
  const n=esc(name);
  const vals=d&&typeof d==="object"?(d.values||d.options||d.choices):null;
  if(Array.isArray(vals)&&vals.length)
    return `<select style="width:100%" onchange="markDirty('${n}',this.value,this)">`+
      vals.map(v=>`<option ${String(v)===String(cur)?"selected":""}>${esc(v)}</option>`).join("")+`</select>`;
  if(d&&typeof d==="object"&&"min" in d&&"max" in d)
    return `<input type="number" style="width:100%" min="${d.min}" max="${d.max}" value="${esc(cur)}"
      onchange="markDirty('${n}',this.value,this)">`;
  // string items: firmware may provide a presets list (STRFUNC) — offer them
  // as suggestions while still allowing free text
  const presets=d&&typeof d==="object"&&Array.isArray(d.presets)?d.presets:null;
  const listId=presets?`dl_${n.replace(/[^a-z0-9]/gi,"_")}`:"";
  const dl=presets?`<datalist id="${listId}">${presets.map(p=>`<option>${esc(p)}</option>`).join("")}</datalist>`:"";
  // mask likely secrets (API returns them cleartext); click 👁 to reveal
  const isPass=/passw|passphrase/i.test(name);
  const eye=isPass?`<button class="mini" tabindex="-1" onclick="const i=this.previousElementSibling;
    i.type=i.type==='password'?'text':'password'">👁</button>`:"";
  return `<span style="display:flex;gap:4px">
    <input style="flex:1" type="${isPass?"password":"text"}" ${listId?`list="${listId}"`:""}
      value="${esc(cur??"")}" onchange="markDirty('${n}',this.value,this)">${eye}</span>${dl}`}
function markDirty(name,value,el){SET.dirty[name]=value;
  el.closest(".setrow").classList.add("dirty");updApply()}
function updApply(){const n=Object.keys(SET.dirty).length;
  const b=$("#btnApply");b.disabled=!n;b.textContent=n?`Apply ${n} change${n>1?"s":""}`:"Apply changes"}
async function saveDirty(){
  const payload={};payload[SET.cat]={};
  for(const[k,v]of Object.entries(SET.dirty)){
    const d=SET.detail[k];
    // only numeric-coerce genuine VALUE items (min/max); strings stay strings
    // so things like leading-zero passwords or numeric hostnames survive
    const isNum=d&&typeof d==="object"&&"min" in d&&"max" in d;
    payload[SET.cat][k]=isNum&&v!==""&&!isNaN(v)?Number(v):v}
  try{await api("/api/configs",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(payload)});
    toast("Applied ✓ (use Save to Flash to persist)","ok");
    loadBootOptions();
    SET.dirty={};updApply();
    document.querySelectorAll(".setrow.dirty").forEach(e=>e.classList.remove("dirty"))}
  catch(e){toast(e.message,"err")}}
async function cfgAction(a){try{await put("/api/configs_action/"+a);loadBootOptions();toast(healthStatusLabel(a)+" ✓","ok")}
  catch(e){toast(e.message,"err")}}

/* ---------- assembly64 ---------- */
const ASM_CATEGORY_LABELS={
  0:"Games",1:"Demos",2:"C128",3:"Graphics",4:"Music",5:"Disc Mags",
  6:"BBS",7:"Misc",8:"Tools",9:"Charts",11:"Intros",18:"SID"
};
let ASM={results:[],current:null,selectedIndex:-1,files:[],formLoaded:false,presetLabels:{category:{}}};
Object.entries(ASM_CATEGORY_LABELS).forEach(([id,label])=>ASM.presetLabels.category[id]=label);
const ASM_TEXT_FIELDS=[
  {name:"name",label:"Release Name",placeholder:"e.g. Last Ninja"},
  {name:"group",label:"Group",placeholder:"e.g. Fairlight"},
  {name:"handle",label:"Handle",placeholder:"e.g. Rob Hubbard"},
  {name:"event",label:"Event",placeholder:"e.g. X 2025"},
];
function asmFieldLabel(value){
  return String(value||"").replace(/[_-]+/g," ").replace(/\b\w/g,c=>c.toUpperCase());
}
function asmRememberPresetLabel(field,value,label){
  const key=String(field||"").toLowerCase();
  if(!key)return;
  const labels=ASM.presetLabels[key]||(ASM.presetLabels[key]={});
  const add=v=>{if(v!==undefined&&v!==null&&String(v)!=="")labels[String(v).toLowerCase()]=String(label)};
  if(value&&typeof value==="object"){
    add(value.id);add(value.value);add(value.aqlKey);add(value.name);add(value.label);
  }else add(value);
}
function asmPresetLabel(field,value){
  if(value===undefined||value===null||String(value)==="")return "—";
  const labels=ASM.presetLabels[String(field||"").toLowerCase()]||{};
  return labels[String(value).toLowerCase()]||asmFieldLabel(value);
}
function asmRatingLabel(value){
  if(value===undefined||value===null||String(value)==="")return "—";
  return String(value);
}
function asmUpdatedLabel(value){
  if(value===undefined||value===null||String(value).trim()==="")return "—";
  const raw=String(value).trim();
  const m=raw.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  if(!m)return raw.replace(/\.\d+$/,'');
  const months=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const month=months[Number(m[2])-1];
  if(!month)return raw.replace(/\.\d+$/,'');
  return `${Number(m[3])} ${month} ${m[1]}, ${m[4]}:${m[5]}`;
}
function asmActionLabel(action){
  return ({mount_run:"Mount & Run",mount_a:"Mount to A",mount_b:"Mount to B",run:"Run",inspect:"Open Image"})[action]||asmFieldLabel(action);
}
function asmEmpty(icon,title,text){
  return `<div class="asm-empty-icon">${icon}</div><strong>${esc(title)}</strong><span>${esc(text)}</span>`;
}
async function asmFormInit(){
  if(ASM.formLoaded)return;ASM.formLoaded=true;
  const form=$("#asmForm");
  const textInputs=ASM_TEXT_FIELDS.map(f=>
    `<label class="asm-field"><span>${esc(f.label)}</span><input data-asmfield="${f.name}" data-dropdown="0"
      placeholder="${esc(f.placeholder)}" onkeydown="if(event.key==='Enter')asmSearch()"></label>`).join("");
  let presetSelects="";
  try{
    const p=await api("/api/asm64/presets");
    $("#asmraw").textContent="/* presets */\n"+JSON.stringify(p,null,2).slice(0,20000);
    let list=Array.isArray(p)?p:null;
    if(!list&&p&&typeof p==="object")
      for(const k of Object.keys(p))if(Array.isArray(p[k])){list=p[k];break}
    const tokOf=v=>typeof v==="object"&&v!==null?(v.aqlKey??v.value??v.id??v.name??""):String(v);
    const labOf=v=>typeof v==="object"&&v!==null?(v.name??v.aqlKey??v.value??v.id??""):String(v);
    if(list)
      presetSelects=list.filter(e=>e&&(e.type||e.name)&&Array.isArray(e.values)).map(e=>{
        const fname=String(e.type||e.name);
        const label=asmFieldLabel(e.name||e.type);
        const title=esc(e.description||label);
        const options=e.values.map(v=>{
          const token=tokOf(v),display=labOf(v);
          asmRememberPresetLabel(fname,v,display);
          asmRememberPresetLabel(fname,token,display);
          return `<option value="${esc(String(token))}">${esc(String(display))}</option>`;
        }).join("");
        return `<label class="asm-field" title="${title}"><span>${esc(label)}</span>
          <select data-asmfield="${esc(fname)}" data-dropdown="1">
            <option value="">Any</option>${options}
          </select></label>`}).join("");
    if(!presetSelects)
      presetSelects=`<span class="hint" style="color:var(--err)">Search filters could not be loaded; text search remains available.</span>`;
  }catch(e){
    $("#asmraw").textContent="/* presets error */\n"+e.message;
    presetSelects=`<span class="hint" style="color:var(--err)">Search filters unavailable: ${esc(e.message)}</span>`}
  form.innerHTML=textInputs+presetSelects+
    `<div class="asm-search-actions"><button onclick="asmClear()">Clear</button><button class="primary" onclick="asmSearch()">Search</button></div>`;
}
function asmCollectFields(){
  return [...document.querySelectorAll("#asmForm [data-asmfield]")].map(el=>({
    name:el.dataset.asmfield,value:el.value.trim(),dropdown:el.dataset.dropdown==="1"}))
    .filter(f=>f.value);
}
function asmClear(){
  document.querySelectorAll("#asmForm [data-asmfield]").forEach(el=>el.value="");
  ASM.results=[];ASM.current=null;ASM.selectedIndex=-1;ASM.files=[];
  $("#asmResTitle").textContent="SEARCH RESULTS";
  $("#asmResMeta").textContent="Enter one or more search terms above.";
  $("#asmtable").className="asm-scroll asm-empty-state";
  $("#asmtable").innerHTML=asmEmpty("A64","Search the Assembly64 catalogue","Results will appear here. Select a release to see its downloadable files.");
  asmResetFiles();
}
function asmResetFiles(){
  $("#asmFilesTitle").textContent="RELEASE FILES";
  $("#asmFilesMeta").textContent="No release selected.";
  $("#asmfilelist").className="asm-scroll asm-empty-state";
  $("#asmfilelist").innerHTML=asmEmpty("▤","Select a release","Files and available run, mount or inspect actions will appear here.");
}
async function asmSearch(){
  const fields=asmCollectFields();
  if(!fields.length){toast("Enter or select at least one search field","err");return}
  ASM.current=null;ASM.selectedIndex=-1;asmResetFiles();
  $("#asmResTitle").textContent="SEARCH RESULTS";
  $("#asmResMeta").textContent="Searching Assembly64…";
  $("#asmtable").className="asm-scroll asm-empty-state";
  $("#asmtable").innerHTML=asmEmpty("…","Searching","Querying the public Assembly64 catalogue.");
  $("#asmraw").textContent="";
  try{
    const r=await api("/api/asm64/search",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({fields})});
    $("#asmraw").textContent=JSON.stringify(r,null,2).slice(0,20000);
    const arr=Array.isArray(r.results)?r.results:[];
    ASM.results=arr;
    $("#asmResTitle").textContent=`SEARCH RESULTS · ${arr.length}`;
    $("#asmResMeta").textContent=r.query?`Query: ${r.query}`:"Assembly64 search complete.";
    asmRenderResults();
  }catch(e){
    $("#asmResMeta").textContent="Search failed.";
    $("#asmtable").className="asm-scroll asm-empty-state";
    $("#asmtable").innerHTML=asmEmpty("!","Unable to search",e.message);
  }
}
function asmRenderResults(){
  const arr=ASM.results||[];
  const table=$("#asmtable");
  if(!arr.length){
    table.className="asm-scroll asm-empty-state";
    table.innerHTML=asmEmpty("0","No matches","Try broadening the release name or removing one of the filters.");
    return;
  }
  table.className="asm-scroll";
  table.innerHTML=`<table class="asm-results-table"><colgroup><col style="width:28%"><col style="width:16%"><col class="asm-col-handle" style="width:14%"><col style="width:10%"><col style="width:7%"><col style="width:7%"><col class="asm-col-updated" style="width:11%"><col style="width:7%"></colgroup>
    <thead><tr><th>Release</th><th>Group</th><th class="asm-col-handle">Handle</th>
    <th>Category</th><th>Rating</th><th>Year</th><th class="asm-col-updated">Updated</th><th></th></tr></thead><tbody>`+
    arr.map((e,i)=>{
      const fav=itemSpec("assembly64",e.name||"Assembly64 release",[e.group,e.handle,e.year].filter(Boolean).join(" · "),"assembly_open",{entry:e});
      return `<tr onclick="asmFiles(${i})" class="${i===ASM.selectedIndex?"sel":""}" style="cursor:pointer">
        <td><div class="asm-result-name" title="${esc(e.name??"")}">${esc(e.name??"Untitled release")}</div>
          <div class="asm-result-sub">${esc([e.handle,asmUpdatedLabel(e.updated)].filter(Boolean).join(" · "))}</div></td>
        <td>${esc(e.group??"—")}</td><td class="asm-col-handle">${esc(e.handle??"—")}</td>
        <td class="hint">${esc(asmPresetLabel("category",e.category))}</td><td class="hint">${esc(asmRatingLabel(e.rating))}</td>
        <td class="hint">${esc(e.year||"—")}</td><td class="hint asm-col-updated" title="${esc(e.updated??"")}">${esc(asmUpdatedLabel(e.updated))}</td>
        <td><div class="asm-result-actions">${starButton(fav)} <button class="mini" onclick="event.stopPropagation();asmFiles(${i})">Files ▸</button></div></td></tr>`;
    }).join("")+`</tbody></table>`;
}
function asmFileType(path){
  const match=String(path||"").match(/\.([^.\\/]+)$/);
  return match?match[1].toUpperCase():"FILE";
}
async function asmFiles(i){
  const e=ASM.results[i];if(!e)return;
  ASM.current=e;ASM.selectedIndex=i;ASM.files=[];asmRenderResults();
  $("#asmFilesTitle").textContent="RELEASE FILES";
  $("#asmFilesMeta").textContent=[e.name,e.group,e.year].filter(Boolean).join(" · ");
  $("#asmfilelist").className="asm-scroll asm-empty-state";
  $("#asmfilelist").innerHTML=asmEmpty("…","Loading release files","Fetching the downloadable content list.");
  try{
    const r=await api(`/api/asm64/entries?id=${encodeURIComponent(e.id)}&category=${e.category??0}`);
    $("#asmraw").textContent=JSON.stringify(r,null,2).slice(0,20000);
    const files=Array.isArray(r.contentEntry)?r.contentEntry:[];ASM.files=files;
    if(!files.length){
      $("#asmfilelist").className="asm-scroll asm-empty-state";
      $("#asmfilelist").innerHTML=asmEmpty("0","No downloadable files","Assembly64 did not list any deployable files for this release.");
      return;
    }
    $("#asmFilesMeta").textContent=[e.name,e.group,`${files.length} file${files.length===1?"":"s"}`].filter(Boolean).join(" · ");
    $("#asmfilelist").className="asm-scroll";
    $("#asmfilelist").innerHTML=`<table class="asm-files-table"><colgroup><col><col style="width:70px"><col style="width:310px"></colgroup>
      <thead><tr><th>File</th><th>Type</th><th style="text-align:right">Actions</th></tr></thead><tbody>`+files.map(f=>{
      const raw=String(f.path??("item"+f.id));
      const fn=esc(raw);const encoded=encodeURIComponent(raw);
      const low=raw.toLowerCase();
      const disk=[".d64",".d71",".d81",".g64"].some(x=>low.endsWith(x));
      const inspectable=[".d64",".d71",".d81"].some(x=>low.endsWith(x));
      let acts=`<button class="mini" onclick="asmDeployEncoded(${f.id},'${encoded}','run')">Run</button>`;
      if(disk)acts=`${inspectable?`<button class="mini" onclick="asmDeployEncoded(${f.id},'${encoded}','inspect')">Open Image</button>`:""}
        <button class="mini mount-mode-action" data-mount-base="Mount to A" onclick="asmDeployEncoded(${f.id},'${encoded}','mount_a')">Mount to A · ${mountModeShort()}</button>
        <button class="mini mount-mode-action" data-mount-base="Mount to B" onclick="asmDeployEncoded(${f.id},'${encoded}','mount_b')">Mount to B · ${mountModeShort()}</button>
        <button class="mini primary mount-mode-action" data-mount-base="Mount & Run" onclick="asmDeployEncoded(${f.id},'${encoded}','mount_run')">Mount & Run · ${mountModeShort()}</button>`;
      return `<tr><td class="asm-file-name">${fn}</td><td class="hint">${asmFileType(raw)}</td><td><div class="asm-file-actions">${acts}</div></td></tr>`}).join("")+
      `</tbody></table>`;
  }catch(e2){
    $("#asmfilelist").className="asm-scroll asm-empty-state";
    $("#asmfilelist").innerHTML=asmEmpty("!","Unable to load files",e2.message);
  }
}
function asmDeployEncoded(item,filename,action){
  asmDeploy(item,decodeURIComponent(filename),action);
}
async function asmDeploy(item,filename,action){
  const e=ASM.current;if(!e)return;
  if(action==="run"&&String(filename||"").toLowerCase().endsWith(".crt"))clearStandaloneScreenNotice();
  toast((action==="inspect"?"Downloading ":"Deploying ")+filename+"…","ok");
  if(action==="mount_run")beginMountRunStatusWatch();
  try{
    const manifest=(ASM.files||[]).map(f=>({item:f.id,filename:String(f.path??("item"+f.id))}));
    const r=await api("/api/asm64/deploy",{method:"POST",timeoutMs:MOUNT_RUN_REQUEST_TIMEOUT_MS,headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id:e.id,category:e.category??0,item,filename,action,manifest})});
    if(action==="inspect"&&r&&r.token){tab("disks");showInspector(r);}
    else toast(asmActionLabel(action)+" ✓","ok");
    if(r?.swap_decision)showSwapDecision(r.swap_decision);
    rememberRecent(itemSpec("assembly64",e.name||filename,[e.group,e.handle].filter(Boolean).join(" · "),"assembly_open",{entry:e}));
    if(action.startsWith("mount")||r?.swap_decision)refreshDrives();
  }catch(e2){toast(e2.message,"err")}
  finally{if(action==="mount_run")finishMountRunStatusWatch()}}

/* ---------- disk swap ---------- */
let swapBusy=false;
function swapDecisionText(decision){
  if(!decision?.message)return "";
  return decision.detail?`${decision.message} · ${decision.detail}`:decision.message;
}
function showSwapDecision(decision,notify=true){
  const text=swapDecisionText(decision);if(!text)return;
  const line=$("#swapDecision");if(line)line.textContent=text;
  if(notify)toast(text,decision.kind==="related"?"ok":"");
}
function swapControlsBusy(busy){
  const panel=$("#swapPanel");
  if(!panel)return;
  panel.querySelectorAll("button").forEach(button=>button.disabled=busy);
}
async function swapRefresh(){
  try{
    const s=await api("/api/swap");
    const p=$("#swapPanel");
    showSwapDecision(s.decision,false);
    if(!s.items||s.items.length<2){p.style.display="none";return}
    p.style.display="block";
    $("#swapBtns").innerHTML=s.items.map((it,i)=>
      `<button class="mini ${i===s.index?"primary":""}" title="${esc(it.label)}"
        onclick="swapGo(${i})">${i+1}</button>`).join("");
    $("#swapLabel").textContent=(s.index>=0?s.items[s.index].label:"")+
      ` (drive ${(s.drive||"a").toUpperCase()})`;
  }catch(e){}
}
async function doSwap(url){
  if(swapBusy)return;
  swapBusy=true;
  // Clicking a swap button leaves that button focused in the browser. Space
  // would otherwise activate it again while the user is trying to control a
  // cracktro/game. Reclaim the C64 canvas immediately, before any network wait.
  screenEl.focus({preventScroll:true});
  swapControlsBusy(true);
  try{
    const r=await put(url);
    toast("Disk swapped → "+r.swapped_to+" ✓","ok");
    await refreshDrives();
  }catch(e){toast(e.message,"err")}
  finally{
    swapBusy=false;
    swapControlsBusy(false);
    screenEl.focus({preventScroll:true});
  }
}
async function swapGo(i){return doSwap("/api/swap/go?index="+i)}
async function swapNext(){return doSwap("/api/swap/next")}
async function swapPrev(){return doSwap("/api/swap/prev")}

/* ---------- disk queue ---------- */
const QUEUE=[];
function queueRender(){
  const el=$("#queueList");
  if(!QUEUE.length){el.innerHTML="Empty — ＋Queue disks above (in order: 1, 2, 3…), or pick local images below";return}
  el.innerHTML="<ol style='margin:4px 0 0 18px;padding:0'>"+QUEUE.map((p,i)=>
    `<li>${esc(p.rsplit?p:p.split("/").pop())} <a class="mini" style="color:var(--err);cursor:pointer"
      onclick="QUEUE.splice(${i},1);queueRender()">✕</a></li>`).join("")+"</ol>";
}
function queueAdd(path){
  if(QUEUE.includes(path)){toast("Already queued","err");return}
  QUEUE.push(path);queueRender();toast("Queued as disk "+QUEUE.length,"ok");
}
function queueClear(){QUEUE.length=0;queueRender()}
async function queueArm(mountFirst){
  if(!QUEUE.length){toast("Queue is empty","err");return}
  try{
    const r=await api("/api/swap/set_paths",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({paths:QUEUE,mount_first:mountFirst,
        mode:$("#localmode")?$("#localmode").value:"unlinked"})});
    toast(mountFirst?("Mounted "+(r.swapped_to||"disk 1")+" ✓ — queue armed"):"Queue armed ✓","ok");
    swapRefresh();refreshDrives();
    if(mountFirst){tab("screen");screenEl.focus()}
  }catch(e){toast(e.message,"err")}
}
async function queueLocalArm(){
  const fl=$("#queueLocal").files;
  if(!fl.length){toast("Pick local disk images first","err");return}
  const fd=new FormData();
  for(const f of fl)fd.append("files",f);
  fd.append("drive","a");fd.append("mode",$("#localmode")?$("#localmode").value:"unlinked");
  toast("Uploading set…","ok");
  try{
    const r=await api("/api/swap/upload",{method:"POST",body:fd});
    toast("Mounted "+(r.swapped_to||"disk 1")+" ✓ — set armed","ok");
    $("#queueLocal").value="";swapRefresh();refreshDrives();tab("screen");screenEl.focus();
  }catch(e){toast(e.message,"err")}
}

/* ---------- SID jukebox ---------- */
let JK={state:null,poll:null,pollBusy:false,stopPending:false,listSignature:"",activeRowKey:""};
const SIDFLOWUI={status:null,poll:null};
const SIDIDX={poll:null,volumes:[],volumesLoaded:false,last:null};
function fmtLen(s){if(s==null||s==="")return"—";const m=Math.floor(s/60),x=Math.round(s%60);
  return m+":"+String(x).padStart(2,"0")}
function sidChipKind(meta){
  const m=meta||{},chip=String(m.chip||"").trim().toLowerCase();
  if(!chip||chip==="?")return "unknown";
  if((Number(m.sids)||1)>1||chip.includes("+"))return "mixed";
  if(chip==="6581"||chip==="8580"||chip==="either")return chip;
  return "unknown";
}
function sidChipBadge(meta,showUnknown=false){
  const m=meta||{},kind=sidChipKind(m),raw=String(m.chip||"").trim();
  if(kind==="unknown"&&!showUnknown&&!m.format)return "";
  const label=kind==="unknown"?"Unknown":raw;
  const count=(Number(m.sids)||1)>1?" ×"+(Number(m.sids)||1):"";
  return `<span class="badge chip-${kind}" title="Declared in the SID header — used by the firmware to select the matching SID socket">${esc(label)}${count}</span>`;
}
function jkAudioSync(){
  const b=$("#jkAudio");if(!b)return;
  b.textContent=audioOn?"🔇 Stop listening":"🔊 Listen here";
  b.classList.toggle("primary",audioOn);
}
function jkListSignature(s){
  return JSON.stringify([s.folder||"",s.source||"",!!s.loading,(s.items||[]).map(it=>[
    it.path||"",it.label||"",it.lazy?1:0,it.meta?.name||"",it.meta?.author||"",
    it.meta?.chip||"",it.meta?.songs||1,it.song||1,it.similarity??null,it.length??null])]);
}
function jkQueueContext(s){
  if(s.radio)return "SIDFlow Radio";
  if(s.recommendation_seed_label)return `More like ${s.recommendation_seed_label}`;
  if(s.folder==="SIDFlow Radio")return "SIDFlow Radio";
  const parts=[s.folder||"",s.source||""].filter(Boolean);
  return [...new Set(parts)].join(" · ");
}
function jkLocateCurrent(smooth=true){
  const s=JK.state||{},index=Number(s.index),list=$("#jkList");
  if(!list||!Number.isInteger(index)||index<0)return false;
  const row=list.querySelector(`[data-juke-index="${index}"]`);
  if(!row)return false;
  const head=list.querySelector(".jkq-head"),listRect=list.getBoundingClientRect(),rowRect=row.getBoundingClientRect();
  const headHeight=head?head.offsetHeight:0;
  const rowTop=list.scrollTop+(rowRect.top-listRect.top);
  const room=Math.max(0,list.clientHeight-headHeight-rowRect.height);
  const wanted=rowTop-headHeight-Math.round(room*.34);
  const top=Math.min(Math.max(0,list.scrollHeight-list.clientHeight),Math.max(0,wanted));
  list.scrollTo({top,left:list.scrollLeft,behavior:smooth?"smooth":"auto"});
  return true;
}

function jkNowFavouriteSync(n){
  const button=$("#jkNowStar");if(!button)return;
  const path=String(n?.path||"");
  if(!n||!path){JK.nowFavouriteItem=null;button.style.display="none";button.removeAttribute("data-item-key");return}
  const name=path.split("/").pop();
  const item=itemSpec("sid",n.meta?.name||n.label||name,path,"sid_play",{folder:parentPath(path),name});
  JK.nowFavouriteItem=item;button.style.display="inline-block";button.dataset.itemKey=itemKey(item);
  const on=!!favMatch(item);button.classList.toggle("on",on);button.textContent=on?"★":"☆";
  button.setAttribute("aria-label",(on?"Remove ":"Add ")+(item.label||name)+(on?" from":" to")+" Favourites");
}
async function jkToggleNowFavourite(){
  if(!JK.nowFavouriteItem)return;
  await toggleFavorite(JK.nowFavouriteItem);
  jkNowFavouriteSync((JK.state||{}).now);
}

function jukeFadeControlsSync(s){
  const cfg=s?.browser_fade||{},box=$("#jkFadeEnabled"),sel=$("#jkFadeSecs"),note=$("#jkFadeNote");if(!box||!sel)return;
  box.checked=!!cfg.enabled;sel.value=String(Number(cfg.duration_secs||2.5).toFixed(1));sel.disabled=!box.checked;
  if(note)note.textContent=cfg.note||"Browser/recording only — Ultimate HDMI and analogue audio do not fade.";
}
async function jukeFadeChanged(){
  const box=$("#jkFadeEnabled"),sel=$("#jkFadeSecs"),enabled=!!box.checked,duration=Number(sel.value||2.5);sel.disabled=!enabled;
  try{const r=await api("/api/juke/fade",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled,duration_secs:duration})});
    if(JK.state)JK.state={...JK.state,browser_fade:r};jukeFadeControlsSync(JK.state);
    toast(enabled?`Browser SID fade: ${duration.toFixed(1)} seconds — applies from the next SID`:`Browser SID fade disabled — applies from the next SID`,"ok");}
  catch(e){toast(e.message,"err");jkRefresh()}
}

function jkRender(s){
  jkAudioSync();
  JK.state=s;jukeFadeControlsSync(s);jukeFadeSync(s);
  const n=s.now;
  if(n){
    const m=n.meta||{};
    $("#jkNow").innerHTML="♪ "+esc(m.name||n.label)+(m.author?" — "+esc(m.author):"")+
      (sidChipBadge(m)?" &nbsp;"+sidChipBadge(m):"")+
      (m.songs>1?` &nbsp;[song ${n.song}/${m.songs}]`:"")+
      (n.length?" &nbsp;("+fmtLen(n.length)+")":"")+
      (s.loading?" &nbsp;· loading…":"");
  }else $("#jkNow").textContent=s.playing?"":"stopped";
  jkNowFavouriteSync(n);
  $("#jkShuffle").checked=!!s.shuffle;
  $("#jkRadio").checked=!!s.radio;
  const moreNow=$("#jkMoreNow");
  moreNow.style.display=n&&n.path?"inline-block":"none";
  moreNow.disabled=!n||!n.path;
  $("#jkSidflowCredit").classList.toggle("hint",!(s.sidflow||{}).available);
  $("#jkSlInfo").textContent=s.songlengths_loaded
    ?`Songlengths loaded (${Number(s.songlengths_loaded).toLocaleString()} entries) — accurate auto-advance`
    :"No Songlengths.md5 configured — auto-advance uses sid_default_secs (config.json)";
  const songSel=$("#jkSong");
  if(n&&n.meta.songs>1){
    songSel.style.display="inline";
    songSel.innerHTML="<select onchange='jkPlaySong(this.value)'>"+
      Array.from({length:n.meta.songs},(_,i)=>`<option value="${i+1}" ${i+1===n.song?"selected":""}>song ${i+1}</option>`).join("")+"</select>";
  }else songSel.style.display="none";
  const lp=$("#jkListPanel"),items=Array.isArray(s.items)?s.items:[];
  lp.style.display="flex";
  plControlsSync(s);
  if(!items.length){
    $("#jkListTitle").textContent="PLAY QUEUE — 0 TUNES";
    $("#jkListContext").textContent="";
    $("#jkListContext").title="";
    $("#jkLocateCurrentBtn").disabled=true;
    JK.activeRowKey="";
    if(JK.listSignature!=="empty"){
      JK.listSignature="empty";
      $("#jkList").innerHTML='<div class="sid-queue-empty" role="status"><strong>No tunes queued</strong><span>Choose a saved play queue above, add a SID, or play a folder.</span></div>';
    }
    return;
  }
  const itemAuthors=items.map(it=>String(it.meta?.author||"").trim());
  const knownAuthors=[...new Set(itemAuthors.filter(Boolean))];
  const singleAuthor=knownAuthors.length===1&&itemAuthors.every(a=>a===knownAuthors[0]);
  const tuneWord=items.length===1?"TUNE":"TUNES";
  $("#jkListTitle").textContent=singleAuthor
    ?`PLAY QUEUE — ${knownAuthors[0]} · ${items.length} ${tuneWord}`
    :`PLAY QUEUE — ${items.length} ${tuneWord}`;
  const queueContext=jkQueueContext(s);
  $("#jkListContext").textContent=[queueContext,s.loading?"Loading…":""].filter(Boolean).join(" · ");
  $("#jkListContext").title=$("#jkListContext").textContent;
  $("#jkLocateCurrentBtn").disabled=!(s.playing&&Number(s.index)>=0);
  const sig=jkListSignature(s)+"|singleAuthor:"+(singleAuthor?knownAuthors[0]:"");
  if(sig!==JK.listSignature){
    JK.listSignature=sig;
    const authorHead=singleAuthor?"":'<div class="jkq-cell jkq-author" role="columnheader">Author</div>';
    const rows=items.map((it,i)=>{
      const m=it.meta||{},author=String(m.author||""),released=String(m.released||"");
      const compactMeta=[singleAuthor?"":author,released].filter(Boolean).join(" · ");
      const authorCell=singleAuthor?"":`<div class="jkq-cell jkq-author" role="cell" title="${esc(author)}">${esc(author)}</div>`;
      return `<div class="jkq-row" role="row" data-juke-index="${i}" onclick="jkPlay(${i},${Number(it.song||0)})">
        <div class="jkq-cell jkq-index" role="cell"><span class="jkq-current-marker" aria-hidden="true">▶</span><span class="jkq-row-number">${i+1}</span></div>
        <div class="jkq-cell jkq-title" role="cell" title="${esc(m.name||it.label)}">${esc(m.name||it.label)}${it.similarity!=null?` <span class="jkq-similarity" title="${it.recommendation_source==="u64deck-fallback"?"Fallback feature similarity from u64deck":"SIDFlow 0.8.0 weighted neighbour similarity"}">${Math.round(Number(it.similarity)*100)}% match</span>`:""}${it.lazy?' <span class="hint">· loads when played</span>':""}<div class="jkq-submeta">${esc(compactMeta)}${it.song&&m.songs>1?` · song ${it.song}`:""}</div></div>
        ${authorCell}<div class="jkq-cell jkq-chip" role="cell">${sidChipBadge(m)}</div>
        <div class="jkq-cell jkq-released hint" role="cell">${esc(released)}</div>
        <div class="jkq-cell jkq-length" role="cell">${fmtLen(it.length)}</div>
        <div class="jkq-cell jkq-actions" role="cell">${it.path?`<button class="mini" onclick="event.stopPropagation();jkMoreLike(${i})" title="Find tunes similar to this queue entry and insert them after the current tune">♪</button>`:""}${it.path?starButton(itemSpec("sid",m.name||it.label,it.path,"sid_play",{folder:parentPath(it.path),name:it.path.split("/").pop()})):""}
          <button class="mini" onclick="event.stopPropagation();jkRemove(${i})" title="Remove from play queue">✕</button></div></div>`;
    }).join("");
    $("#jkList").innerHTML=`<div class="jkq-grid ${singleAuthor?"single-author":""}" role="rowgroup">
      <div class="jkq-row jkq-head" role="row"><div class="jkq-cell" role="columnheader">#</div><div class="jkq-cell" role="columnheader">Title</div>${authorHead}<div class="jkq-cell" role="columnheader">Chip</div><div class="jkq-cell jkq-released" role="columnheader">Released</div><div class="jkq-cell" role="columnheader">Length</div><div class="jkq-cell" role="columnheader"></div></div>${rows}</div>`;
  }
  document.querySelectorAll("#jkList [data-juke-index]").forEach(row=>{
    const current=+row.dataset.jukeIndex===Number(s.index)&&!!s.playing;
    row.classList.toggle("playing",current);
    if(current){row.setAttribute("aria-current","true");row.title="Currently playing"}
    else{row.removeAttribute("aria-current");row.removeAttribute("title")}
  });
  const activeRowKey=s.playing&&Number(s.index)>=0?`${Number(s.playback_id)||0}:${Number(s.index)}`:"";
  if(activeRowKey&&activeRowKey!==JK.activeRowKey)requestAnimationFrame(()=>jkLocateCurrent(true));
  JK.activeRowKey=activeRowKey;
}

async function jkRefresh(){
  if(JK.pollBusy||JK.stopPending)return;JK.pollBusy=true;
  try{jkRender(await api("/api/juke",{timeoutMs:5000}))}catch(e){}
  finally{JK.pollBusy=false}
}
function jkPollStart(){plRefresh();if(JK.poll)return;JK.poll=setInterval(()=>{
  const active=document.querySelector("#tab-sid.active");
  if(active||((JK.state||{}).playing))jkRefresh()},3000)}
const JKB={path:"/"};
async function jkHome(){
  JK.homing=true;
  $("#jkBDirs").innerHTML='<span class="hint">looking for your SID collection…</span>';
  try{
    const h=await api("/api/juke/hvsc");
    if(h.path){
      if(h.detected)toast("HVSC found: "+h.path+(h.songlengths_loaded?` — ${h.songlengths_loaded} songlengths`:""),"ok");
      JK.homing=false;await jkBrowse(h.path);return}
  }catch(e){}
  finally{JK.homing=false}
  await jkBrowse((typeof FS!=="undefined"&&FS.path&&FS.path!=="/")?FS.path:"/");
}
async function jkBrowse(path){
  $("#jkBDirs").innerHTML='<span class="hint">browsing…</span>';
  try{
    const r=await api("/api/fs?path="+encodeURIComponent(path||"/"));
    JKB.path=r.path||path||"/";
    $("#jkPath").value=JKB.path;
    $("#jkBPath").textContent=JKB.path;
    const dirs=r.entries.filter(e=>e.dir).sort((a,b)=>a.name.localeCompare(b.name));
    const sidNames=r.entries.filter(e=>!e.dir&&e.name.toLowerCase().endsWith(".sid"))
      .map(e=>e.name).sort((a,b)=>a.localeCompare(b));
    const sids=sidNames.length;
    $("#jkBCount").textContent=sids?`— ${sids} SID${sids>1?"s":""} here`:"";
    $("#jkBLoad").style.display=sids?"inline-block":"none";
    const folderQueueSources=new Set(["SQLite index","Ultimate folder"]);
    const loadedHere=(JK.state&&folderQueueSources.has(String(JK.state.source||""))&&
      String(JK.state.folder||"").toLowerCase()===JKB.path.toLowerCase());
    $("#jkBDirs").innerHTML=(dirs.length?dirs.map(d=>{
      const path=(JKB.path==="/"?"":JKB.path)+"/"+d.name;
      const fav=itemSpec("sid_folder",d.name,path,"sid_folder",{path});
      return `<span class="row" style="gap:2px">
        <button class="mini" onclick="jkBrowse('${jsq(path)}')">📁 ${esc(d.name)}</button>${starButton(fav)}</span>`;
    }).join("")
      :'<span class="hint">no subfolders</span>')+
      (sids?`<div style="flex-basis:100%;margin-top:4px" class="hint">`+
        sidNames.slice(0,60).map(n=>{
          const path=(JKB.path==="/"?"":JKB.path)+"/"+n;
          const fav=itemSpec("sid",n.replace(/\.sid$/i,""),path,"sid_play",{folder:JKB.path,name:n});
          return `<span style="display:inline-flex;align-items:center;margin-right:10px">
            <a style="cursor:pointer" title="Play only this tune now"
              onclick="jkPlayFrom('${jsq(JKB.path)}','${jsq(n)}')">♪ ${esc(n.replace(/\.sid$/i,""))}</a>
              <button class="mini" onclick="jkAdd('${jsq(JKB.path)}','${jsq(n)}')" title="Add only this tune to the current play queue">＋</button>${starButton(fav)}</span>`;
        }).join("")+
        (sids>60?` …and ${sids-60} more`:"")+
        (loadedHere?' <span class="badge ok">this folder is the current play queue</span>':"")+
        `</div>`:"");
  }catch(e){$("#jkBDirs").innerHTML=`<span style="color:var(--err)">${esc(e.message)}</span>
    <button class="mini" data-retry onclick="jkBrowse(JKB.path)">retry</button>`}
}
function jkBrowseUp(){
  const p=JKB.path.replace(/\/+$/,"");
  jkBrowse(p.includes("/")?(p.slice(0,p.lastIndexOf("/"))||"/"):"/");
}
async function jukeFolder(path){
  if(!path){toast("Enter a folder path","err");return}
  tab("sid");$("#jkPath").value=path;
  toast("Loading SIDs…","ok");
  try{const s=await api("/api/juke/folder",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({path}),timeoutMs:25000});
    jkRender(s);toast(s.items.length+" tunes loaded"+(s.skipped?` (${s.skipped} skipped)`:""),"ok");
    rememberRecent(itemSpec("sid_folder",path.split("/").filter(Boolean).pop()||path,path,"sid_folder",{path}))}
  catch(e){toast(e.message,"err")}}
async function jukeLocal(){
  const fl=$("#jkLocal").files;if(!fl.length){toast("Pick .sid files first","err");return}
  const fd=new FormData();for(const f of fl)fd.append("files",f);
  try{const s=await api("/api/juke/upload",{method:"POST",body:fd});
    $("#jkLocal").value="";jkRender(s);toast(s.items.length+" tunes loaded","ok")}
  catch(e){toast(e.message,"err")}}
async function jkHvsc(force){
  $("#jkBDirs").innerHTML='<span class="hint">'+(force?"re-detecting HVSC…":"…")+'</span>';
  try{
    const h=await api("/api/juke/hvsc"+(force?"?force=true":""));
    if(h.path){
      if($("#jkSidRoot"))$("#jkSidRoot").value=h.path;
      if(h.detected)toast("HVSC "+(force?"re-":"")+"detected: "+h.path+
        (h.songlengths_loaded?` — ${h.songlengths_loaded} songlengths`:""),"ok");
      jkBrowse(h.path);
    }else{toast("No HVSC found on device storage (a folder containing MUSICIANS + DOCUMENTS)","err");
      jkBrowse("/")}
  }catch(e){toast(e.message,"err")}}
function jkSearchFilterChanged(){
  const filtered=$("#jkChip").value!=="all"||$("#jkFormat").value!=="all"||!!$("#jkYear").value.trim();
  $("#jkSearch").placeholder=filtered?"optional title, author or path…":"🔎 search all of HVSC…";
}
function jkSidBadges(meta){
  const m=meta||{},out=[];
  const chip=sidChipBadge(m,true);if(chip)out.push(chip);
  if(m.format)out.push(`<span class="badge">${esc(m.format)}</span>`);
  return out.join(" ");
}
async function jkSearchGo(){
  const q=$("#jkSearch").value.trim(),chip=$("#jkChip").value,format=$("#jkFormat").value,year=$("#jkYear").value.trim();
  if(q&&q.length<2){toast("Type at least 2 characters","err");return}
  if(year&&!/^(?:19|20)\d{2}$/.test(year)){toast("Enter a four-digit year from 1900 to 2099","err");return}
  if(!q&&chip==="all"&&format==="all"&&!year){toast("Enter a search term or choose a Chip/Format/Year filter","err");return}
  $("#jkSearchOut").style.display="block";
  $("#jkSearchList").innerHTML='<span class="hint">searching…</span>';
  try{
    const params=new URLSearchParams({q,chip,format,year});
    const r=await api("/api/juke/search?"+params.toString());
    const what=q?`"${q}"`:"filtered search";
    $("#jkSearchTitle").textContent=`${what} — ${r.total} match${r.total===1?"":"es"} in ${r.indexed} indexed tunes · ${r.backend||"index"}`+
      (r.total>r.results.length?` (showing ${r.results.length})`:"");
    if(!r.results.length){$("#jkSearchList").innerHTML='<span class="hint">no matches</span>';return}
    $("#jkSearchList").innerHTML=r.results.map(h=>{
      const m=h.meta||{},title=m.name||h.name.replace(/\.sid$/i,"");
      return `<div class="hitrow" onclick="jkPlayFrom('${jsq(h.folder)}','${jsq(h.name)}')"
         title="Play only this tune now">
        <span style="flex:0 0 auto">▶</span>
        <span style="min-width:170px"><b>${esc(title)}</b>${m.author?`<br><span class="hint">${esc(m.author)}</span>`:""}</span>
        <span style="white-space:nowrap">${jkSidBadges(m)}</span>
        <span class="hint" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(h.rel.slice(0,h.rel.lastIndexOf("/")))}</span>
        ${starButton(itemSpec("sid",title,h.path,"sid_play",{folder:h.folder,name:h.name}))}
        <button class="mini" onclick="event.stopPropagation();jkAdd('${jsq(h.folder)}','${jsq(h.name)}')" title="Add only this tune to the current play queue without interrupting playback">＋</button>
        <button class="mini" onclick="event.stopPropagation();jkBrowse('${jsq(h.folder)}')">📁 folder</button></div>`}).join("");
  }catch(e){$("#jkSearchList").innerHTML=`<span style="color:var(--err)">${esc(e.message)}</span>`}}

function jkSidIndexTogglePanel(){
  const panel=$("#jkSidIndex"),button=$("#jkSidIndexToggle");if(!panel)return;
  panel.open=!panel.open;
  if(button)button.setAttribute("aria-expanded",panel.open?"true":"false");
  if(panel.open){
    jkSidIndexInit();
    requestAnimationFrame(()=>panel.scrollIntoView({behavior:"smooth",block:"nearest"}));
  }
}
async function jkSidIndexInit(){
  if(!SIDIDX.volumesLoaded)jkSidVolumesLoad();
  try{
    const s=await api("/api/juke/index/status");
    SIDIDX.last=s;
    if(!$("#jkSidRoot").value)$("#jkSidRoot").value=s.configured_root||$("#jkPath").value||"/USB0/HVSC";
    if(!$("#jkSidSource").value&&s.local_source)$("#jkSidSource").value=s.local_source;
    jkSidIndexRender(s);
    if(s.running)jkSidIndexPollStart();
  }catch(e){}
}
async function jkSidVolumesLoad(){
  const sel=$("#jkSidVolume");if(!sel)return;
  try{
    const r=await api("/api/local/volumes");SIDIDX.volumes=r.volumes||[];SIDIDX.volumesLoaded=true;
    sel.innerHTML='<option value="">Select a detected drive…</option>'+SIDIDX.volumes.map((v,i)=>
      `<option value="${i}">${esc((v.label?v.label+" · ":"")+v.path+" · "+v.type)}</option>`).join("")+'<option value="manual">Manual path…</option>';
    const first=SIDIDX.volumes.findIndex(v=>v.removable);
    if(first>=0&&!$("#jkSidSource").value){sel.value=String(first);jkSidVolumePicked()}
  }catch(e){sel.innerHTML='<option value="">Drive detection unavailable</option>'}
}
function jkSidVolumePicked(){
  const value=$("#jkSidVolume").value;if(value==="manual"){$("#jkSidSource").focus();return}
  const v=SIDIDX.volumes[+value];if(v)$("#jkSidSource").value=v.path;
}
async function jkSidIndexUltimate(){
  try{
    const state=SIDIDX.last||await api("/api/juke/index/status");
    if(state.running){await api("/api/juke/index/stop",{method:"POST"});toast("Stopping SID metadata refresh…","ok");return}
    const root=$("#jkSidRoot").value.trim()||$("#jkPath").value.trim();
    if(!root){toast("Enter or detect the Ultimate HVSC path","err");return}
    if(!confirm(`Refresh SID metadata from the Ultimate?\n\n${root}\n\nOnly SID headers are read. A complete collection may take a long time over FTP; the local option is faster.`))return;
    await api("/api/juke/index/ultimate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({root,force:$("#jkSidForce").checked})});
    toast("SID metadata refresh started from Ultimate","ok");jkSidIndexPollStart();
  }catch(e){toast(e.message,"err")}
}
async function jkSidIndexLocal(){
  try{
    const state=SIDIDX.last||await api("/api/juke/index/status");
    if(state.running){await api("/api/juke/index/stop",{method:"POST"});toast("Stopping SID metadata refresh…","ok");return}
    const source=$("#jkSidSource").value.trim(),root=$("#jkSidRoot").value.trim()||"/USB0/HVSC";
    if(!source){toast("Choose the local HVSC folder","err");return}
    if(!confirm(`Build the SID metadata index from:\n\n${source}\n\nas ${root}?\n\nThe scan reads only SID headers and does not modify files.`))return;
    await api("/api/juke/index/local",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({source,root,force:$("#jkSidForce").checked})});
    toast("Local SID metadata scan started","ok");jkSidIndexPollStart();
  }catch(e){toast(e.message,"err")}
}
async function jkSidIndexPause(){
  try{
    const s=SIDIDX.last||await api("/api/juke/index/status");if(!s.running)return;
    await api("/api/juke/index/pause",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({paused:!s.manual_paused})});
    jkSidIndexPollTick();
  }catch(e){toast(e.message,"err")}
}
function jkSidIndexPollStart(){
  if(SIDIDX.poll)return;SIDIDX.poll=setInterval(jkSidIndexPollTick,1000);jkSidIndexPollTick();
}
function jkSidIndexRender(s){
  SIDIDX.last=s;const out=$("#jkSidIndexStatus"),ub=$("#jkSidUltimateBtn"),lb=$("#jkSidLocalBtn"),pb=$("#jkSidPauseBtn"),tb=$("#jkSidIndexToggle");
  if(tb){
    const count=Number(s.metadata_count||0);
    tb.classList.toggle("sid-index-running",!!s.running);
    tb.classList.toggle("sid-index-ready",!s.running&&count>0);
    tb.textContent=s.running?`SID Index · ${Number(s.parsed||0).toLocaleString()} parsed`:
      (count?`SID Index · ${count.toLocaleString()}`:"SID Index");
    tb.title=s.running?"SID metadata indexing is running — open controls and progress":
      (count?`${count.toLocaleString()} SID tunes indexed — open SID Index controls and status`:"Open SID metadata indexing controls and status");
  }
  if(!out)return;
  if(s.running){
    out.style.display="block";ub.textContent="⏹ Stop SID Refresh";lb.textContent="⏹ Stop SID Refresh";pb.style.display="inline-block";pb.textContent=s.manual_paused?"▶ Resume":"⏸ Pause";
    const head=s.mode==="local"?`💻 Local SID index ${s.source} → ${s.root}`:`♪ SID refresh from Ultimate · ${s.root}`;
    const state=s.paused?`⏸ ${s.pause_reason||"Paused"}`:`${s.current||"Starting…"}`;
    out.textContent=`${head} — ${s.files||0} files · ${s.parsed||0} parsed · ${s.cached||0} unchanged · ${s.errors||0} errors · ${fmtDuration(s.elapsed||0)} · ${s.files_per_sec||0} files/s · ${state}`;
  }else{
    ub.textContent="Refresh From Ultimate";lb.textContent="Build From Local HVSC";pb.style.display="none";
    const run=(s.runs||[])[0];
    if(s.error){out.style.display="block";out.textContent="SID index error: "+s.error}
    else if(run){out.style.display="block";out.textContent=`SID metadata: ${s.metadata_count||0} tunes · last ${run.mode} scan ${run.completed} · ${run.parsed} parsed · ${run.cached} unchanged · ${run.errors} errors`}
    else if(s.metadata_count){out.style.display="block";out.textContent=`SID metadata: ${s.metadata_count} indexed tunes`}
    else out.style.display="none";
  }
}
async function jkSidIndexPollTick(){
  try{const s=await api("/api/juke/index/status");jkSidIndexRender(s);if(!s.running&&SIDIDX.poll){clearInterval(SIDIDX.poll);SIDIDX.poll=null;await jkRefresh()}}
  catch(e){}
}
async function jkUpdateCheck(){
  $("#jkVer").textContent="Checking…";
  try{
    const v=await api("/api/juke/hvsc_version");
    if(!v.installed&&!v.latest){$("#jkVer").textContent="Couldn't determine versions";return}
    let msg=(v.installed?("installed: #"+v.installed):"installed: ?")+
            (v.latest?(" · latest: #"+v.latest):" · latest: ?");
    if(v.up_to_date)msg+=" — up to date ✓";
    else if(v.installed&&v.latest)msg+=" — update available!";
    $("#jkVer").innerHTML=esc(msg)+(v.up_to_date?"":
      ` <a href="${esc(v.downloads)}" target="_blank" style="color:var(--acc)">get it</a>`+
      ` <span class="hint" title="${esc(v.note)}">ⓘ</span>`);
  }catch(e){$("#jkVer").textContent=e.message}}
async function jkRandom(){
  const root=JKB.path&&JKB.path!=="/"?JKB.path:($("#jkPath").value||"/");
  toast("Choosing a random indexed SID…","ok");
  try{const s=await api("/api/juke/random",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({root}),timeoutMs:25000});
    jkRender(s);toast("♪ Selected from "+(s.indexed_candidates||0)+" indexed SIDs"+
      (s.loading?" — loading its folder in the background":""),"ok");
    if(s.selected)rememberRecent(itemSpec("sid",s.selected.split("/").pop().replace(/\.sid$/i,""),s.selected,"sid_play",{folder:parentPath(s.selected),name:s.selected.split("/").pop()}))}
  catch(e){toast(e.message,"err")}}
function plControlsSync(s=JK.state||{}){
  const hasItems=Array.isArray(s.items)&&s.items.length>0;
  const selected=!!$("#plSel")?.value;
  const save=$("#plSaveBtn"),clear=$("#jkClearQueueBtn"),del=$("#plDeleteBtn");
  if(save)save.disabled=!hasItems;
  if(clear)clear.disabled=!hasItems;
  if(del)del.disabled=!selected;
}
async function plRefresh(){
  try{
    const r=await api("/api/playlists");
    const sel=$("#plSel"),cur=sel.value;
    sel.innerHTML='<option value="">— saved play queues —</option>'+
      r.playlists.map(p=>`<option value="${esc(p.name)}">${esc(p.name)} (${p.count})</option>`).join("");
    if([...sel.options].some(o=>o.value===cur))sel.value=cur;
    plControlsSync();
  }catch(e){}}
async function plSave(){
  const name=$("#plName").value.trim();
  if(!name){toast("Give the play queue a name","err");return}
  try{
    const r=await api("/api/playlists/save",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({name})});
    toast(`💾 "${r.saved}" — ${r.count} tunes`+
      (r.local_skipped?` (${r.local_skipped} local-only not saved)`:""),"ok");
    $("#plName").value="";plRefresh();
  }catch(e){toast(e.message,"err")}}
async function plLoad(){
  const name=$("#plSel").value;
  if(!name){toast("Pick a saved play queue first","err");return}
  try{
    const s=await api("/api/playlists/load",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({name})});
    jkRender(s);
    toast(`▶ "${name}" loaded — ${s.items.length} tunes`+
      (s.skipped?` (${s.skipped} unfetchable)`:""),"ok");
  }catch(e){toast(e.message,"err")}}
async function plDelete(){
  const name=$("#plSel").value;
  if(!name){toast("Pick a saved play queue first","err");return}
  try{await api("/api/playlists/delete",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({name})});
    toast(`🗑 "${name}" deleted`,"ok");plRefresh();
  }catch(e){toast(e.message,"err")}}
async function jkAdd(folder,name){
  try{
    const s=await api("/api/juke/add_path",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({path:(folder==="/"?"":folder)+"/"+name})});
    jkRender(s);
    toast("＋ "+(s.added||name)+" → play queue ("+s.items.length+")","ok");
  }catch(e){toast(e.message,"err")}}
async function jkRemove(i){
  try{jkRender(await api("/api/juke/remove",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({index:i})}))}
  catch(e){toast(e.message,"err")}}
async function jkClearQueue(){
  const s=JK.state||{},items=s.items||[];
  const keepCurrent=!!(s.playing&&s.index>=0&&s.index<items.length);
  const removeCount=Math.max(0,items.length-(keepCurrent?1:0));
  if(!removeCount&&!s.radio){toast(keepCurrent?"No queued tunes behind the current SID":"Play queue is already empty","ok");return}
  if(removeCount>1&&!confirm(`Clear ${removeCount} queued tunes?\n\n${keepCurrent?"The current SID will continue playing and Radio will be turned off.":"Radio will be turned off."}`))return;
  try{const out=await api("/api/juke/clear",{method:"POST"});jkRender(out);
    toast(out.cleared?
      (out.kept_current?`Cleared ${out.cleared} queued tunes — current SID will stop at its normal end`:`Play queue cleared (${out.cleared} tunes)`):
      "Radio is off — current SID will stop at its normal end","ok")}
  catch(e){toast(e.message,"err")}}
async function jkPlayFrom(folder,name){
  jukeFadePrepareReplacement();
  try{
    const path=(folder==="/"?"":folder)+"/"+name;
    const s=await api("/api/juke/play_path",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({path}),timeoutMs:20000});
    jkRender(s);
    rememberRecent(itemSpec("sid",s.now?.meta?.name||name,path,"sid_play",{folder,name}));
    toast("♪ "+(s.now?.meta?.name||name)+" — playing as a one-tune queue","ok");
  }catch(e){jukeFadeCancel(true);toast(e.message,"err")}}
async function jkPlay(i,song){jukeFadePrepareReplacement();try{
  jkRender(await put("/api/juke/play?index="+i+(song?"&song="+song:""),{timeoutMs:20000}))}
  catch(e){jukeFadeCancel(true);toast(e.message,"err")}}
function jkPlayCurrent(){jkPlay(Math.max(0,(JK.state||{}).index||0))}
function jkPlaySong(s){if(JK.state&&JK.state.index>=0)jkPlay(JK.state.index,+s)}
async function jkStop(){
  if(JK.stopPending)return;
  JK.stopPending=true;AUDIO_JUKE_STOP_MUTED=true;jukeFadeCancel(true);
  // Stop all browser-scheduled audio immediately and drop incoming chunks until
  // the backend reset completes. This prevents the live WebSocket from simply
  // refilling the Web Audio queue while Stop is in flight.
  flushBrowserAudio();
  // Make the controls respond immediately while the low-latency reset packet
  // is sent. Suppress the periodic refresh until the authoritative response
  // arrives so an older playing snapshot cannot flash back into the UI.
  if(JK.state){JK.state={...JK.state,playing:false};jkRender(JK.state)}
  try{jkRender(await put("/api/juke/stop",{timeoutMs:6000}))}
  catch(e){toast(e.message,"err")}
  finally{flushBrowserAudio();AUDIO_JUKE_STOP_MUTED=false;JK.stopPending=false}
}
async function jk(a){jukeFadePrepareReplacement();try{jkRender(await put("/api/juke/"+a))}catch(e){jukeFadeCancel(true);toast(e.message,"err")}}
async function jkShuffleSet(){try{
  jkRender(await put("/api/juke/shuffle?on="+($("#jkShuffle").checked?"true":"false")))}
  catch(e){toast(e.message,"err")}}

function sidflowBytes(n){
  n=Number(n||0);if(!n)return "0 B";const units=["B","KiB","MiB","GiB"];let i=0;
  while(n>=1024&&i<units.length-1){n/=1024;i++}return (i? n.toFixed(n>=100?0:n>=10?1:2):String(Math.round(n)))+" "+units[i]
}
function sidflowStatusRender(s){
  SIDFLOWUI.status=s;const out=$("#sidflowStatus"),bar=$("#sidflowProgress"),dl=$("#sidflowDownloadBtn"),rm=$("#sidflowRemoveBtn");
  if(!out)return;
  const j=s.job||{};
  if(j.running){
    const stage=String(j.stage||"working"),done=stage==="downloading"?Number(j.downloaded||0):Number(j.processed||0),total=stage==="downloading"?Number(j.total||0):Number(j.process_total||0);
    out.textContent=(j.message||"Preparing SIDFlow data…")+(total?` — ${Math.round(done*100/total)}%`:": "+stage);
    bar.style.display="block";if(total){bar.max=total;bar.value=done}else{bar.removeAttribute("value")}
    dl.disabled=true;dl.textContent="SIDFlow download in progress…";rm.style.display="none";
    if(!SIDFLOWUI.poll)SIDFLOWUI.poll=setInterval(sidflowStatusLoad,1000);
    return;
  }
  if(SIDFLOWUI.poll){clearInterval(SIDFLOWUI.poll);SIDFLOWUI.poll=null}
  bar.style.display="none";bar.value=0;dl.disabled=false;
  if(s.available&&!s.needs_update){
    const release=s.release_tag||s.supported_release||"",hvsc=s.hvsc_version?` · ${s.hvsc_version}`:"";
    const model=s.similarity_metric?` · ${s.similarity_metric}`:"",dims=s.vector_dimensions?` · ${s.vector_dimensions}D`:"";
    const feature=s.feature_schema_version?` · features ${s.feature_schema_version}`:"";
    const neighbours=s.neighbors?` · ${Number(s.neighbors).toLocaleString()} neighbours`:"";
    const warning=s.quality_warning?` · ⚠ ${s.quality_warning}`:"";
    out.textContent=`Ready — SIDFlow ${release}${hvsc} · ${Number(s.tracks||0).toLocaleString()} tracks${neighbours}${model}${dims}${feature} · ${sidflowBytes(s.bytes)}${warning}`;
    dl.textContent="Re-download SIDFlow 0.8.0";rm.style.display="inline-block";
  }else if(s.available&&s.needs_update){
    const installed=s.release_tag||"legacy data";
    out.textContent=`Update required — ${installed} is installed; SIDFlow ${s.supported_release||"0.8.0"} is required for the new weighted neighbour model.`;
    dl.textContent=`Update to SIDFlow ${s.supported_release||"0.8.0"}`;rm.style.display="inline-block";
  }else{
    const issue=j.error||s.error||"";
    out.textContent=issue?`Not available — ${issue}`:"Not downloaded — More like this and Radio will offer to install it.";
    dl.textContent=s.bytes?"Re-download Similarity Data":"Download Similarity Data";rm.style.display=s.bytes?"inline-block":"none";
  }
}
async function sidflowStatusLoad(){
  try{const s=await api("/api/sidflow/status",{timeoutMs:5000});sidflowStatusRender(s);return s}catch(e){
    if($("#sidflowStatus"))$("#sidflowStatus").textContent="SIDFlow status unavailable: "+e.message;return null}
}
async function sidflowDownload(skipConfirm=false){
  if(!skipConfirm&&!confirm("Download the pinned SIDFlow 0.8.0 similarity export?\n\nThe download is about 194 MB. It expands temporarily to about 982 MB and needs roughly 1.8 GiB free while u64deck verifies, imports and safely promotes the new database. Your current working SIDFlow database is kept unless the entire update succeeds."))return false;
  try{const s=await api("/api/sidflow/download",{method:"POST",timeoutMs:10000});sidflowStatusRender(s);toast("SIDFlow similarity download started","ok");return true}
  catch(e){toast(e.message,"err");return false}
}
async function sidflowRemove(){
  if(!confirm("Remove the local SIDFlow similarity database?\n\nThe SID Jukebox and your HVSC files are not affected."))return;
  try{const s=await api("/api/sidflow",{method:"DELETE"});sidflowStatusRender(s);toast("SIDFlow similarity data removed","ok")}
  catch(e){toast(e.message,"err")}
}
async function sidflowEnsure(){
  const s=await sidflowStatusLoad();
  if(s?.available&&!s?.needs_update&&!s?.quality_warning)return true;
  if(s?.needs_update){
    if(confirm(`SIDFlow ${s.supported_release||"0.8.0"} is required for the improved weighted recommendations.\n\nUpdate it now?`))await sidflowDownload(true);
    tab("settings");return false
  }
  if(s?.quality_warning){toast(s.quality_warning,"err");tab("settings");return false}
  if(s?.job?.running){toast("SIDFlow similarity data is still being prepared","ok");tab("settings");return false}
  if(confirm("More like this and Radio use SIDFlow similarity data by Chris Gleissner.\n\nDownload and prepare it now?")){
    await sidflowDownload(true);tab("settings");
  }
  return false;
}
async function jkMoreLike(index=null){
  if(!await sidflowEnsure())return;
  const i=index==null?Number((JK.state||{}).index):Number(index);
  if(i<0){toast("Choose a tune first","err");return}
  try{const s=await api("/api/juke/more_like",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({index:i,limit:20}),timeoutMs:10000});jkRender(s);
    toast(`♪ ${s.added||0} SIDFlow matches inserted after the current tune — thanks Chris!`,"ok")}
  catch(e){toast(e.message,"err")}
}
async function jkRadioSet(){
  const box=$("#jkRadio"),on=!!box.checked;
  if(on&&!await sidflowEnsure()){box.checked=false;return}
  try{const s=await put("/api/juke/radio?on="+(on?"true":"false"),{timeoutMs:10000});jkRender(s);
    toast(on?"📻 SIDFlow Radio is on":"SIDFlow Radio is off","ok")}
  catch(e){box.checked=!on;toast(e.message,"err")}
}

/* ---------- quick launch library ---------- */
async function qlRefresh(){
  try{
    const r=await api("/api/library");
    const el=$("#qlbtns");
    if(!r.files.length){el.innerHTML='<span class="hint">empty — add files below</span>';return}
    el.innerHTML=r.files.map(f=>{
      const label=esc(f.name.replace(/\.[^.]+$/,""));
      const fav=itemSpec("library",f.name,f.name,"library_run",{name:f.name});
      return `<span class="row" style="gap:2px"><button class="primary" onclick="qlRun('${jsq(f.name)}')"
        oncontextmenu="event.preventDefault();qlDel('${jsq(f.name)}')"
        title="${esc(f.name)} — right-click to remove">▶ ${label}</button>${starButton(fav)}</span>`}).join("");
  }catch(e){}
}
async function qlRun(name){
  toast("Launching "+name+"…","ok");
  try{await put("/api/library/run?name="+encodeURIComponent(name));
    toast(name+" running ✓","ok");rememberRecent(itemSpec("library",name,name,"library_run",{name}));screenEl.focus()}
  catch(e){toast(e.message,"err")}
}
async function qlAdd(){
  const f=$("#qlfile").files[0];if(!f){toast("Choose a file first","err");return}
  const fd=new FormData();fd.append("file",f);
  try{await api("/api/library/upload",{method:"POST",body:fd});
    $("#qlfile").value="";qlRefresh();toast("Added to library ✓","ok")}
  catch(e){toast(e.message,"err")}
}
async function qlDel(name){
  if(!confirm("Remove "+name+" from the library?"))return;
  try{await api("/api/library/delete?name="+encodeURIComponent(name),{method:"POST"});qlRefresh()}
  catch(e){toast(e.message,"err")}
}

/* ---------- row selection highlight ---------- */
for(const id of ["fslist","inspector"]){
  const el=document.getElementById(id);
  if(el)el.addEventListener("click",e=>{
    const tr=e.target.closest("tr");
    if(!tr||!el.contains(tr)||!tr.closest("tbody"))return;
    tr.closest("tbody").querySelectorAll("tr.sel").forEach(t=>t.classList.remove("sel"));
    tr.classList.add("sel")});
}

/* ---------- boot ---------- */
loadQuality();loadTransport();loadIfaces();loadBootOptions();loadMountOptions();loadRecSettings();setAudioState("off",0);clearVideoCanvas();itemsLoad();qlRefresh();idxPollStart();
renderDrivePanel("Waiting for the Ultimate connection before reading drive status…");
loadInfo();setInterval(loadInfo,30000);
