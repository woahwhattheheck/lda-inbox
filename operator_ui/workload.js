"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const SCHEMA = "lda.inbox.workload.v1", SNAPSHOT = "lda.inbox.workload-snapshot.v1";
  const SELECTION = "lda.inbox.workload-selection.v1", MAX_FILE = 64 * 1024 * 1024;
  const fragment = new URLSearchParams(location.hash.slice(1));
  let token = fragment.get("token") || "";
  try {
    if (token) sessionStorage.setItem("lda-operator-token", token);
    else token = sessionStorage.getItem("lda-operator-token") || "";
  } catch (_) { /* The launcher fragment also works without browser storage. */ }
  try { history.replaceState(null, "", location.pathname); } catch (_) { /* Embedded offline readers can have an opaque origin. */ }
  let pages = [], rows = [], visible = [], next = null, liveScope = null;
  let mode = "empty", busy = false, epoch = 0, controller = null, shown = 50;
  let selectedKeys = null;
  const elements = (tag, text, cls = "") => {
    const value = document.createElement(tag); value.textContent = text; value.className = cls; return value;
  };
  const notice = (text, error = false) => {
    $("notice").textContent = text; $("notice").classList.toggle("error", error);
  };
  const timestamp = value => typeof value === "string" && /(?:Z|[+-]\d\d:\d\d)$/.test(value)
    ? Date.parse(value) : NaN;
  const date = value => Number.isFinite(timestamp(value)) ? new Date(value).toLocaleString() : "Not recorded";
  const duration = seconds => {
    if (!Number.isFinite(seconds)) return "Not recorded";
    const minutes = Math.floor(Math.abs(seconds) / 60);
    if (minutes < 1) return "under a minute";
    if (minutes < 60) return `${minutes} min`;
    if (minutes < 1440) return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
    return `${Math.floor(minutes / 1440)} d ${Math.floor(minutes % 1440 / 60)} h`;
  };
  const workspaceKey = (scope, workspace) => JSON.stringify([scope, workspace]);
  const taskKey = (scope, workspace, task) => JSON.stringify([scope, workspace, task]);

  function validatePage(value) {
    if (!value || value.schema !== SCHEMA || typeof value.scope !== "string" || !value.scope ||
        !Array.isArray(value.workspaces) || value.workspaces.length > 10 ||
        !Number.isSafeInteger(value.total_workspaces) || value.total_workspaces < 0 ||
        !Number.isFinite(timestamp(value.as_of)) ||
        (value.next !== null && (typeof value.next !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value.next)))) {
      throw new Error("This is not a readable workload observation page.");
    }
    for (const workspace of value.workspaces) {
      if (!workspace || typeof workspace.id !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(workspace.id) ||
          typeof workspace.label !== "string" || !Number.isSafeInteger(workspace.revision) || workspace.revision < 1 ||
          typeof workspace.digest !== "string" || !/^[a-f0-9]{64}$/.test(workspace.digest) ||
          !Number.isFinite(timestamp(workspace.as_of)) || !workspace.document || workspace.document.v !== 1 ||
          !Array.isArray(workspace.document.tasks) || workspace.document.tasks.length > 256) {
        throw new Error("A saved workspace is missing its identity, edition or complete inbox.");
      }
      const ids = new Set();
      for (const task of workspace.document.tasks) {
        if (!task || typeof task.id !== "string" || !task.id || ids.has(task.id) ||
            typeof task.command !== "string" || typeof task.done !== "boolean" ||
            (task.execution !== undefined && (!task.execution || typeof task.execution !== "object" || Array.isArray(task.execution)))) {
          throw new Error("A saved task record is unreadable or repeats an ID within one workspace.");
        }
        ids.add(task.id);
      }
    }
    return value;
  }

  function observe(task, asOf) {
    const now = timestamp(asOf), created = timestamp(task.created), ex = task.execution;
    if (task.done) return "completed";
    if (!Number.isFinite(created)) return "invalid";
    if (created > now) return "future";
    if (!ex) return "available";
    const claimed = timestamp(ex.claimed_at), expiry = timestamp(ex.lease_expires_at);
    if (!Number.isFinite(claimed) || !Number.isFinite(expiry) || expiry <= claimed) return "invalid";
    if (claimed > now) return "future";
    if (ex.state === "available") {
      const released = timestamp(ex.released_at);
      return !Number.isFinite(released) ? "invalid" : released > now ? "future" : "available";
    }
    if (ex.state !== "leased") return "invalid";
    return expiry <= now ? "expired" : "leased";
  }

  function derive(source) {
    const output = [], seen = new Set();
    for (const page of source) for (const workspace of page.workspaces) {
      const wk = workspaceKey(page.scope, workspace.id);
      if (seen.has(wk)) throw new Error("This observation repeats a workspace. Refresh from one current source.");
      seen.add(wk);
      for (const task of workspace.document.tasks) {
        const state = observe(task, workspace.as_of), ex = task.execution || {};
        const remaining = (timestamp(ex.lease_expires_at) - timestamp(workspace.as_of)) / 1000;
        output.push({key: taskKey(page.scope, workspace.id, task.id), wk, scope: page.scope, workspace, task,
          state, remaining, soon: state === "leased" && remaining <= 900,
          age: (timestamp(workspace.as_of) - timestamp(task.created)) / 1000,
          worker: state === "available" || !ex.worker_id ? "" : String(ex.worker_id)});
      }
    }
    return output;
  }

  function controls() {
    $("refresh").disabled = busy || !token;
    $("more").hidden = mode !== "live" || next === null;
    $("more").disabled = busy;
    $("cancel").hidden = !busy;
    $("open").disabled = busy;
    $("save").disabled = busy || !pages.length;
    $("selection").disabled = busy || !visible.length;
    $("operator").hidden = !token;
    $("operator").href = "/#" + new URLSearchParams({token});
  }

  function fillSelect(id, entries) {
    const element = $(id), previous = element.value;
    element.replaceChildren();
    for (const [value, text] of entries) {
      const option = elements("option", text); option.value = value; element.append(option);
    }
    element.value = entries.some(([value]) => value === previous) ? previous : "";
  }

  function rebuild() {
    rows = derive(pages);
    const workspaces = pages.flatMap(page => page.workspaces.map(w => [workspaceKey(page.scope, w.id), `${w.label} (${w.id.slice(0, 8)})`]));
    const workers = [...new Set(rows.map(row => row.worker).filter(Boolean))].sort();
    fillSelect("workspace", [["", "All loaded workspaces"], ...workspaces]);
    fillSelect("worker", [["", "All workers"], ["@unassigned", "Unassigned / released"], ...workers.map(value => [value, value])]);
    const scopes = new Map();
    for (const page of pages) scopes.set(page.scope, page.total_workspaces);
    const total = [...scopes.values()].reduce((a, b) => a + b, 0);
    const times = pages.map(page => timestamp(page.as_of));
    const first = times.length ? new Date(Math.min(...times)).toLocaleString() : "";
    const last = times.length ? new Date(Math.max(...times)).toLocaleString() : "";
    $("coverage").textContent = pages.length
      ? `${mode === "live" ? "Live reads" : "Saved observation"}: ${workspaces.length} of ${total} workspace records loaded. States at read time: ${first}${first !== last ? " to " + last : ""}. Refresh for current leases.${selectedKeys ? " A retained task selection is active; Reset filters reveals the complete loaded inboxes." : ""}`
      : "No workspaces loaded.";
    $("editions").replaceChildren();
    for (const page of pages) for (const w of page.workspaces) {
      const item = elements("div", "", "edition");
      item.append(elements("strong", w.label), elements("p", `Edition ${w.revision} · ${w.action} · updated ${date(w.updated)} · read ${date(w.as_of)}`),
        elements("code", `Scope ${page.scope} / workspace ${w.id}`), elements("code", `Document SHA-256 ${w.digest}`));
      $("editions").append(item);
    }
    shown = 50; render();
  }

  function render() {
    const base = selectedKeys ? rows.filter(row => selectedKeys.has(row.key)) : rows;
    const counts = {all: base.length, available: 0, expired: 0, leased: 0, soon: 0, completed: 0};
    for (const row of base) { if (row.state in counts) counts[row.state]++; if (row.soon) counts.soon++; }
    $("total").textContent = counts.all;
    for (const name of ["available", "expired", "leased", "soon", "completed"]) $(name).textContent = counts[name];
    const search = $("search").value.trim().toLowerCase(), state = $("state").value;
    const activeFilters = [search, state !== "all", $("workspace").value, $("worker").value].filter(Boolean).length;
    $("filter-summary").textContent = `Search and filters${activeFilters ? " · " + activeFilters + " active" : ""}`;
    visible = base.filter(row => (state === "all" || state === "soon" && row.soon || row.state === state) &&
      (!$("workspace").value || row.wk === $("workspace").value) &&
      (!$("worker").value || (row.worker || "@unassigned") === $("worker").value) &&
      `${row.task.id} ${row.task.command} ${row.workspace.label} ${row.task.execution?.worker_id || ""}`.toLowerCase().includes(search));
    const order = {expired: 0, available: 1, invalid: 2, leased: 3, future: 4, completed: 5};
    const finite = value => Number.isFinite(value) ? value : Number.MAX_SAFE_INTEGER;
    visible.sort((a, b) => {
      let result = 0;
      switch ($("sort").value) {
        case "oldest": result = finite(timestamp(a.task.created)) - finite(timestamp(b.task.created)); break;
        case "expiry": result = finite(["leased", "expired"].includes(a.state) ? a.remaining : NaN) - finite(["leased", "expired"].includes(b.state) ? b.remaining : NaN); break;
        case "recent": result = finite(timestamp(b.workspace.updated)) - finite(timestamp(a.workspace.updated)); break;
        case "name": result = a.task.id.localeCompare(b.task.id); break;
        default: result = order[a.state] - order[b.state] || finite(timestamp(a.task.created)) - finite(timestamp(b.task.created));
      }
      return result || a.key.localeCompare(b.key);
    });
    document.querySelectorAll("[data-state]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.state === state)));
    const duplicateIds = new Map();
    for (const row of rows) duplicateIds.set(row.task.id, (duplicateIds.get(row.task.id) || 0) + 1);
    const duplicates = [...duplicateIds.values()].filter(count => count > 1).length;
    $("duplicates").textContent = `${duplicates} task IDs occur in more than one loaded workspace. Each row keeps its own source workspace and edition.`;
    const workers = new Map();
    for (const row of base) {
      const name = row.worker || "@unassigned";
      if (!workers.has(name)) workers.set(name, {active: 0, expired: 0, other: 0});
      const count = workers.get(name);
      if (row.state === "leased") count.active++; else if (row.state === "expired") count.expired++; else count.other++;
    }
    $("workers").replaceChildren();
    for (const [name, count] of [...workers].sort((a, b) => b[1].active - a[1].active || a[0].localeCompare(b[0]))) {
      const button = elements("button", "", "worker");
      button.append(elements("strong", name === "@unassigned" ? "Unassigned / released" : name),
        elements("span", `${count.active} active · ${count.expired} expired · ${count.other} other`));
      button.addEventListener("click", () => { $("worker").value = name; shown = 50; render(); });
      $("workers").append(button);
    }
    $("tasks").replaceChildren();
    for (const row of visible.slice(0, shown)) {
      const card = elements("article", "", "task"), title = elements("div", ""), origin = elements("div", ""), timing = elements("div", ""), actions = elements("div", "", "actions");
      title.append(elements("h3", row.task.id), elements("p", row.task.command, "command"));
      if (duplicateIds.get(row.task.id) > 1) title.append(elements("small", `ID appears in ${duplicateIds.get(row.task.id)} workspaces`));
      origin.append(elements("span", row.state === "leased" ? "Active lease" : row.state === "invalid" ? "Unreadable timing" : row.state, "badge"),
        elements("strong", row.workspace.label), elements("small", `Edition ${row.workspace.revision} · ${row.workspace.id.slice(0, 8)}`),
        elements("p", row.worker || (row.task.execution?.worker_id ? `Released by ${row.task.execution.worker_id}` : "Unassigned")));
      timing.append(elements("span", "Created", "label"), elements("p", !Number.isFinite(row.age) ? "Not recorded" : row.age < 0 ? `In ${duration(row.age)}` : `${duration(row.age)} ago`));
      if (["leased", "expired"].includes(row.state)) {
        timing.append(elements("span", row.state === "expired" ? "Lease expired" : "Lease remaining", "label"), elements("p", duration(row.remaining)), elements("small", date(row.task.execution.lease_expires_at)));
      }
      if (mode === "live" && row.scope === liveScope && token) {
        const link = elements("a", "Open original task", "button");
        link.href = "/#" + new URLSearchParams({token, workspace: row.workspace.id, task: row.task.id}); actions.append(link);
      } else actions.append(elements("small", "Saved view. Refresh live workspaces to open current task actions."));
      const download = elements("button", "Download inbox");
      download.addEventListener("click", () => save(row.workspace.document, `inbox-${row.workspace.id}-edition-${row.workspace.revision}.json`));
      actions.append(download);
      const detail = elements("details", ""), summary = elements("summary", "Complete command and source");
      detail.append(summary, elements("pre", row.task.command), elements("p", `Read ${date(row.workspace.as_of)} · generation ${row.task.execution?.attempt || 0}`),
        elements("pre", JSON.stringify({scope: row.scope, workspace: row.workspace.id, revision: row.workspace.revision, document_sha256: row.workspace.digest, task: row.task}, null, 2)));
      card.append(title, origin, timing, actions, detail); $("tasks").append(card);
    }
    $("matched").textContent = `${visible.length} matching · ${Math.min(shown, visible.length)} shown`;
    $("empty").hidden = visible.length > 0;
    $("empty").textContent = !pages.length ? "Open a saved observation or refresh live workspaces to begin." : "No tasks match these filters.";
    $("show-more").hidden = visible.length <= shown;
    controls();
  }

  async function load(reset) {
    if (busy || !token) return;
    const operation = ++epoch;
    busy = true; controller = new AbortController(); controls();
    notice(reset ? "Reading current workspace editions…" : "Reading the next five workspaces…");
    try {
      const response = await fetch(`/api/workload${!reset && next ? "?after=" + encodeURIComponent(next) : ""}`, {
        cache: "no-store", headers: {"X-Operator-Token": token}, signal: controller.signal});
      let value;
      try { value = await response.json(); } catch (_) { throw new Error("The workload response was unreadable. The previous observation is retained."); }
      if (!response.ok) throw new Error(value.error || "Workspace read failed.");
      validatePage(value);
      if (operation !== epoch) return;
      if (!reset && value.scope !== liveScope) throw new Error("The connected database changed. Refresh all workspaces before continuing.");
      if (!reset && value.next && value.next <= next) throw new Error("The workspace continuation did not advance. Refresh all workspaces.");
      const proposed = reset ? [value] : [...pages, value];
      derive(proposed); // Check the whole replacement before changing the visible observation.
      pages = proposed; liveScope = value.scope; next = value.next; mode = "live"; selectedKeys = null;
      rebuild(); notice("Current page loaded. Each task action opens a refreshed source workspace.");
    } catch (error) {
      if (operation === epoch) notice(error.name === "AbortError" ? "Loading stopped. The completed observation is retained." : error.message, error.name !== "AbortError");
    } finally {
      if (operation === epoch) { busy = false; controller = null; controls(); }
    }
  }

  function save(value, name) {
    const blob = new Blob([JSON.stringify(value, null, 2) + "\n"], {type: "application/json"});
    const url = URL.createObjectURL(blob), anchor = document.createElement("a");
    anchor.href = url; anchor.download = name; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  $("refresh").addEventListener("click", () => load(true));
  $("more").addEventListener("click", () => load(false));
  $("cancel").addEventListener("click", () => controller?.abort());
  $("open").addEventListener("change", async event => {
    const file = event.target.files[0]; if (!file || busy) return;
    busy = true; controls(); notice("Opening the complete saved observation…");
    try {
      if (file.size > MAX_FILE) throw new Error("Choose an observation of at most 64 MiB.");
      let input;
      try { input = JSON.parse(await file.text()); }
      catch (_) { throw new Error("The selected file is not readable JSON. Choose a saved workload observation."); }
      const proposed = input?.schema === SCHEMA ? [input] : [SNAPSHOT, SELECTION].includes(input?.schema) ? input.pages : null;
      if (!Array.isArray(proposed) || !proposed.length || proposed.length > 1000) throw new Error("Choose a workload snapshot or filtered selection JSON file.");
      proposed.forEach(validatePage); const proposedRows = derive(proposed);
      let retained = null;
      if (input.schema === SELECTION) {
        if (!Array.isArray(input.selection) || input.selection.length > proposedRows.length) throw new Error("The saved selection is unreadable.");
        retained = new Set(input.selection); const available = new Set(proposedRows.map(row => row.key));
        if ([...retained].some(key => typeof key !== "string" || !available.has(key))) throw new Error("The saved selection refers to a task outside its retained source.");
      }
      pages = proposed; mode = "saved"; next = null; liveScope = null; selectedKeys = retained;
      for (const id of ["search", "workspace", "worker"]) $(id).value = "";
      $("state").value = "all"; $("sort").value = "attention";
      rebuild(); notice("Saved observation opened. Complete inboxes and their source identities are retained.");
    } catch (error) { notice(error.message + " The previous observation is unchanged.", true); }
    finally { busy = false; event.target.value = ""; controls(); }
  });
  $("save").addEventListener("click", () => {
    save({schema: SNAPSHOT, created: new Date().toISOString(), pages}, "Inbox_Workload_Observation.json");
    notice("Saved all loaded workspace documents. The coverage line shows whether more workspaces remain on the host.");
  });
  $("selection").addEventListener("click", () => {
    const keys = new Set(visible.map(row => row.wk));
    const source = pages.map(page => ({...page, workspaces: page.workspaces.filter(w => keys.has(workspaceKey(page.scope, w.id)))})).filter(page => page.workspaces.length);
    save({schema: SELECTION, created: new Date().toISOString(), pages: source, selection: visible.map(row => row.key)}, "Inbox_Workload_Selection.json");
    notice(`Saved ${visible.length} selected task records with their complete source inboxes. Open the selection here to continue.`);
  });
  for (const id of ["search", "state", "workspace", "worker", "sort"]) $(id).addEventListener(id === "search" ? "input" : "change", () => { shown = 50; render(); });
  $("reset").addEventListener("click", () => {
    for (const id of ["search", "workspace", "worker"]) $(id).value = "";
    $("state").value = "all"; $("sort").value = "attention"; selectedKeys = null; rebuild();
  });
  document.querySelectorAll("[data-state]").forEach(button => button.addEventListener("click", () => { $("state").value = button.dataset.state; shown = 50; render(); }));
  $("show-more").addEventListener("click", () => { shown += 50; render(); });
  if (window.matchMedia("(max-width: 600px)").matches) $("filter-panel").open = false;
  controls();
  if (token) load(true);
})();
