"use strict";
const $ = id => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
let token = fragment.get("token") || "";
try {
  if (token) sessionStorage.setItem("lda-operator-token", token);
  else token = sessionStorage.getItem("lda-operator-token") || "";
} catch (_) { /* API will request the current launcher URL if storage is unavailable. */ }
history.replaceState(null, "", location.pathname);

let scope = "", draftKey = "", pendingKey = "", busy = false, valid = null, saveTimer = null;
let draft = {v:1,label:"",tasks:[]};
const uid = () => crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
const now = () => new Date().toISOString();
const message = (text,error=false) => { $("notice").textContent=text; $("notice").classList.toggle("error",error); };
const node = (tag,text,className="") => { const element=document.createElement(tag); element.textContent=text; element.className=className; return element; };
const slug = text => (text||"inbox").toLowerCase().replace(/[^a-z0-9._-]+/g,"-").replace(/^-+|-+$/g,"").slice(0,64)||"inbox";

async function api(path, body) {
  let response;
  try {
    response = await fetch(path,{method:body?"POST":"GET",cache:"no-store",
      headers:{"X-Operator-Token":token,...(body?{"Content-Type":"application/json"}:{})},
      ...(body?{body:JSON.stringify(body)}:{})});
  } catch (_) { throw new Error("Connection interrupted. Reopen the launcher, then recover the exact save."); }
  let value;
  try { value=await response.json(); } catch (_) { throw new Error("Unreadable response from the local authoring host."); }
  if(!response.ok){const error=new Error(value.error||"Request failed");error.definite=[400,409].includes(response.status);throw error;}
  return value;
}

function makeTask(index=0){
  const suffix=String(index+1).padStart(2,"0");
  return {id:`task-${suffix}`,created:now(),kind:"run_task",command:"",timeout_s:3600,done:false};
}
function loadDraft(){
  try {
    const saved=localStorage.getItem(draftKey);
    if(saved) draft=JSON.parse(saved);
  } catch (_) { message("Draft storage is unavailable. The page still works, but reload recovery is disabled.",true); }
  if(!draft||draft.v!==1||!Array.isArray(draft.tasks)) draft={v:1,label:"",tasks:[]};
  $("document-label").value=draft.label||"";
  $("workspace-label").value=draft.workspace_label||draft.label||"";
}
function persist(){
  draft.label=$("document-label").value.trim();
  draft.workspace_label=$("workspace-label").value.trim();
  try { localStorage.setItem(draftKey,JSON.stringify(draft)); $("draft-state").textContent="Unfinished draft retained in this browser"; }
  catch (_) { $("draft-state").textContent="Draft storage unavaillable"; }
}
function scheduleValidation(){
  clearTimeout(saveTimer);persist();valid=null;controls();
  $("validation").textContent="Checking";$("digest").textContent="";
  saveTimer=setTimeout(validate,180);
}
function controls(){
  const pending=readPending();
 for(const id of ["add","download","save","clear","open-file","workspace-label","document-label"]) $(id).disabled=busy;
  $("save").disabled=busy||!valid||!!pending;
  $("download").disabled=busy||!valid;
  $("recover").hidden=!pending;$("recover").disabled=busy;
  document.querySelectorAll(".task-card input,.task-card textarea,.task-card button").forEach(e=>e.disabled=busy);
}
function readPending(){try{return JSON.parse(sessionStorage.getItem(pendingKey)||"null");}catch(_){return null;}}
function setPending(value){try{if(value)sessionStorage.setItem(pendingKey,JSON.stringify(value));else sessionStorage.removeItem(pendingKey);return true;}catch(_){return false;}}

function field(labelText,input){const label=node("label",labelText);label.append(input);return label;}
function input(type,value){const element=document.createElement(type==="textarea"?"textarea":"input");if(type!=="textarea")element.type=type;element.value=value;return element;}
function render(){
  $("tasks").replaceChildren();$("empty").hidden=draft.tasks.length>0;
  draft.tasks.forEach((task,index)=>{
    const card=node("article","","task-card");
    const head=node("div","","task-head");head.append(node("strong",`Task ${index+1}`));
    const buttons=node("div","","row-actions");
    for(const [labelText,delta] of [["Up",-1],["Down",1]]){
      const button=node("button",labelText);button.type="button";button.disabled=(delta<0?index===0:index===draft.tasks.length-1)||busy;
      button.addEventListener("click",()=>{const other=index+delta;[draft.tasks[index],draft.tasks[other]]=[draft.tasks[other],draft.tasks[index]];render();scheduleValidation();});buttons.append(button);
    }
    const remove=node("button","Remove");remove.type="button";remove.addEventListener("click",()=>{draft.tasks.splice(index,1);render();scheduleValidation();});buttons.append(remove);head.append(buttons);card.append(head);
    const grid=node("div","","task-grid");
    const id=input("text",task.id||"");id.maxLength=128;id.pattern="[A-Za-z0-9][A-Za-z0-9._-]*";id.addEventListener("input",()=>{task.id=id.value;scheduleValidation();});
    const created=input("text",task.created||"");created.placeholder="2026-10-16T14:00:00Z";created.addEventListener("input",()=>{task.created=created.value;scheduleValidation();});
    const timeout=input("number",String(task.timeout_s??3600));timeout.min="1";timeout.max="86400";timeout.addEventListener("input",()=>{task.timeout_s=Number(timeout.value);scheduleValidation();});
    grid.append(field("Task ID",id),field("Created (ISO-8601)",created),field("Timeout seconds",timeout));card.append(grid);
    const command=input("textarea",task.command||"");command.rows=4;command.placeholder="Describe the complete task to run";command.addEventListener("input",()=>{task.command=command.value;scheduleValidation();});card.append(field("Command",command));
    const stamp=node("button","Use current time");stamp.type="button";stamp.addEventListener("click",()=>{task.created=now();created.value=task.created;scheduleValidation();});card.append(stamp);
    $("tasks").append(card);
  });
  $("task-count").textContent=String(draft.tasks.length);controls();
}
function documentValue(){return {v:1,label:$("document-label").value.trim(),tasks:draft.tasks.map(task=>({id:task.id,created:task.created,kind:"run_task",command:task.command,timeout_s:task.timeout_s,done:false}))};}
async function validate(){
  if(busy)return;
  try{
    valid=await api("/api/compose/validate",{document:documentValue()});
    $("validation").textContent="Valid";$("byte-count").textContent=String(valid.bytes);$("digest").textContent=`Document SHA-256 ${valid.digest}`;
    message(`${valid.task_count} pending task${valid.task_count===1?"":"s"} pass the maintained protocol.`);
  }catch(error){valid=null;$("validation").textContent="Needs attention";$("byte-count").textContent="0";$("digest").textContent="";message(error.message,true);}
  controls();
}
async function ensureValid(){await validate();if(!valid)throw new Error("Resolve the validation message before continuing");return valid;}
function downloadText(text,name){const blob=new Blob([text],{type:"application/json"}),url=URL.createObjectURL(blob),anchor=document.createElement("a");anchor.href=url;anchor.download=name;anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
async function saveWorkspace(recovery=false){
  if(busy)return;let pending=readPending();
  try{
    if(!recovery){
      const current=await ensureValid();pending={path:"/api/import",body:{request_id:uid(),label:$("workspace-label").value.trim()||draft.label||"Authored inbox",source_text:current.canonical_text}};
      if(!setPending(pending))throw new Error("The exact save request could not be retained. No workspace was created.");
    }
    busy=true;controls();message(recovery?"Recovering the exact retained workspace save…":"Saving the authored inbox as a retained workspace…");
    const result=await api(pending.path,pending.body);setPending(null);
    try{localStorage.removeItem(draftKey);}catch(_){}
    message(`Workspace retained at edition ${result.revision}. Opening the operator desk.`);
    location.href="/";
  }catch(error){if(error.definite)setPending(null);message(error.message,true);}finally{busy=false;controls();}
}

$("add").addEventListener("click",()=>{draft.tasks.push(makeTask(draft.tasks.length));render();scheduleValidation();});
$("document-label").addEventListener("input",scheduleValidation);$("workspace-label").addEventListener("input",persist);
$("download").addEventListener("click",async()=>{try{const current=await ensureValid();downloadText(current.canonical_text,`${slug(draft.label)}.json`);message("Canonical inbox JSON downloaded.");}catch(error){message(error.message,true);}});
$("save").addEventListener("click",()=>saveWorkspace(false));$("recover").addEventListener("click",()=>saveWorkspace(true));
$("clear").addEventListener("click",()=>{if(!confirm("Clear the unfinished local draft?"))return;draft={v:1,label:"",tasks:[]};$("document-label").value="";$("workspace-label").value="";setPending(null);render();scheduleValidation();});
$("open-file").addEventListener("change",async event=>{const file=event.target.files[0];if(!file)return;if(file.size>1048576){message("Inbox JSON exceeds the 1 MiB contract limit.",true);return;}try{const value=JSON.parse(await file.text());draft={v:value.v,label:value.label||file.name,tasks:value.tasks||[],workspace_label:value.label||file.name};$("document-label").value=draft.label;$("workspace-label").value=draft.workspace_label;render();scheduleValidation();}catch(error){message(`Unable to open inbox: ${error.message}`,true);}finally{event.target.value="";}});

(async()=>{
  try{const state=await api("/api/state");scope=state.scope;draftKey=`lda-inbox-compose:${scope}`;pendingKey=`lda-inbox-compose-pending:${scope}`;loadDraft();render();scheduleValidation();if(readPending())message("An exact workspace save is retained. Recover it before starting another save.");}
  catch(error){message(error.message,true);controls();}
})();
