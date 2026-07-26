const { api, appUrl, copyText, escapeHtml, fmtTime, initIcons, setToast } = window.Workbench;
const $ = selector => document.querySelector(selector);
const state = {
  page: 1, pageSize: 50, total: 0, search: "", registered: "",
  rows: [], selected: new Set(), fetchingCode: new Set(),
};

function pages() { return Math.max(1, Math.ceil(state.total / state.pageSize)); }
function selectedEmails() { return [...state.selected]; }

function rawAccountLine(row) {
  return [row.email, row.password || "", row.client_id || "", row.refresh_token || ""].join("----");
}

function updateSelection() {
  const count = state.selected.size;
  $("#selectedCount").textContent = count;
  $("#resetSelectedBtn").disabled = count === 0;
  $("#deleteSelectedBtn").disabled = count === 0;
  $("#copySelectedBtn").disabled = count === 0;
  const visible = state.rows.map(row => row.email);
  $("#selectAll").checked = visible.length > 0 && visible.every(email => state.selected.has(email));
  $("#selectAll").indeterminate = visible.some(email => state.selected.has(email)) && !$("#selectAll").checked;
}

function renderRows() {
  const body = $("#poolTable tbody");
  if (!state.rows.length) {
    body.innerHTML = `<tr><td class="empty" colspan="6"><i data-lucide="inbox"></i> 当前筛选下暂无邮箱</td></tr>`;
    initIcons();
    return;
  }
  body.innerHTML = state.rows.map(row => {
    const canReset = ["done", "failed"].includes(row.status);
    const fetching = state.fetchingCode.has(row.email);
    return `<tr>
      <td class="check-cell"><input class="row-check" type="checkbox" data-email="${escapeHtml(row.email)}" ${state.selected.has(row.email) ? "checked" : ""}></td>
      <td><span class="truncate email" title="${escapeHtml(row.email)}">${escapeHtml(row.email)}</span><span class="subline">导入 ${fmtTime(row.imported_at)}</span></td>
      <td><span class="badge ${escapeHtml(row.status)}">${escapeHtml(row.status)}</span></td>
      <td><span class="badge ${row.is_registered ? "registered" : "unregistered"}">${row.is_registered ? "已注册" : "未注册"}</span></td>
      <td><span class="truncate" title="${escapeHtml(row.fail_reason || "")}">${escapeHtml(row.fail_reason || "—")}</span></td>
      <td><div class="actions">
        <a href="${appUrl(`?email=${encodeURIComponent(row.email)}`)}" title="到控制台使用此邮箱"><button class="text-action"><i data-lucide="play"></i>使用</button></a>
        <button class="text-action" data-action="fetch-code" data-email="${escapeHtml(row.email)}" title="从该邮箱现取一次最近的验证码" ${fetching ? "disabled" : ""}>${fetching ? "取码中" : "接码"}</button>
        <button class="icon-action" data-action="copy" data-email="${escapeHtml(row.email)}" title="复制账号（四段邮箱）"><i data-lucide="copy"></i></button>
        <button class="icon-action" data-action="reset" data-email="${escapeHtml(row.email)}" title="重置为 available" ${canReset ? "" : "disabled"}><i data-lucide="rotate-ccw"></i></button>
        <button class="icon-action danger" data-action="delete" data-email="${escapeHtml(row.email)}" title="删除"><i data-lucide="trash-2"></i></button>
      </div></td>
    </tr>`;
  }).join("");
  initIcons();
}

async function refresh(reset = false) {
  if (reset) state.page = 1;
  const status = $("#statusFilter").value;
  const search = state.search;
  const registered = $("#registeredFilter").value;
  const offset = (state.page - 1) * state.pageSize;
  try {
    const [list, statsResult] = await Promise.all([
      api(`api/accounts?status=${encodeURIComponent(status)}&search=${encodeURIComponent(search)}&registered=${encodeURIComponent(registered)}&limit=${state.pageSize}&offset=${offset}`),
      api("api/stats"),
    ]);
    state.rows = list.items;
    state.total = list.total;
    if (state.page > pages()) {
      state.page = pages();
      return refresh(false);
    }
    renderRows();
    $("#pageInfo").textContent = `第 ${state.page} / ${pages()} 页 · ${state.total} 条`;
    $("#prevBtn").disabled = state.page <= 1;
    $("#nextBtn").disabled = state.page >= pages();
    const stats = statsResult.stats;
    $("#metricTotal").textContent = stats.total || 0;
    $("#metricAvailable").textContent = stats.available || 0;
    $("#metricInUse").textContent = stats.in_use || 0;
    $("#metricDone").textContent = stats.done || 0;
    $("#metricFailed").textContent = stats.failed || 0;
    $("#metricRegistered").textContent = stats.registered || 0;
    $("#totalNote").textContent = stats.total || 0;
    $("#updatedAt").textContent = `列表刷新 ${new Date().toLocaleTimeString("zh-CN", {hour:"2-digit", minute:"2-digit"})}`;
    $("#serverState").textContent = "服务正常";
    updateSelection();
  } catch (error) {
    $("#serverState").textContent = "服务异常";
    setToast(`刷新失败：${error.message}`, "bad");
  }
}

async function resetSelected(emails) {
  const result = await api("api/accounts/bulk_reset", {
    method: "POST", body: JSON.stringify({ emails }),
  });
  state.selected.clear();
  await refresh();
  setToast(`已重置 ${result.reset} 个号`, "ok");
}

async function deleteSelected(emails) {
  const result = await api("api/accounts/bulk_delete", {
    method: "POST", body: JSON.stringify({ emails }),
  });
  state.selected.clear();
  await refresh();
  setToast(`已删除 ${result.deleted} 个号`, "ok");
}

$("#refreshBtn").addEventListener("click", () => refresh());
$("#statusFilter").addEventListener("change", () => { state.selected.clear(); refresh(true); });
$("#registeredFilter").addEventListener("change", () => { state.selected.clear(); refresh(true); });
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
$("#nextBtn").addEventListener("click", () => { if (state.page < pages()) { state.page += 1; refresh(); } });
$("#selectAll").addEventListener("change", event => {
  for (const row of state.rows) {
    if (event.target.checked) state.selected.add(row.email);
    else state.selected.delete(row.email);
  }
  renderRows();
  updateSelection();
});
$("#poolTable").addEventListener("change", event => {
  if (!event.target.matches(".row-check")) return;
  if (event.target.checked) state.selected.add(event.target.dataset.email);
  else state.selected.delete(event.target.dataset.email);
  updateSelection();
});
$("#poolTable").addEventListener("click", async event => {
  const button = event.target.closest("button[data-action]");
  if (!button || button.disabled) return;
  const email = button.dataset.email;
  const action = button.dataset.action;
  try {
    if (action === "reset") {
      await api(`api/accounts/reset/${encodeURIComponent(email)}`, { method: "POST" });
      setToast(`${email} 已重置`, "ok");
    } else if (action === "delete") {
      if (!confirm(`删除 ${email}？`)) return;
      await api(`api/accounts/${encodeURIComponent(email)}`, { method: "DELETE" });
      state.selected.delete(email);
      setToast(`${email} 已删除`, "ok");
    } else if (action === "copy") {
      const row = state.rows.find(r => r.email === email);
      if (!row) throw new Error("找不到该行数据");
      await copyText(rawAccountLine(row));
      setToast(`已复制 ${email} 的四段邮箱`, "ok");
      return;
    } else if (action === "fetch-code") {
      state.fetchingCode.add(email);
      renderRows();
      try {
        const result = await api(`api/accounts/${encodeURIComponent(email)}/fetch_code`, { method: "POST" });
        await copyText(result.code);
        setToast(`验证码 ${result.code}（已复制）`, "ok");
      } finally {
        state.fetchingCode.delete(email);
        renderRows();
      }
      return;
    }
    await refresh();
  } catch (error) {
    setToast(error.message, "bad");
  }
});

$("#copySelectedBtn").addEventListener("click", async () => {
  const emails = selectedEmails();
  const rows = state.rows.filter(row => emails.includes(row.email));
  if (!rows.length) return setToast("所选账号不在当前页，请翻到对应页再复制", "bad");
  try {
    await copyText(rows.map(rawAccountLine).join("\n"));
    setToast(`已复制 ${rows.length} 条四段邮箱`, "ok");
  } catch (error) { setToast(error.message, "bad"); }
});

$("#resetSelectedBtn").addEventListener("click", () => {
  resetSelected(selectedEmails()).catch(error => setToast(error.message, "bad"));
});
$("#deleteSelectedBtn").addEventListener("click", () => {
  const emails = selectedEmails();
  if (!confirm(`删除选中的 ${emails.length} 个号？`)) return;
  deleteSelected(emails).catch(error => setToast(error.message, "bad"));
});
$("#resetFailedBtn").addEventListener("click", async () => {
  try {
    const result = await api("api/accounts/reset_failed", { method: "POST" });
    setToast(`已重置 ${result.reset} 个 failed 号`, "ok");
    await refresh();
  } catch (error) { setToast(error.message, "bad"); }
});
$("#releaseBtn").addEventListener("click", async () => {
  try {
    const result = await api("api/accounts/release_stale", { method: "POST" });
    setToast(`已释放 ${result.released} 个卡死号`, "ok");
    await refresh();
  } catch (error) { setToast(error.message, "bad"); }
});
$("#bulkDeleteBtn").addEventListener("click", async () => {
  const status = $("#bulkStatus").value;
  if (!status) return setToast("请先选择要删除的状态", "bad");
  if (!confirm(status === "all" ? "确定删除号池全部账号？此操作不可撤销。" : `删除全部 ${status} 账号？`)) return;
  try {
    const result = await api("api/accounts/bulk_delete", {
      method: "POST", body: JSON.stringify({ status }),
    });
    setToast(`已删除 ${result.deleted} 个账号`, "ok");
    $("#bulkStatus").value = "";
    await refresh(true);
  } catch (error) { setToast(error.message, "bad"); }
});

$("#toggleImportBtn").addEventListener("click", () => $("#importPanel").classList.toggle("hidden"));
$("#cancelImportBtn").addEventListener("click", () => $("#importPanel").classList.add("hidden"));
$("#importBtn").addEventListener("click", async () => {
  const text = $("#importText").value.trim();
  if (!text) return setToast("请粘贴四段邮箱", "bad");
  try {
    const result = await api("api/import", { method: "POST", body: JSON.stringify({ text }) });
    setToast(`导入 ${result.inserted}，更新 ${result.updated}，跳过 ${result.skipped}`, "ok");
    $("#importText").value = "";
    $("#importPanel").classList.add("hidden");
    await refresh(true);
  } catch (error) { setToast(error.message, "bad"); }
});

initIcons();
refresh(true);
