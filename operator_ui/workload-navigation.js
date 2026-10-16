"use strict";
// Preserve incoming selection before the maintained operator clears its fragment.
const workloadTarget = new URLSearchParams(location.hash.slice(1));
document.addEventListener("DOMContentLoaded", () => {
  const link = document.createElement("a");
  link.textContent = "Workload overview";
  link.href = "/workload#" + new URLSearchParams({token});
  link.style.cssText = "display:inline-block;margin:0 0 16px;color:inherit;font-weight:600";
  document.querySelector("main").prepend(link);
  const workspace = workloadTarget.get("workspace"), task = workloadTarget.get("task");
  if (workspace) {
    if (task) selected = task;
    refresh(workspace).then(() => {
      if (task && !state.document?.tasks.some(item => item.id === task)) {
        message("The selected task is absent from the current edition. Review this workspace.", true);
      } else {
        message(pending ? "Recover the retained operation before staging another." : "The current source edition is open.");
      }
    }).catch(error => message(error.message, true));
  }
});
