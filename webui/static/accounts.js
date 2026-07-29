const { api, appUrl, copyText, escapeHtml, fmtTime, initIcons, setToast } = window.Workbench;
const $ = selector => document.querySelector(selector);
// Stripe 托管付款链接有效期有限，超过这个时长就当过期，点击按钮重新生成。
const LINK_TTL_MS = 24 * 3600 * 1000;
const LINK_LABELS = { upi: "UPI", kakao: "Kakao" };
const state = {
  page: 1,
  pageSize: 20,
  total: 0,
  filter: "all",
  search: "",
  rows: [],
  selected: new Set(),
  taskTimers: new Map(),
  visibleTaskId: "",
  activeRtEmails: new Set(),
  active2faEmails: new Set(),
  activeLinkEmails: new Set(),
  refreshing: false,
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
  for (const id of ["deleteSelectedBtn", "copyAtBtn", "copyEmailBtn", "copy2faBtn", "statusSelectedBtn"]) {
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

function usedBadge(row) {
  return row.used_at
    ? `<span class="badge used" title="${escapeHtml(fmtTime(row.used_at))}">已使用</span>`
    : `<span class="badge unused">未使用</span>`;
}

function tokenButton(row, field, length) {
  const short = { access_token: "AT", session_token: "ST", refresh_token: "RT" }[field];
  if (!length) return `<button class="token-btn missing" data-action="acquire" data-email="${escapeHtml(row.email)}" data-field="${field}">${field === "refresh_token" ? "获取" : "—"} ${short}</button>`;
  return `<button class="token-btn present" data-action="copy-token" data-email="${escapeHtml(row.email)}" data-field="${field}" title="复制 ${short}">${short} · ${length}</button>`;
}

function linkButton(row, method) {
  const label = LINK_LABELS[method];
  const email = escapeHtml(row.email);
  const refreshButton = (disabled = false) => `
    <button class="icon-action link-refresh" data-action="gen-link" data-method="${method}" data-email="${email}"
      title="重新获取 ${label} 链接" ${disabled ? "disabled" : ""}>
      <i data-lucide="refresh-cw"></i>
    </button>`;
  if (state.activeLinkEmails.has(`${method}:${row.email}`)) {
    return `<span class="link-actions"><button class="text-action" disabled>${label}…</button>${refreshButton(true)}</span>`;
  }
  const info = row.links?.[method];
  const fresh = info?.link && (Date.now() - Number(info.at) * 1000 < LINK_TTL_MS);
  if (fresh) {
    // 生成后不占额外空间：链接放 title 悬浮显示，点击即复制。
    return `<span class="link-actions"><button class="text-action bound" data-action="copy-link" data-method="${method}" data-email="${email}" title="${escapeHtml(info.link)}&#10;点击复制">${label} ✓</button>${refreshButton()}</span>`;
  }
  const tip = info?.link ? `${label} 链接已过期，点击重新生成` : `点击生成 ${label} 付款链接`;
  const text = info?.link ? `${label} 过期` : label;
  return `<span class="link-actions"><button class="text-action${info?.link ? " expired" : ""}" data-action="gen-link" data-method="${method}" data-email="${email}" title="${tip}">${text}</button>${refreshButton()}</span>`;
}

function renderRows() {
  const body = $("#accountsTable tbody");
  if (!state.rows.length) {
    const message = state.search
      ? `未找到包含“${escapeHtml(state.search)}”的账号`
      : "当前筛选下暂无账号";
    body.innerHTML = `<tr><td class="empty" colspan="10"><i data-lucide="database-zap"></i> ${message}</td></tr>`;
    initIcons();
    return;
  }
  body.innerHTML = state.rows.map((row, index) => {
    const checkedAt = row.plus_check?.checked_at;
    const twoFaBusy = state.active2faEmails.has(row.email);
    const twoFaBound = row.totp_len > 0;
    return `<tr>
      <td class="check-cell"><input class="row-check" type="checkbox" data-email="${escapeHtml(row.email)}" ${state.selected.has(row.email) ? "checked" : ""}></td>
      <td class="index-cell">${(state.page - 1) * state.pageSize + index + 1}</td>
      <td><span class="truncate email" title="${escapeHtml(row.email)}">${escapeHtml(row.email)}</span><span class="subline">录入 ${fmtTime(row.created_at)}</span></td>
      <td>${plusBadge(row.plus_check)}</td>
      <td>${usedBadge(row)}</td>
      <td>${tokenButton(row, "access_token", row.at_len)}</td>
      <td>${tokenButton(row, "session_token", row.st_len)}</td>
      <td>${tokenButton(row, "refresh_token", row.rt_len)}</td>
      <td><span class="truncate">${checkedAt ? fmtTime(checkedAt) : "未检查"}</span></td>
      <td><div class="actions">
        <button class="icon-action" data-action="view" data-email="${escapeHtml(row.email)}" title="查看凭证"><i data-lucide="eye"></i></button>
        <button class="icon-action" data-action="email" data-email="${escapeHtml(row.email)}" title="获取原始邮箱四段"><i data-lucide="mail"></i></button>
        <button class="text-action" data-action="status" data-email="${escapeHtml(row.email)}" title="实时刷新账号状态">状态</button>
        <button class="text-action${twoFaBound ? " bound" : ""}" data-action="${twoFaBound ? "copy-2fa" : "bind-2fa"}" data-email="${escapeHtml(row.email)}" title="${twoFaBusy ? "2FA 绑定任务进行中" : twoFaBound ? "复制 账号----密码----2FA 密钥" : "邮箱重认证后绑定 2FA"}" ${twoFaBusy ? "disabled" : ""}>${twoFaBusy ? "处理中" : twoFaBound ? "2FA" : "绑 2FA"}</button>
        ${linkButton(row, "upi")}
        ${linkButton(row, "kakao")}
        <button class="icon-action" data-action="download" data-email="${escapeHtml(row.email)}" title="下载 Sub2API JSON" ${row.rt_len ? "" : "disabled"}><i data-lucide="download"></i></button>
        <button class="icon-action danger" data-action="delete" data-email="${escapeHtml(row.email)}" title="删除账号"><i data-lucide="trash-2"></i></button>
      </div></td>
    </tr>`;
  }).join("");
  initIcons();
}

async function refresh(reset = false, { notify = false } = {}) {
  if (state.refreshing) return;
  state.refreshing = true;
  const refreshButton = $("#refreshBtn");
  const originalTitle = refreshButton.title;
  let refreshAdjustedPage = false;
  refreshButton.disabled = true;
  refreshButton.title = "正在刷新列表";
  if (reset) state.page = 1;
  const offset = (state.page - 1) * state.pageSize;
  try {
    const result = await api(`api/registered?limit=${state.pageSize}&offset=${offset}&filter=${encodeURIComponent(state.filter)}&search=${encodeURIComponent(state.search)}`);
    state.rows = result.items;
    state.total = result.total;
    if (state.page > totalPages()) {
      state.page = totalPages();
      refreshAdjustedPage = true;
      return;
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
    $("#metricUsed").textContent = result.summary?.used ?? "—";
    $("#totalNote").textContent = result.summary?.total ?? result.total;
    $("#updatedAt").textContent = `列表刷新 ${new Date().toLocaleTimeString("zh-CN", {hour:"2-digit", minute:"2-digit"})}`;
    $("#serverState").textContent = "服务正常";
    updateSelection();
    if (notify) setToast("列表已刷新", "ok");
  } catch (error) {
    $("#serverState").textContent = "服务异常";
    setToast(`刷新失败：${error.message}`, "bad");
  } finally {
    state.refreshing = false;
    refreshButton.disabled = false;
    refreshButton.title = originalTitle || "刷新列表";
    if (refreshAdjustedPage) void refresh(false, { notify });
  }
}

async function getCredential(email) {
  const { data } = await api(`api/registered/${encodeURIComponent(email)}`);
  return data;
}

async function markUsed(emails) {
  if (!emails || !emails.length) return;
  try {
    await api("api/account-management/mark_used", {
      method: "POST", body: JSON.stringify({ emails }),
    });
    await refresh(false);
  } catch (_) {
    // 静默失败：使用状态只是辅助标记，不应该打断主操作的成功提示
  }
}

async function copyTokens(emails) {
  const values = [];
  const usedEmails = [];
  let skipped = 0;
  for (const email of emails) {
    const data = await getCredential(email);
    if (data.access_token) { values.push(data.access_token); usedEmails.push(email); }
    else skipped += 1;
  }
  if (!values.length) throw new Error("所选账号都没有 AT");
  await copyText(values.join("\n"));
  setToast(`已复制 ${values.length} 个 AT${skipped ? `，跳过 ${skipped} 个` : ""}`, "ok");
  markUsed(usedEmails);
}

async function copySourceEmails(emails) {
  const values = [];
  const copiedEmails = [];
  const failed = [];
  for (const email of emails) {
    try {
      const result = await api(`api/account-management/email/${encodeURIComponent(email)}`);
      values.push(result.raw);
      copiedEmails.push(email);
    } catch (_) {
      failed.push(email);
    }
  }
  if (!values.length) throw new Error("号池中没有所选账号的原始四段邮箱");
  await copyText(values.join("\n"));
  setToast(`已复制 ${values.length} 条邮箱四段${failed.length ? `，${failed.length} 条未找到` : ""}`, "ok");
  await markUsed(copiedEmails);
}

async function copyTwoFactor(emails) {
  const result = await api("api/account-management/2fa/lines", {
    method: "POST",
    body: JSON.stringify({ emails }),
  });
  await copyText(result.lines.join("\n"));
  const missing = result.missing?.length || 0;
  setToast(`已复制 ${result.lines.length} 条 2FA${missing ? `，跳过 ${missing} 个未绑定账号` : ""}`, "ok");
}

async function deleteSelectedAccounts() {
  const emails = selectedEmails();
  if (!emails.length) throw new Error("请先勾选要删除的异常账号");
  if (!confirm(`确定删除选中的 ${emails.length} 个账号注册凭证？\n此操作不可恢复，但不会删除号池中的原始邮箱。`)) return;

  const result = await api("api/registered/bulk_delete", {
    method: "POST",
    body: JSON.stringify({ emails }),
  });
  state.selected.clear();
  await refresh();
  setToast(`已删除 ${result.deleted || 0} 个账号`, "ok");
}

function showCredential(email, data) {
  state.modalData = data;
  $("#modalTitle").textContent = email;
  const fields = [
    ["email", "邮箱"], ["access_token", "Access Token"], ["session_token", "Session Token"],
    ["refresh_token", "Refresh Token"], ["id_token", "ID Token"],
    ["totp_secret", "2FA 密钥"],
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
      otp_timeout: 180,
    }),
  });
  const busySet = path.includes("acquire-rt") ? state.activeRtEmails
    : path.includes("bind-2fa") ? state.active2faEmails : null;
  if (busySet) {
    emails.forEach(email => busySet.add(email));
    renderRows();
    updateSelection();
    if (result.reused) setToast("该账号已有同类任务在跑，已继续查看原任务", "ok");
  }
  watchTask(result.task_id, label, busySet ? { set: busySet, emails } : null, emails);
}

async function startLinkTask(email, method) {
  const key = `${method}:${email}`;
  const result = await api("api/account-management/tasks/gen-link", {
    method: "POST",
    body: JSON.stringify({ emails: [email], method }),
  });
  state.activeLinkEmails.add(key);
  renderRows();
  watchTask(result.task_id, `生成 ${LINK_LABELS[method]} 链接`, { set: state.activeLinkEmails, emails: [key] }, [email]);
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

function watchTask(taskId, label, busy = null, requestedEmails = []) {
  state.visibleTaskId = taskId;
  $("#taskDrawer").classList.remove("hidden");
  $("#taskTitle").textContent = label;
  $("#taskState").textContent = "等待";
  $("#taskState").className = "task-state";
  $("#taskPhase").textContent = "等待执行";
  $("#taskMeta").textContent = `0 / ${busy?.emails.length || "—"}`;
  $("#taskDetail").textContent = "任务正在排队";
  $("#taskActionRequired").textContent = "";
  $("#taskActionRequired").classList.add("hidden");
  $("#taskEvents").innerHTML = "";
  $("#taskProgress").style.width = "0%";
  $("#taskDownloadBtn").hidden = true;
  $("#taskDownloadBtn").dataset.taskId = "";
  const schedule = (delay) => {
    const prior = state.taskTimers.get(taskId);
    if (prior) clearTimeout(prior);
    state.taskTimers.set(taskId, setTimeout(poll, delay));
  };
  const poll = async () => {
    try {
      const task = await api(`api/account-tasks/${taskId}`);
      const isVisible = state.visibleTaskId === taskId;
      const pct = task.total ? Math.round(task.completed / task.total * 100) : 100;
      if (isVisible) {
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
      }
      if (["done", "partial", "failed"].includes(task.state)) {
        state.taskTimers.delete(taskId);
        busy?.emails.forEach(email => busy.set.delete(email));
        const tone = task.succeeded ? "ok" : "bad";
        if (isVisible) setToast(task.message, tone);
        if (["sub2_export", "acquire_rt"].includes(task.kind) && task.download_ready) {
          markUsed(requestedEmails);
        }
        await refresh(false);
        if (isVisible && task.kind === "acquire_rt" && task.download_ready) {
          window.location.assign(appUrl(`api/account-tasks/${taskId}/download`));
          $("#taskDownloadBtn").hidden = true;
        }
        return;
      }
      schedule(900);
    } catch (error) {
      const detail = String(error?.message || error || "未知错误");
      const missingTask = /(?:404|任务不存在|已过期)/.test(detail);
      if (!missingTask) {
        if (state.visibleTaskId === taskId) {
          $("#taskMeta").textContent = `任务查询失败，正在重试：${detail}`;
        }
        schedule(1500);
        return;
      }
      const timer = state.taskTimers.get(taskId);
      if (timer) clearTimeout(timer);
      state.taskTimers.delete(taskId);
      busy?.emails.forEach(email => busy.set.delete(email));
      renderRows();
      updateSelection();
      if (state.visibleTaskId === taskId) {
        $("#taskState").textContent = "已中断";
        $("#taskState").className = "task-state bad";
        $("#taskMeta").textContent = "任务已中断";
        $("#taskDetail").textContent = "服务重启或任务已过期，请重新提交。";
        setToast("后台任务已中断，已解除按钮锁定", "bad");
      }
      await refresh(false);
    }
  };
  poll();
}

$("#filterSegment").addEventListener("click", event => {
  const button = event.target.closest("[data-filter]");
  if (!button) return;
  state.filter = button.dataset.filter;
  $("#filterSegment").querySelectorAll("button").forEach(item => item.classList.toggle("active", item === button));
  $("#plusFilter").value = "plus";
  $("#plusFilterWrap").classList.remove("active");
  state.selected.clear();
  refresh(true);
});

$("#plusFilter").addEventListener("change", event => {
  state.filter = event.target.value;
  $("#filterSegment").querySelectorAll("button").forEach(item => item.classList.remove("active"));
  $("#plusFilterWrap").classList.add("active");
  state.selected.clear();
  refresh(true);
});

$("#refreshBtn").addEventListener("click", () => refresh(false, { notify: true }));
let searchTimer = null;
$("#searchInput").addEventListener("input", event => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.search = event.target.value.trim();
    state.selected.clear();
    refresh(true);
  }, 300);
});
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
      if (field === "access_token" && data[field]) markUsed([email]);
    } else if (action === "view") {
      showCredential(email, await getCredential(email));
    } else if (action === "email") {
      await copySourceEmails([email]);
    } else if (action === "copy-2fa") {
      await copyTwoFactor([email]);
    } else if (action === "bind-2fa") {
      await startTask("api/account-management/tasks/bind-2fa", [email], "绑定 2FA");
    } else if (action === "status") {
      await startTask("api/account-management/tasks/refresh-status", [email], "刷新账号状态");
    } else if (action === "acquire" || action === "acquire-rt") {
      await startTask("api/account-management/tasks/acquire-rt", [email], "重新 OTP 登录获取 RT");
    } else if (action === "copy-link") {
      const method = button.dataset.method;
      const link = state.rows.find(row => row.email === email)?.links?.[method]?.link;
      if (!link) { setToast("链接不存在，请重新生成", "bad"); return; }
      await copyText(link);
      setToast(`${LINK_LABELS[method]} 链接已复制`, "ok");
    } else if (action === "gen-link") {
      await startLinkTask(email, button.dataset.method);
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
$("#deleteSelectedBtn").addEventListener("click", () => deleteSelectedAccounts().catch(error => setToast(error.message, "bad")));
$("#copyEmailBtn").addEventListener("click", () => copySourceEmails(selectedEmails()).catch(error => setToast(error.message, "bad")));
$("#copy2faBtn").addEventListener("click", () => copyTwoFactor(selectedEmails()).catch(error => setToast(error.message, "bad")));
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
