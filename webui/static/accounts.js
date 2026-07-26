const { api, appUrl, copyText, escapeHtml, fmtTime, initIcons, setToast } = window.Workbench;
const $ = selector => document.querySelector(selector);
const state = {
  page: 1,
  pageSize: 20,
  total: 0,
  filter: "all",
  rows: [],
  selected: new Set(),
  taskTimer: null,
  activeRtEmails: new Set(),
  modalData: null,
};

function totalPages() {
  return Math.max(1, Math.ceil(state.total / state.pageSize));
}

function selectedEmails() {
  return [...state.selected];
}

function updateSelection() {
  const count = state.selected.size;
  $("#selectedCount").textContent = count;
  for (const id of ["copyAtBtn", "copyEmailBtn", "statusSelectedBtn"]) {
    $(`#${id}`).disabled = count === 0;
  }
  $("#acquireRtBtn").disabled = count === 0
    || selectedEmails().some(email => state.activeRtEmails.has(email));
  const selectedWithRt = state.rows.some(row => state.selected.has(row.email) && row.rt_len > 0);
  $("#downloadBtn").disabled = !selectedWithRt;
  const visible = state.rows.map(row => row.email);
  $("#selectAll").checked = visible.length > 0 && visible.every(email => state.selected.has(email));
  $("#selectAll").indeterminate = visible.some(email => state.selected.has(email)) && !$("#selectAll").checked;
}

function plusBadge(info) {
  const status = info?.status || "unknown";
  const label = info?.label || "未检查";
  return `<span class="badge ${escapeHtml(status)}" title="${escapeHtml(label)}">${escapeHtml(label)}</span>`;
}

function tokenButton(row, field, length) {
  const short = { access_token: "AT", session_token: "ST", refresh_token: "RT" }[field];
  if (!length) return `<button class="token-btn missing" data-action="acquire" data-email="${escapeHtml(row.email)}" data-field="${field}">${field === "refresh_token" ? "获取" : "—"} ${short}</button>`;
  return `<button class="token-btn present" data-action="copy-token" data-email="${escapeHtml(row.email)}" data-field="${field}" title="复制 ${short}">${short} · ${length}</button>`;
}

function renderRows() {
  const body = $("#accountsTable tbody");
  if (!state.rows.length) {
    body.innerHTML = `<tr><td class="empty" colspan="8"><i data-lucide="database-zap"></i> 当前筛选下暂无账号</td></tr>`;
    initIcons();
    return;
  }
  body.innerHTML = state.rows.map(row => {
    const checkedAt = row.plus_check?.checked_at;
    const rtBusy = state.activeRtEmails.has(row.email);
    return `<tr>
      <td class="check-cell"><input class="row-check" type="checkbox" data-email="${escapeHtml(row.email)}" ${state.selected.has(row.email) ? "checked" : ""}></td>
      <td><span class="truncate email" title="${escapeHtml(row.email)}">${escapeHtml(row.email)}</span><span class="subline">录入 ${fmtTime(row.created_at)}</span></td>
      <td>${plusBadge(row.plus_check)}</td>
      <td>${tokenButton(row, "access_token", row.at_len)}</td>
      <td>${tokenButton(row, "session_token", row.st_len)}</td>
      <td>${tokenButton(row, "refresh_token", row.rt_len)}</td>
      <td><span class="truncate">${checkedAt ? fmtTime(checkedAt) : "未检查"}</span></td>
      <td><div class="actions">
        <button class="icon-action" data-action="view" data-email="${escapeHtml(row.email)}" title="查看凭证"><i data-lucide="eye"></i></button>
        <button class="icon-action" data-action="email" data-email="${escapeHtml(row.email)}" title="获取原始邮箱四段"><i data-lucide="mail"></i></button>
        <button class="text-action" data-action="status" data-email="${escapeHtml(row.email)}" title="实时刷新账号状态">状态</button>
        <button class="text-action" data-action="acquire-rt" data-email="${escapeHtml(row.email)}" title="${rtBusy ? "RT 获取任务进行中" : "重新 OTP 登录获取 RT"}" ${rtBusy ? "disabled" : ""}>${rtBusy ? "处理中" : "取 RT"}</button>
        <button class="icon-action" data-action="download" data-email="${escapeHtml(row.email)}" title="下载 Sub2API JSON" ${row.rt_len ? "" : "disabled"}><i data-lucide="download"></i></button>
        <button class="icon-action danger" data-action="delete" data-email="${escapeHtml(row.email)}" title="删除账号"><i data-lucide="trash-2"></i></button>
      </div></td>
    </tr>`;
  }).join("");
  initIcons();
}

async function refresh(reset = false) {
  if (reset) state.page = 1;
  const offset = (state.page - 1) * state.pageSize;
  try {
    const result = await api(`api/registered?limit=${state.pageSize}&offset=${offset}&filter=${encodeURIComponent(state.filter)}`);
    state.rows = result.items;
    state.total = result.total;
    if (state.page > totalPages()) {
      state.page = totalPages();
      return refresh(false);
    }
    renderRows();
    $("#pageInfo").textContent = `第 ${state.page} / ${totalPages()} 页 · ${state.total} 条`;
    $("#prevBtn").disabled = state.page <= 1;
    $("#nextBtn").disabled = state.page >= totalPages();
    $("#metricTotal").textContent = result.summary?.total ?? result.total;
    $("#metricRt").textContent = result.summary?.has_rt ?? "—";
    $("#metricPlus").textContent = result.summary?.plus ?? "—";
    $("#metricFree").textContent = result.summary?.free ?? "—";
    $("#metricIssues").textContent = result.summary?.issues ?? "—";
    $("#totalNote").textContent = result.summary?.total ?? result.total;
    $("#updatedAt").textContent = `列表刷新 ${new Date().toLocaleTimeString("zh-CN", {hour:"2-digit", minute:"2-digit"})}`;
    $("#serverState").textContent = "服务正常";
    updateSelection();
  } catch (error) {
    $("#serverState").textContent = "服务异常";
    setToast(`刷新失败：${error.message}`, "bad");
  }
}

async function getCredential(email) {
  const { data } = await api(`api/registered/${encodeURIComponent(email)}`);
  return data;
}

async function copyTokens(emails) {
  const values = [];
  let skipped = 0;
  for (const email of emails) {
    const data = await getCredential(email);
    if (data.access_token) values.push(data.access_token);
    else skipped += 1;
  }
  if (!values.length) throw new Error("所选账号都没有 AT");
  await copyText(values.join("\n"));
  setToast(`已复制 ${values.length} 个 AT${skipped ? `，跳过 ${skipped} 个` : ""}`, "ok");
}

async function copySourceEmails(emails) {
  const values = [];
  const failed = [];
  for (const email of emails) {
    try {
      const result = await api(`api/account-management/email/${encodeURIComponent(email)}`);
      values.push(result.raw);
    } catch (_) {
      failed.push(email);
    }
  }
  if (!values.length) throw new Error("号池中没有所选账号的原始四段邮箱");
  await copyText(values.join("\n"));
  setToast(`已复制 ${values.length} 条邮箱四段${failed.length ? `，${failed.length} 条未找到` : ""}`, "ok");
}

function showCredential(email, data) {
  state.modalData = data;
  $("#modalTitle").textContent = email;
  const fields = [
    ["email", "邮箱"], ["access_token", "Access Token"], ["session_token", "Session Token"],
    ["refresh_token", "Refresh Token"], ["id_token", "ID Token"],
    ["device_id", "Device ID"], ["cookie_header", "Cookie"],
  ];
  $("#modalBody").innerHTML = fields.map(([key, label]) => `
    <div class="field-row">
      <label>${label}</label>
      <span class="field-value" title="${escapeHtml(data[key] || "")}">${escapeHtml(data[key] || "—")}</span>
      <button class="icon-action" data-modal-copy="${key}" ${data[key] ? "" : "disabled"}><i data-lucide="copy"></i></button>
    </div>`).join("");
  $("#credentialModal").classList.remove("hidden");
  initIcons();
}

async function startTask(path, emails, label) {
  const result = await api(path, {
    method: "POST",
    body: JSON.stringify({
      emails,
      proxy: $("#proxyInput").value.trim(),
      otp_timeout: 180,
    }),
  });
  const isRtTask = path.includes("acquire-rt");
  if (isRtTask) {
    emails.forEach(email => state.activeRtEmails.add(email));
    renderRows();
    updateSelection();
    if (result.reused) setToast("该账号已有获取 RT 任务，已继续查看原任务", "ok");
  }
  watchTask(result.task_id, label, isRtTask ? emails : []);
}

function renderTaskEvents(events = []) {
  const visible = events.slice(-8);
  $("#taskEvents").innerHTML = visible.map(event => `
    <div class="task-event ${escapeHtml(event.status || "running")}">
      <span class="task-event-dot"></span>
      <span class="task-event-label">${escapeHtml(event.label || event.phase || "处理")}</span>
      <span class="task-event-detail">
        ${escapeHtml(event.detail || "—")}
        ${event.code ? `<code class="task-event-code">${escapeHtml(event.code)}</code>` : ""}
      </span>
    </div>
  `).join("");
}

function watchTask(taskId, label, rtEmails = []) {
  clearTimeout(state.taskTimer);
  $("#taskDrawer").classList.remove("hidden");
  $("#taskTitle").textContent = label;
  $("#taskState").textContent = "等待";
  $("#taskState").className = "task-state";
  $("#taskPhase").textContent = "等待执行";
  $("#taskMeta").textContent = `0 / ${rtEmails.length || "—"}`;
  $("#taskDetail").textContent = "任务正在排队";
  $("#taskActionRequired").textContent = "";
  $("#taskActionRequired").classList.add("hidden");
  $("#taskEvents").innerHTML = "";
  $("#taskProgress").style.width = "0%";
  $("#taskDownloadBtn").hidden = true;
  $("#taskDownloadBtn").dataset.taskId = "";
  const poll = async () => {
    try {
      const task = await api(`api/account-tasks/${taskId}`);
      const pct = task.total ? Math.round(task.completed / task.total * 100) : 100;
      $("#taskProgress").style.width = `${pct}%`;
      $("#taskPhase").textContent = task.phase_label || "处理中";
      $("#taskMeta").textContent = `${task.completed} / ${task.total}`;
      $("#taskDetail").textContent = task.phase_detail || task.message || "处理中";
      $("#taskState").textContent = {
        queued: "等待", running: "运行中", done: "完成", partial: "部分完成", failed: "失败",
      }[task.state] || task.state;
      $("#taskState").className = `task-state ${task.state === "done" ? "ok" : ["failed", "partial"].includes(task.state) ? "bad" : ""}`;
      const actionBox = $("#taskActionRequired");
      actionBox.textContent = task.action_required || "";
      actionBox.classList.toggle("hidden", !task.action_required);
      renderTaskEvents(task.events);
      if (task.download_ready) {
        $("#taskDownloadBtn").hidden = false;
        $("#taskDownloadBtn").dataset.taskId = taskId;
      }
      if (["done", "partial", "failed"].includes(task.state)) {
        rtEmails.forEach(email => state.activeRtEmails.delete(email));
        const tone = task.succeeded ? "ok" : "bad";
        setToast(task.message, tone);
        await refresh(false);
        if (task.kind === "acquire_rt" && task.download_ready) {
          window.location.assign(appUrl(`api/account-tasks/${taskId}/download`));
          $("#taskDownloadBtn").hidden = true;
        }
        return;
      }
      state.taskTimer = setTimeout(poll, 900);
    } catch (error) {
      $("#taskMeta").textContent = `任务查询失败：${error.message}`;
    }
  };
  poll();
}

$("#filterSegment").addEventListener("click", event => {
  const button = event.target.closest("[data-filter]");
  if (!button) return;
  state.filter = button.dataset.filter;
  $("#filterSegment").querySelectorAll("button").forEach(item => item.classList.toggle("active", item === button));
  state.selected.clear();
  refresh(true);
});

$("#refreshBtn").addEventListener("click", () => refresh(false));
$("#pageSize").addEventListener("change", event => {
  state.pageSize = Number(event.target.value);
  state.selected.clear();
  refresh(true);
});
$("#prevBtn").addEventListener("click", () => { if (state.page > 1) { state.page -= 1; refresh(); } });
$("#nextBtn").addEventListener("click", () => { if (state.page < totalPages()) { state.page += 1; refresh(); } });
$("#selectAll").addEventListener("change", event => {
  for (const row of state.rows) {
    if (event.target.checked) state.selected.add(row.email);
    else state.selected.delete(row.email);
  }
  renderRows();
  updateSelection();
});

$("#accountsTable").addEventListener("change", event => {
  if (!event.target.matches(".row-check")) return;
  if (event.target.checked) state.selected.add(event.target.dataset.email);
  else state.selected.delete(event.target.dataset.email);
  updateSelection();
});

$("#accountsTable").addEventListener("click", async event => {
  const button = event.target.closest("button[data-action]");
  if (!button || button.disabled) return;
  const { action, email, field } = button.dataset;
  try {
    if (action === "copy-token") {
      const data = await getCredential(email);
      await copyText(data[field] || "");
      setToast(`${field.toUpperCase()} 已复制`, "ok");
    } else if (action === "view") {
      showCredential(email, await getCredential(email));
    } else if (action === "email") {
      await copySourceEmails([email]);
    } else if (action === "status") {
      await startTask("api/account-management/tasks/refresh-status", [email], "刷新账号状态");
    } else if (action === "acquire" || action === "acquire-rt") {
      await startTask("api/account-management/tasks/acquire-rt", [email], "重新 OTP 登录获取 RT");
    } else if (action === "download") {
      await startTask("api/account-management/tasks/sub2-export", [email], "生成 Sub2API JSON");
    } else if (action === "delete") {
      if (!confirm(`删除 ${email} 的注册凭证？`)) return;
      await api(`api/registered/${encodeURIComponent(email)}`, { method: "DELETE" });
      state.selected.delete(email);
      await refresh();
      setToast("账号已删除", "ok");
    }
  } catch (error) {
    setToast(error.message, "bad");
  }
});

$("#copyAtBtn").addEventListener("click", () => copyTokens(selectedEmails()).catch(error => setToast(error.message, "bad")));
$("#copyEmailBtn").addEventListener("click", () => copySourceEmails(selectedEmails()).catch(error => setToast(error.message, "bad")));
$("#acquireRtBtn").addEventListener("click", () => startTask("api/account-management/tasks/acquire-rt", selectedEmails(), "批量获取 RT").catch(error => setToast(error.message, "bad")));
$("#statusSelectedBtn").addEventListener("click", () => startTask("api/account-management/tasks/refresh-status", selectedEmails(), "刷新选中账号状态").catch(error => setToast(error.message, "bad")));
$("#statusPageBtn").addEventListener("click", () => startTask("api/account-management/tasks/refresh-status", state.rows.map(row => row.email), "刷新当前页状态").catch(error => setToast(error.message, "bad")));
$("#downloadBtn").addEventListener("click", () => startTask("api/account-management/tasks/sub2-export", selectedEmails(), "生成 Sub2API JSON").catch(error => setToast(error.message, "bad")));

$("#closeModalBtn").addEventListener("click", () => $("#credentialModal").classList.add("hidden"));
$("#credentialModal").addEventListener("click", event => {
  if (event.target === $("#credentialModal")) $("#credentialModal").classList.add("hidden");
});
$("#modalBody").addEventListener("click", event => {
  const button = event.target.closest("[data-modal-copy]");
  if (!button) return;
  copyText(state.modalData?.[button.dataset.modalCopy] || "").then(
    () => setToast("字段已复制", "ok"),
    error => setToast(error.message, "bad"),
  );
});
$("#copyJsonBtn").addEventListener("click", () => {
  copyText(JSON.stringify(state.modalData, null, 2)).then(
    () => setToast("凭证 JSON 已复制", "ok"),
    error => setToast(error.message, "bad"),
  );
});
$("#taskCloseBtn").addEventListener("click", () => $("#taskDrawer").classList.add("hidden"));
$("#taskDownloadBtn").addEventListener("click", event => {
  const taskId = event.currentTarget.dataset.taskId;
  if (!taskId) return;
  window.location.assign(appUrl(`api/account-tasks/${taskId}/download`));
  event.currentTarget.hidden = true;
});

initIcons();
refresh(true);
