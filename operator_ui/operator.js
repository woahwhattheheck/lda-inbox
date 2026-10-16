"use strict";
const $ = id => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
let token = fragment.get("token") || "";
try {
  if (token) sessionStorage.setItem("lda-operator-token", token);
  else token = sessionStorage.getItem("lda-operator-token") || "";
} catch (_) { /* API will require the current launcher URL when storage is unavailable. */ }
history.replaceState(null, "", location.pathname);
let state = null, selected = null, busy = false, pending = null, historyBefore = null;
let workspaceEpoch = 0;
const pendingKey = () => `lda-operator-pending:${state.scope}`;
const message = (text, error=false) => { $("notice").textContent = text; $("notice").classList.toggle("error",error); };
const node = (tag,text,className="") => { const e=document.createElement(tag); e.textContent=text; e.className=className; return e; };
const stamp = text => text ? new Date(text).toLocaleString() : "Not set";
const short = (text,n=150) => text.length>n ? text.slice(0,n)+"…" : text;

async function api(path, body) {
  let response;
  try {
    response = await fetch(path, {method:body ? "POST":"GET", cache:"no-store",
      headers:{"X-Operator-Token":token, ...(body ? {"Content-Type":"application/json"}:{})},
      ...(body ? {body:JSON.stringify(body)}:{})});
  } catch (_) { throw new Error("Connection interrupted. Reconnect, then recover the retained request."); }
  let value;
  try { value=await response.json(); } catch (_) { throw new Error("Unreadable response. Recover the same request after reconnecting."); }
  if(!response.ok) { const e=new Error(value.error || "Request failed"); e.definite=[400,409].includes(response.status); throw e; }
  return value;
}
function status(task) {
  if(task.done) return "completed";
  const execution=task.execution;
  if(!execution || execution.state==="available") return "available";
  const now=Date.parse(state.as_of)+(Date.now()-state.loadedAt);
  if(Date.parse(execution.claimed_at)>now) return "future";
  return Date.parse(execution.lease_expires_at)<=now ? "expired":"leased";
}
function controls() {
  const disabled=busy||!!pending;
  for(const id of ["workspace","refresh","import","search","filter"]) $(id).disabled=busy;
  $("operation-fields").disabled=disabled;
  $("import").disabled=disabled;
  $("recover").disabled=busy;
  $("pending").hidden=!pending;
  if(pending) $("pending-note").textContent=`${pending.body.action || "import"} / ${pending.body.request_id}. The exact request is retained in this browser tab.`;
  $("download").disabled=busy||!state?.document;
  $("original").disabled=busy||!state?.document;
}
function actionFields() {
  const action=$("action").value;
  $("seconds-label").hidden=!["claim","renew"].includes(action);
  $("seconds").required=["claim","renew"].includes(action);
  $("reason-label").hidden=action!=="release";
  $("completion-fields").hidden=action!=="complete";
  $("completion-id").required=action==="complete";
}
function renderTasks() {
  const tasks=state?.document?.tasks||[], counts={available:0,leased:0,completed:0};
  tasks.forEach(t=>{const s=status(t);counts[s]=(counts[s]||0)+1;});
  $("count-all").textContent=tasks.length;
  $("count-ready").textContent=counts.available;
  $("count-live").textContent=counts.leased;
  $("count-done").textContent=counts.completed;
  const search=$("search").value.toLowerCase(), filter=$("filter").value;
  const visible=tasks.filter(t=>(filter==="all"||status(t)===filter)&&
    `${t.id} ${t.command} ${t.execution?.worker_id||""}`.toLowerCase().includes(search));
  $("tasks").replaceChildren();
  visible.forEach(task=>{
    const button=node("button","","task"); button.type="button";
    button.setAttribute("aria-pressed",String(selected===task.id));
    button.append(node("strong",task.id),node("span",`${status(task)} · ${task.execution?.worker_id||"Unassigned"} · generation ${task.execution?.attempt||0}`,"task-sub"),node("span",short(task.command),"task-command"));
    button.addEventListener("click",()=>{selected=task.id;renderTasks();renderDetail();});
    $("tasks").append(button);
  });
  $("empty").hidden=visible.length>0;
  $("empty").textContent=!state?.document ? "Open an inbox JSON file to begin." : tasks.length ? "No tasks match these filters." : "This inbox contains no tasks.";
}
function renderDetail() {
  const task=state?.document?.tasks.find(t=>t.id===selected);
  $("task-detail").hidden=!task;
  if(!task){$("task-title").textContent="Select a task";$("task-state").textContent="Its specification and current ownership appear here.";return;}
  $("task-title").textContent=task.id;
  $("task-state").textContent=`${status(task)} · ${task.timeout_s} second task limit`;
  $("command").textContent=task.command;
  $("task-json").textContent=JSON.stringify(task,null,2);
  $("ownership").replaceChildren();
  const ex=task.execution||{};
  for(const [name,value] of [["Worker",ex.worker_id||"Unassigned"],["Lease",ex.lease_id||"Unassigned"],["Generation",ex.attempt||0],["Claimed",stamp(ex.claimed_at)],["Expires",stamp(ex.lease_expires_at)],["Completed",stamp(task.completed_at)]])
    $("ownership").append(node("dt",name),node("dd",String(value)));
  $("seconds").max=Math.min(3600,task.timeout_s);
  controls();
}
async function loadHistory(append=false) {
  if(!state?.workspace){$("history").replaceChildren();$("older").hidden=true;return;}
  const epoch=workspaceEpoch, workspace=state.workspace;
  const value=await api(`/api/history?workspace=${workspace}${append&&historyBefore ? `&before=${historyBefore}`:""}`);
  if(epoch!==workspaceEpoch)return;
  if(!append)$("history").replaceChildren();
  value.editions.forEach(item=>{
    const row=node("div","","edition"), description=node("div","");
    description.append(node("strong",`Edition ${item.revision} · ${item.action}`),node("span",stamp(item.created)),node("br",""),node("code",item.digest));
    const button=node("button",`Download ${item.revision}`);
    button.addEventListener("click",()=>download(workspace,item.revision));
    row.append(description,button);$("history").append(row);
  });
  historyBefore=value.before;$("older").hidden=!historyBefore;
}
async function refresh(workspace=state?.workspace) {
  const epoch=++workspaceEpoch;
  const value=await api(`/api/state${workspace ? `?workspace=${workspace}`:""}`);
  if(epoch!==workspaceEpoch)return;
  state={...value,loadedAt:Date.now()};
  $("workspace").replaceChildren();
  state.workspaces.forEach(w=>{const option=node("option",w.label);option.value=w.id;$("workspace").append(option);});
  $("workspace").value=state.workspace||"";
  if(!state.document?.tasks.some(t=>t.id===selected))selected=state.document?.tasks[0]?.id||null;
  if(!pending){try {pending=JSON.parse(sessionStorage.getItem(pendingKey())||"null");}catch(_){message("Browser request storage is unavailable. Reads and downloads remain available.",true);}}
  $("revision").textContent=state.document ? `Edition ${state.revision}`:"";
  $("clock").textContent=`Host clock at refresh: ${stamp(state.as_of)}`;
  $("digest").textContent=state.digest ? `Document SHA-256 ${state.digest}`:"";
  renderTasks();renderDetail();controls();await loadHistory();
}
async function dispatch(path,body,recovery=false) {
  if(busy||(!recovery&&pending))return;
  if(!recovery){
    pending={path,body};
    try{sessionStorage.setItem(pendingKey(),JSON.stringify(pending));}
    catch(_){pending=null;message("The request could not be retained in this tab. No operation was sent.",true);return;}
  }
  busy=true;controls();message(recovery ? "Recovering the exact retained request…":"Saving the local edition…");
  try{
    const result=await api(pending.path,pending.body);
    sessionStorage.removeItem(pendingKey());pending=null;
    await refresh(result.workspace);
    message(`${result.action} retained at edition ${result.revision}. The current workspace is open.`);
  }catch(error){
    if(error.definite){try{sessionStorage.removeItem(pendingKey());}catch(_){}pending=null;}
    message(error.message,true);
  }finally{busy=false;controls();}
}
async function download(workspace,revision=null,original=false){
  try{
    const response=await fetch(`/api/export?workspace=${workspace}${revision ? `&revision=${revision}`:""}${original ? "&original=1":""}`,{headers:{"X-Operator-Token":token},cache:"no-store"});
    if(!response.ok){const value=await response.json();throw new Error(value.error);}
    const blob=await response.blob(), url=URL.createObjectURL(blob), anchor=document.createElement("a");
    anchor.href=url;anchor.download=`inbox-${workspace.slice(0,8)}-${original ? "original":revision||"current"}.json`;
    anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    message("Complete inbox JSON downloaded.");
  }catch(error){message(error.message,true);}
}
$("operation").addEventListener("submit",event=>{
  event.preventDefault();if(!state?.document||!selected)return;
  const task=state.document.tasks.find(t=>t.id===selected),action=$("action").value;
  const body={request_id:crypto.randomUUID(),workspace:state.workspace,action,task_id:selected,
    worker_id:$("worker").value,lease_id:$("lease").value,expected_document_sha256:state.digest};
  if(["claim","renew"].includes(action))body.lease_seconds=Number($("seconds").value);
  if(action!=="claim")body.expected_attempt=task.execution?.attempt||0;
  if(action==="release")body.reason=$("reason").value||null;
  if(action==="complete"){body.completion_id=$("completion-id").value;body.result=$("result").value;}
  dispatch("/api/transition",body);
});
$("import").addEventListener("click",async()=>{
  if(busy||pending)return;
  const file=$("import-file").files[0];if(!file){message("Choose an inbox JSON file.",true);return;}
  if(file.size>1048576){message("Inbox JSON exceeds the 1 MiB contract limit.",true);return;}
  busy=true;controls();
  try{const text=await file.text();busy=false;await dispatch("/api/import",{request_id:crypto.randomUUID(),label:$("import-name").value.trim()||file.name,source_text:text});}
  catch(error){message(error.message,true);}
  finally{busy=false;controls();}
});
$("recover").addEventListener("click",()=>pending&&dispatch(pending.path,pending.body,true));
$("refresh").addEventListener("click",()=>refresh().then(()=>message("Current edition refreshed.")).catch(e=>message(e.message,true)));
$("workspace").addEventListener("change",()=>refresh($("workspace").value).catch(e=>message(e.message,true)));
$("search").addEventListener("input",renderTasks);$("filter").addEventListener("change",renderTasks);
$("action").addEventListener("change",actionFields);
$("download").addEventListener("click",()=>download(state.workspace));
$("original").addEventListener("click",()=>download(state.workspace,null,true));
$("older").addEventListener("click",()=>loadHistory(true).catch(e=>message(e.message,true)));
actionFields();
refresh().then(()=>message(pending ? "Recover the retained operation before staging another." : "Workspace ready. Import an inbox or select a retained task.")).catch(e=>message(e.message,true));
