(() => {
const APP_BASE = (() => {
  const path = window.location.pathname;
  return path.endsWith("/") ? path : path.slice(0, path.lastIndexOf("/") + 1);
})();

function appUrl(path = "") {
  return `${APP_BASE}${String(path).replace(/^\/+/, "")}`;
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(appUrl(path), {
    cache: "no-store",
    ...options,
    headers,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || response.statusText);
  return data;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function fmtTime(seconds, withDate = true) {
  if (!seconds) return "—";
  const date = new Date(Number(seconds) * 1000);
  return date.toLocaleString("zh-CN", withDate ? {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  } : { hour: "2-digit", minute: "2-digit" });
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.style.cssText = "position:fixed;left:-9999px;top:0";
  document.body.appendChild(area);
  area.select();
  const ok = document.execCommand("copy");
  area.remove();
  if (!ok) throw new Error("浏览器拒绝剪贴板操作");
}

function initIcons() {
  if (window.lucide) window.lucide.createIcons({ attrs: { "stroke-width": 1.8 } });
}

function setToast(message, tone = "") {
  const toast = document.querySelector("#toast");
  if (!toast) return;
  toast.textContent = message;
  toast.className = `toast show ${tone}`.trim();
  clearTimeout(setToast.timer);
  setToast.timer = setTimeout(() => { toast.className = "toast"; }, 3600);
}

window.Workbench = {
  api, appUrl, copyText, escapeHtml, fmtTime, initIcons, setToast,
};
})();
