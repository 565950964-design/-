/* app.js - 记账小助手前端逻辑 */

const API = "";  // 同域调用
const currentUserId = new URLSearchParams(window.location.search).get("user_id") || "web-local";
const ADMIN_TOKEN_KEY = "bookkeeper_admin_token";
const USER_TOKEN_KEY = "bookkeeper_user_token";
let adminToken = localStorage.getItem(ADMIN_TOKEN_KEY) || "";
let userToken = localStorage.getItem(USER_TOKEN_KEY) || "";


function withUserId(path) {
  const separator = path.includes("?") ? "&" : "?";
  const tokenPart = userToken ? `&user_token=${encodeURIComponent(userToken)}` : "";
  return `${path}${separator}user_id=${encodeURIComponent(currentUserId)}${tokenPart}`;
}

function getUserHeaders(extraHeaders = {}) {
  const headers = { ...extraHeaders };
  if (userToken) {
    headers["X-User-Token"] = userToken;
  }
  return headers;
}

async function apiFetch(path, options = {}) {
  const merged = { ...options };
  merged.headers = getUserHeaders(options.headers || {});
  return fetch(path, merged);
}

// ========== 全局状态 ==========
let currentYear = new Date().getFullYear();
let currentMonth = new Date().getMonth() + 1;
let currentDay = new Date().getDate();
let dashboardPeriod = "month";
let billsData = [];
let categoryChartInstance = null;
let trendChartInstance = null;
let addBillType = "expense";
let currentBudget = 0;
let currentExpense = 0;


function initUserSwitcher() {
  const label = document.getElementById("currentUserLabel");
  const input = document.getElementById("userIdInput");
  const tokenInput = document.getElementById("userTokenInput");
  const saveTokenBtn = document.getElementById("saveUserTokenBtn");
  const button = document.getElementById("switchUserBtn");

  label.textContent = currentUserId;
  input.value = currentUserId === "web-local" ? "" : currentUserId;
  if (tokenInput) tokenInput.value = userToken;

  const switchUser = () => {
    const nextUserId = input.value.trim();
    const url = new URL(window.location.href);
    if (nextUserId) {
      url.searchParams.set("user_id", nextUserId);
    } else {
      url.searchParams.delete("user_id");
    }
    window.location.href = url.toString();
  };

  button.addEventListener("click", switchUser);
  if (saveTokenBtn && tokenInput) {
    saveTokenBtn.addEventListener("click", () => {
      userToken = tokenInput.value.trim();
      if (userToken) {
        localStorage.setItem(USER_TOKEN_KEY, userToken);
        showToast("用户口令已保存 ✅");
      } else {
        localStorage.removeItem(USER_TOKEN_KEY);
        showToast("用户口令已清除");
      }
    });
  }
  input.addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      switchUser();
    }
  });
}


function getAdminHeaders(extraHeaders = {}) {
  const headers = { ...extraHeaders };
  if (adminToken) {
    headers["X-Admin-Token"] = adminToken;
  }
  return headers;
}


async function adminFetch(path, options = {}) {
  const merged = { ...options };
  merged.headers = getAdminHeaders(options.headers || {});
  return fetch(path, merged);
}


function setAdminContentVisible(visible) {
  const area = document.getElementById("admin-content-area");
  if (area) area.style.display = visible ? "" : "none";
}


function initAdminAuth() {
  const tokenInput = document.getElementById("adminTokenInput");
  const saveBtn = document.getElementById("saveAdminTokenBtn");
  const clearBtn = document.getElementById("clearAdminTokenBtn");

  tokenInput.value = adminToken;

  saveBtn.addEventListener("click", async () => {
    const candidate = tokenInput.value.trim();
    adminToken = candidate;
    localStorage.setItem(ADMIN_TOKEN_KEY, candidate);
    const ok = await verifyAdminToken();
    if (ok) {
      showToast("管理员登录成功 ✅");
      setAdminContentVisible(true);
      loadUsers();
    } else {
      setAdminContentVisible(false);
    }
  });

  clearBtn.addEventListener("click", () => {
    adminToken = "";
    localStorage.removeItem(ADMIN_TOKEN_KEY);
    tokenInput.value = "";
    showToast("已退出管理员登录");
    setAdminContentVisible(false);
  });

  tokenInput.addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      saveBtn.click();
    }
  });
}


async function verifyAdminToken(silent = false) {
  try {
    const res = await adminFetch(`${API}/api/admin/auth-check`);
    if (res.status === 401) {
      if (!silent) {
        showToast("管理员口令无效或未配置 ⚠️");
      }
      return false;
    }
    const data = await res.json();
    return !!data.success;
  } catch {
    if (!silent) {
      showToast("管理员鉴权请求失败 ⚠️");
    }
    return false;
  }
}

// ========== 工具函数 ==========
function showToast(msg, duration = 2500) {
  const toast = document.getElementById("toast");
  toast.textContent = msg;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), duration);
}

function formatAmount(amount) {
  return "¥" + Number(amount).toFixed(2);
}

function formatDate(dateStr) {
  try {
    const d = new Date(dateStr);
    return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours().toString().padStart(2,"0")}:${d.getMinutes().toString().padStart(2,"0")}`;
  } catch { return dateStr; }
}

function renderInsightCard(summary, budget) {
  const container = document.getElementById("insightGrid");
  if (!container || !summary) return;

  const topCategory = summary.categories && summary.categories.length
    ? `${summary.categories[0].category} ${formatAmount(summary.categories[0].total)}`
    : "本月还没有支出分类";

  let budgetText = "还没设置预算，可以去预算管理页加上月预算。";
  if (budget > 0) {
    const remain = budget - (summary.expense || 0);
    budgetText = remain >= 0
      ? `预算还剩 ${formatAmount(remain)}，保持得不错。`
      : `预算已超出 ${formatAmount(Math.abs(remain))}，后半月要收一收。`;
  }

  const balanceText = summary.balance >= 0
    ? `当前周期结余 ${formatAmount(summary.balance)}，现金流是正的。`
    : `当前周期净支出 ${formatAmount(Math.abs(summary.balance))}，注意平衡收入和支出。`;

  container.innerHTML = `
    <div class="insight-pill">🍓 最高消费分类：${topCategory}</div>
    <div class="insight-pill">🧁 ${budgetText}</div>
    <div class="insight-pill">🌷 ${balanceText}</div>
  `;
}

const CATEGORY_EMOJI = {
  "餐饮": "🍜", "交通": "🚕", "购物": "🛍️", "娱乐": "🎮",
  "居家": "🏠", "医疗": "💊", "教育": "📚", "人情": "🧧",
  "收入": "💰", "其他": "📝"
};

const CATEGORY_COLORS = [
  "#6c63ff", "#ff6b6b", "#51cf66", "#339af0", "#fcc419",
  "#ff8787", "#a9e34b", "#4dabf7", "#f06595", "#74c0fc"
];

// ========== 月份选择器 ==========
function updateMonthLabel() {
  const label = document.getElementById("currentMonth");
  if (dashboardPeriod === "day") {
    label.textContent = `${currentYear}年${currentMonth}月${currentDay}日`;
  } else if (dashboardPeriod === "quarter") {
    label.textContent = `${currentYear}年Q${Math.floor((currentMonth - 1) / 3) + 1}`;
  } else if (dashboardPeriod === "year") {
    label.textContent = `${currentYear}年`;
  } else {
    label.textContent = `${currentYear}年${currentMonth}月`;
  }
  refreshCurrentPage();
}

document.getElementById("prevMonth").addEventListener("click", () => {
  if (dashboardPeriod === "day") {
    const current = new Date(currentYear, currentMonth - 1, currentDay);
    current.setDate(current.getDate() - 1);
    currentYear = current.getFullYear();
    currentMonth = current.getMonth() + 1;
    currentDay = current.getDate();
  } else if (dashboardPeriod === "quarter") {
    currentMonth -= 3;
    if (currentMonth < 1) {
      currentMonth += 12;
      currentYear--;
    }
  } else if (dashboardPeriod === "year") {
    currentYear--;
  } else {
    currentMonth--;
    if (currentMonth < 1) { currentMonth = 12; currentYear--; }
  }
  updateMonthLabel();
});

document.getElementById("nextMonth").addEventListener("click", () => {
  if (dashboardPeriod === "day") {
    const current = new Date(currentYear, currentMonth - 1, currentDay);
    current.setDate(current.getDate() + 1);
    currentYear = current.getFullYear();
    currentMonth = current.getMonth() + 1;
    currentDay = current.getDate();
  } else if (dashboardPeriod === "quarter") {
    currentMonth += 3;
    if (currentMonth > 12) {
      currentMonth -= 12;
      currentYear++;
    }
  } else if (dashboardPeriod === "year") {
    currentYear++;
  } else {
    currentMonth++;
    if (currentMonth > 12) { currentMonth = 1; currentYear++; }
  }
  updateMonthLabel();
});

// ========== 导航切换 ==========
function switchPage(pageId) {
  document.querySelectorAll(".nav-item").forEach(item => item.classList.remove("active"));
  document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));

  const navItem = document.querySelector(`[data-page="${pageId}"]`);
  if (navItem) navItem.classList.add("active");

  const page = document.getElementById(`page-${pageId}`);
  if (page) page.classList.add("active");

  loadPageData(pageId);
}

function refreshCurrentPage() {
  const activePage = document.querySelector(".page.active");
  if (!activePage) return;
  const pageId = activePage.id.replace("page-", "");
  loadPageData(pageId);
}

function loadPageData(pageId) {
  switch (pageId) {
    case "dashboard": loadDashboard(); break;
    case "bills": loadBills(); break;
    case "budget": loadBudget(); break;
    case "users": loadUsers(); break;
    case "tutorial": break;
    case "chat": break;
    case "add": initAddForm(); break;
  }
}

document.querySelectorAll(".nav-item").forEach(item => {
  item.addEventListener("click", () => switchPage(item.dataset.page));
});

// ========== 聊天记账 ==========
function appendMessage(content, isUser = false) {
  const messages = document.getElementById("chatMessages");
  const div = document.createElement("div");
  div.className = `message ${isUser ? "user-message" : "bot-message"}`;
  const avatar = isUser ? "👤" : "🤖";
  div.innerHTML = `
    <div class="message-avatar">${avatar}</div>
    <div class="message-bubble">${content.replace(/\n/g, "<br/>")}</div>
  `;
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
}

function appendBotBillMessage(data) {
  const billId = data?.bill?.id;
  let content = data.reply || "记录失败，请重试";
  if (data.success && data.type === "add" && billId) {
    content += `\n<button class="chat-inline-btn" data-delete-bill-id="${billId}">撤销这笔</button>`;
  }
  appendMessage(content, false);

  const messages = document.getElementById("chatMessages");
  const latestBtn = messages.querySelector(".message:last-child .chat-inline-btn");
  if (latestBtn) {
    latestBtn.addEventListener("click", async () => {
      await deleteChatBill(Number(latestBtn.dataset.deleteBillId));
    });
  }
}

async function sendChat(forceText = "") {
  const input = document.getElementById("chatInput");
  const msg = (forceText || input.value || "").trim();
  if (!msg) return;
  if (!forceText) input.value = "";
  appendMessage(msg, true);

  try {
    const res = await apiFetch(`${API}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: msg, user_id: currentUserId })
    });
    const data = await res.json();
    appendBotBillMessage(data);
    if (data.success && ["add", "add-multi", "undo", "restore", "budget"].includes(data.type)) {
      refreshCurrentPage();
    }
    if (data.success && data.link) {
      window.open(data.link, "_blank", "noopener,noreferrer");
    }
  } catch (e) {
    appendMessage("网络错误，请重试 😢");
  }
}

async function deleteChatBill(id) {
  if (!id) return;
  const res = await apiFetch(withUserId(`${API}/api/bills/${id}`), { method: "DELETE" });
  const data = await res.json();
  if (data.success) {
    appendMessage("🗑️ 已撤销这笔误发记录", false);
    showToast("已撤销 ✅");
    refreshCurrentPage();
  } else {
    showToast("撤销失败 ⚠️");
  }
}

document.getElementById("sendBtn").addEventListener("click", sendChat);
document.getElementById("chatInput").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
});

document.querySelectorAll(".quick-action-btn").forEach(btn => {
  btn.addEventListener("click", () => sendChat(btn.dataset.chatCmd || ""));
});

document.querySelectorAll(".period-chip").forEach(btn => {
  btn.addEventListener("click", () => {
    dashboardPeriod = btn.dataset.period || "month";
    document.querySelectorAll(".period-chip").forEach(chip => chip.classList.remove("active"));
    btn.classList.add("active");
    updateMonthLabel();
  });
});

// ========== 账单总览 ==========
async function loadDashboard() {
  const budgetCard = document.getElementById("budgetProgressCard");
  const [summaryRes, trendRes, budgetRes] = await Promise.all([
    apiFetch(withUserId(`${API}/api/summary?period=${dashboardPeriod}&year=${currentYear}&month=${currentMonth}&day=${currentDay}`)),
    apiFetch(withUserId(`${API}/api/trend?period=${dashboardPeriod}&year=${currentYear}&month=${currentMonth}&day=${currentDay}`)),
    apiFetch(withUserId(`${API}/api/budget?year=${currentYear}&month=${currentMonth}`))
  ]);

  const summaryData = await summaryRes.json();
  const trendData = await trendRes.json();
  const budgetData = await budgetRes.json();

  const s = summaryData.summary;
  document.getElementById("totalExpense").textContent = formatAmount(s.expense);
  document.getElementById("totalIncome").textContent = formatAmount(s.income);
  document.getElementById("totalBalance").textContent = formatAmount(s.balance);
  document.getElementById("totalCount").textContent = s.count;
  document.getElementById("dashboardMonthLabel").textContent = `${summaryData.label || `${currentYear}年${currentMonth}月`}账单概览`;

  currentExpense = s.expense;
  currentBudget = budgetData.budget || 0;
  if (budgetCard) budgetCard.style.display = dashboardPeriod === "month" ? "" : "none";
  updateBudgetProgress();

  const todayD = new Date();
  const daysElapsed =
    dashboardPeriod === "day"
      ? 1
      : currentYear === todayD.getFullYear() && currentMonth === todayD.getMonth() + 1 && dashboardPeriod === "month"
        ? todayD.getDate()
        : dashboardPeriod === "month"
          ? new Date(currentYear, currentMonth, 0).getDate()
          : Math.max(s.count, 1);
  const avgEl = document.getElementById("dailyAvg");
  if (avgEl) avgEl.textContent = formatAmount(daysElapsed > 0 ? s.expense / daysElapsed : 0);

  renderInsightCard(s, currentBudget);

  renderCategoryChart(s.categories);
  renderTrendChart(trendData.trend);
}

function updateBudgetProgress() {
  const progressFill = document.getElementById("budgetProgressFill");
  const progressText = document.getElementById("budgetProgressText");

  if (currentBudget <= 0) {
    progressText.textContent = "未设置预算";
    progressFill.style.width = "0%";
    return;
  }

  const percent = Math.min(100, (currentExpense / currentBudget) * 100);
  progressFill.style.width = `${percent}%`;
  progressFill.className = `progress-fill${percent >= 90 ? " danger" : ""}`;
  progressText.textContent = `${formatAmount(currentExpense)} / ${formatAmount(currentBudget)} (${percent.toFixed(1)}%)`;
}

function renderCategoryChart(categories) {
  const ctx = document.getElementById("categoryChart").getContext("2d");
  if (categoryChartInstance) categoryChartInstance.destroy();

  if (!categories || categories.length === 0) {
    categoryChartInstance = new Chart(ctx, {
      type: "doughnut",
      data: { labels: ["暂无数据"], datasets: [{ data: [1], backgroundColor: ["#e8ecf4"] }] },
      options: { plugins: { legend: { display: false } }, cutout: "65%" }
    });
    return;
  }

  categoryChartInstance = new Chart(ctx, {
    type: "doughnut",
    data: {
      labels: categories.map(c => c.category),
      datasets: [{
        data: categories.map(c => c.total),
        backgroundColor: CATEGORY_COLORS.slice(0, categories.length),
        borderWidth: 2,
        borderColor: "#fff",
        hoverOffset: 8
      }]
    },
    options: {
      cutout: "65%",
      plugins: {
        legend: {
          position: "bottom",
          labels: {
            boxWidth: 12,
            padding: 12,
            font: { size: 12, family: "-apple-system, BlinkMacSystemFont, 'PingFang SC', sans-serif" }
          }
        },
        tooltip: {
          callbacks: {
            label: ctx => ` ${ctx.label}: ¥${ctx.parsed.toFixed(2)}`
          }
        }
      }
    }
  });
}

function renderTrendChart(trend) {
  const ctx = document.getElementById("trendChart").getContext("2d");
  if (trendChartInstance) trendChartInstance.destroy();

  const labels = (trend || []).map(t => t.label || `${t.month}月`);
  const expenses = (trend || []).map(t => t.expense);
  const incomes = (trend || []).map(t => t.income);

  trendChartInstance = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          label: "支出",
          data: expenses,
          backgroundColor: "rgba(255, 107, 107, 0.8)",
          borderRadius: 6,
          borderSkipped: false
        },
        {
          label: "收入",
          data: incomes,
          backgroundColor: "rgba(81, 207, 102, 0.8)",
          borderRadius: 6,
          borderSkipped: false
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: "top",
          labels: {
            boxWidth: 12,
            padding: 16,
            font: { size: 12 }
          }
        },
        tooltip: {
          callbacks: {
            label: ctx => ` ${ctx.dataset.label}: ¥${ctx.parsed.y.toFixed(2)}`
          }
        }
      },
      scales: {
        x: {
          grid: { display: false },
          border: { display: false }
        },
        y: {
          grid: { color: "rgba(0,0,0,0.04)" },
          border: { display: false },
          ticks: {
            callback: v => `¥${v}`
          }
        }
      }
    }
  });
}


function formatDateTime(dateStr) {
  if (!dateStr) return "-";
  try {
    const date = new Date(dateStr);
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  } catch {
    return dateStr;
  }
}


async function loadUsers() {
  const authed = await verifyAdminToken(true);
  if (!authed) {
    setAdminContentVisible(false);
    if (adminToken) showToast("管理员口令失效，请重新登录 ⚠️");
    return;
  }

  setAdminContentVisible(true);
  const res = await adminFetch(`${API}/api/wechat-users`);
  const data = await res.json();
  if (!data.success) {
    showToast("用户列表加载失败 ⚠️");
    return;
  }

  document.getElementById("pendingCount").textContent = data.counts.pending || 0;
  document.getElementById("approvedCount").textContent = data.counts.approved || 0;
  document.getElementById("adminCount").textContent = data.counts.admin || 0;
  document.getElementById("rejectedCount").textContent = data.counts.rejected || 0;

  const pendingUsers = data.users.filter(user => user.status === "pending");
  const approvedUsers = data.users.filter(user => user.status === "approved" || user.status === "admin");

  renderUsersList("pendingUsersList", pendingUsers, true);
  renderUsersList("approvedUsersList", approvedUsers, false);
  loadApprovalLogs();
}


async function loadApprovalLogs() {
  const res = await adminFetch(`${API}/api/approval-logs?limit=30`);
  const data = await res.json();
  if (!data.success) {
    renderApprovalLogs([]);
    return;
  }
  renderApprovalLogs(data.logs || []);
}


function renderApprovalLogs(logs) {
  const container = document.getElementById("approvalLogsList");
  if (!container) return;
  if (!logs.length) {
    container.innerHTML = `<div class="user-empty-state">暂无审批日志</div>`;
    return;
  }

  container.innerHTML = logs.map(log => {
    const actionText = log.action === "approve" ? "批准" : "拒绝";
    const timeText = formatDateTime(log.created_at);
    return `
      <div class="log-row">
        <div class="log-top">
          <span class="log-action ${log.action}">${actionText}</span>
          <span class="log-time">${timeText}</span>
        </div>
        <div class="log-meta">用户ID：${escapeHtml(log.user_id)}</div>
        <div class="log-meta">操作人：${escapeHtml(log.operator)} | 渠道：${escapeHtml(log.channel)}</div>
        <div class="log-meta">说明：${escapeHtml(log.note || "-")}</div>
      </div>
    `;
  }).join("");
}


function renderUsersList(containerId, users, isPending) {
  const container = document.getElementById(containerId);
  if (!users.length) {
    container.innerHTML = `<div class="user-empty-state">${isPending ? "暂无待审批用户" : "暂无已批准用户"}</div>`;
    return;
  }

  container.innerHTML = users.map(user => renderUserCard(user, isPending)).join("");

  container.querySelectorAll(".rename-save-btn").forEach(btn => {
    btn.addEventListener("click", () => saveUserProfile(btn.dataset.userId));
  });

  container.querySelectorAll(".user-action-btn.view").forEach(btn => {
    btn.addEventListener("click", () => switchToUserBook(btn.dataset.userId));
  });
}


function renderUserCard(user, isPending) {
  const displayName = user.display_name || "未备注用户";
  const note = user.requested_note || "未填写申请说明";
  const applyNickname = user.apply_nickname || "";
  const applyTail = user.apply_contact_tail || "";
  const approver = user.approved_by || "-";
  const requestedAt = formatDateTime(user.requested_at);
  const approvedAt = formatDateTime(user.approved_at);
  const badgeText = user.status === "admin" ? "管理员" : user.status === "approved" ? "已批准" : user.status === "rejected" ? "已拒绝" : "待审批";
  const key = encodeURIComponent(user.user_id);
  return `
    <div class="user-card" data-user-id="${escapeHtml(user.user_id)}">
      <div class="user-card-header">
        <div>
          <div class="user-card-name">${displayName}</div>
          <div class="user-card-id">${user.user_id}</div>
        </div>
        <span class="user-status-badge ${user.status}">${badgeText}</span>
      </div>
      <div class="user-card-note">申请说明：${note}</div>
      <div class="user-card-meta">
        <span>申请时间：${requestedAt}</span>
        <span>审批时间：${approvedAt}</span>
        <span>审批人：${approver}</span>
        <span>账单数：${user.bill_count || 0}</span>
      </div>
      <div class="user-card-note">系统备注：${escapeHtml(displayName)}</div>
      <div class="user-card-note">申请昵称：${escapeHtml(applyNickname || "未填写")}</div>
      <div class="user-card-note">手机尾号：${escapeHtml(applyTail || "未填写")}</div>
      <div class="user-card-remark-row">
        <input type="text" class="form-input remark-input" id="rename-${key}" value="${escapeHtml(displayName === "未备注用户" ? "" : displayName)}" placeholder="管理员可设置中文账本名" />
        <button class="rename-save-btn" data-user-id="${user.user_id}">保存账本名</button>
      </div>
      <div class="user-actions">
        <button class="user-action-btn view" data-user-id="${user.user_id}">查看账本</button>
      </div>
    </div>
  `;
}


function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;")
    .replace(/'/g, "&#39;");
}


async function saveUserProfile(userId) {
  const key = encodeURIComponent(userId);
  const renameInput = document.getElementById(`rename-${key}`);

  if (!renameInput) {
    showToast("用户字段读取失败 ⚠️");
    return;
  }

  const displayName = renameInput.value.trim();

  const res = await adminFetch(`${API}/api/wechat-users/${encodeURIComponent(userId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      display_name: displayName
    })
  });
  const data = await res.json();
  if (data.success) {
    showToast("账本名已保存 ✅");
    loadUsers();
  } else {
    showToast(data.message || "保存失败 ⚠️");
  }
}


async function approveUser(userId) {
  const res = await adminFetch(`${API}/api/wechat-users/${encodeURIComponent(userId)}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approved_by: currentUserId })
  });
  const data = await res.json();
  if (data.success) {
    showToast("已批准该用户 ✅");
    loadUsers();
  } else {
    showToast(data.message || "批准失败 ⚠️");
  }
}


async function rejectUser(userId) {
  const res = await adminFetch(`${API}/api/wechat-users/${encodeURIComponent(userId)}/reject`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approved_by: currentUserId })
  });
  const data = await res.json();
  if (data.success) {
    showToast("已拒绝该用户");
    loadUsers();
  } else {
    showToast(data.message || "拒绝失败 ⚠️");
  }
}


function switchToUserBook(userId) {
  const url = new URL(window.location.href);
  url.searchParams.set("user_id", userId);
  window.location.href = url.toString();
}

// ========== 账单明细 ==========
async function loadBills() {
  const filterType = document.getElementById("filterType").value;
  const res = await apiFetch(withUserId(`${API}/api/bills?year=${currentYear}&month=${currentMonth}&type=${filterType}`));
  const data = await res.json();
  billsData = data.bills || [];
  filterAndRenderBills();
}

function filterAndRenderBills() {
  const search = (document.getElementById("billSearch")?.value || "").trim().toLowerCase();
  const category = document.getElementById("filterCategory")?.value || "all";
  let filtered = billsData;
  if (search) {
    filtered = filtered.filter(b =>
      (b.description || "").toLowerCase().includes(search) ||
      (b.category || "").toLowerCase().includes(search)
    );
  }
  if (category !== "all") {
    filtered = filtered.filter(b => b.category === category);
  }
  renderBillsList(filtered);
}

function renderBillsList(bills) {
  const container = document.getElementById("billsList");
  if (!bills || bills.length === 0) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">📭</div>
        <p>本月暂无账单记录</p>
      </div>`;
    return;
  }

  // 按日期分组
  const groups = {};
  bills.forEach(bill => {
    const key = bill.day ? `${bill.year}-${String(bill.month).padStart(2,"0")}-${String(bill.day).padStart(2,"0")}` : "未知日期";
    if (!groups[key]) groups[key] = { bills: [], total: 0 };
    groups[key].bills.push(bill);
    if (bill.bill_type === "expense") groups[key].total += bill.amount;
  });

  const sortedKeys = Object.keys(groups).sort((a, b) => b.localeCompare(a));

  container.innerHTML = sortedKeys.map(dateKey => {
    const group = groups[dateKey];
    const dateLabel = formatGroupDate(dateKey);
    const billsHtml = group.bills.map(bill => renderBillItem(bill)).join("");
    return `
      <div class="bill-group-header">
        <span>${dateLabel}</span>
        <span>支出 ${formatAmount(group.total)}</span>
      </div>
      ${billsHtml}
    `;
  }).join("");

  // 绑定操作按钮
  container.querySelectorAll(".action-btn.edit").forEach(btn => {
    btn.addEventListener("click", () => openEditModal(parseInt(btn.dataset.id)));
  });
  container.querySelectorAll(".action-btn.delete").forEach(btn => {
    btn.addEventListener("click", () => deleteBill(parseInt(btn.dataset.id)));
  });
}

function formatGroupDate(dateKey) {
  const today = new Date();
  const todayStr = `${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,"0")}-${String(today.getDate()).padStart(2,"0")}`;
  const yesterday = new Date(today); yesterday.setDate(today.getDate()-1);
  const yesterdayStr = `${yesterday.getFullYear()}-${String(yesterday.getMonth()+1).padStart(2,"0")}-${String(yesterday.getDate()).padStart(2,"0")}`;
  if (dateKey === todayStr) return "今天";
  if (dateKey === yesterdayStr) return "昨天";
  const [, m, d] = dateKey.split("-");
  return `${parseInt(m)}月${parseInt(d)}日`;
}

function renderBillItem(bill) {
  const emoji = CATEGORY_EMOJI[bill.category] || "📝";
  const isIncome = bill.bill_type === "income";
  const amountClass = isIncome ? "income" : "";
  const amountPrefix = isIncome ? "+" : "-";
  const timeStr = bill.created_at ? formatDate(bill.created_at) : "";

  return `
    <div class="bill-item" data-id="${bill.id}">
      <div class="bill-category-icon">${emoji}</div>
      <div class="bill-info">
        <div class="bill-desc">${bill.description || bill.category}</div>
        <div class="bill-meta">
          <span>${bill.category}</span>
          <span>${timeStr}</span>
        </div>
      </div>
      <div class="bill-amount ${amountClass}">${amountPrefix}${formatAmount(bill.amount)}</div>
      <div class="bill-actions">
        <button class="action-btn edit" data-id="${bill.id}" title="编辑">✏️</button>
        <button class="action-btn delete" data-id="${bill.id}" title="删除">🗑️</button>
      </div>
    </div>
  `;
}

document.getElementById("filterType").addEventListener("change", loadBills);
document.getElementById("billSearch")?.addEventListener("input", filterAndRenderBills);
document.getElementById("filterCategory")?.addEventListener("change", filterAndRenderBills);
document.getElementById("resetBillsFilterBtn")?.addEventListener("click", () => {
  const search = document.getElementById("billSearch");
  const type = document.getElementById("filterType");
  const category = document.getElementById("filterCategory");
  if (search) search.value = "";
  if (type) type.value = "all";
  if (category) category.value = "all";
  loadBills();
});
document.getElementById("exportBillsBtn")?.addEventListener("click", () => {
  window.location.href = withUserId(`${API}/api/bills/export?year=${currentYear}&month=${currentMonth}`);
});

// ========== 删除账单 ==========
async function deleteBill(id) {
  if (!confirm("确定要删除这条记录吗？")) return;
  const res = await apiFetch(withUserId(`${API}/api/bills/${id}`), { method: "DELETE" });
  const data = await res.json();
  if (data.success) {
    showToast("删除成功 ✅");
    loadBills();
  }
}

// ========== 编辑账单 ==========
function openEditModal(id) {
  const bill = billsData.find(b => b.id === id);
  if (!bill) return;

  document.getElementById("editId").value = id;
  document.getElementById("editAmount").value = bill.amount;
  document.getElementById("editCategory").value = bill.category;
  document.getElementById("editDescription").value = bill.description;
  document.getElementById("editType").value = bill.bill_type;

  document.getElementById("editModal").classList.add("active");
}

document.getElementById("closeModal").addEventListener("click", closeModal);
document.getElementById("cancelModal").addEventListener("click", closeModal);
document.getElementById("editModal").addEventListener("click", e => {
  if (e.target === document.getElementById("editModal")) closeModal();
});

function closeModal() {
  document.getElementById("editModal").classList.remove("active");
}

document.getElementById("confirmEdit").addEventListener("click", async () => {
  const id = document.getElementById("editId").value;
  const payload = {
    amount: parseFloat(document.getElementById("editAmount").value),
    category: document.getElementById("editCategory").value,
    description: document.getElementById("editDescription").value,
    bill_type: document.getElementById("editType").value
  };

  const res = await apiFetch(`${API}/api/bills/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...payload, user_id: currentUserId })
  });
  const data = await res.json();
  if (data.success) {
    showToast("修改成功 ✅");
    closeModal();
    loadBills();
  }
});

// ========== 手动添加 ==========
function initAddForm() {
  const today = new Date().toISOString().split("T")[0];
  document.getElementById("addDate").value = today;
}

document.querySelectorAll(".form-tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".form-tab").forEach(t => t.classList.remove("active"));
    tab.classList.add("active");
    addBillType = tab.dataset.type;

    // 收入时切换分类选项
    const categorySelect = document.getElementById("addCategory");
    if (addBillType === "income") {
      categorySelect.innerHTML = `<option value="收入">💰 收入</option><option value="其他">📝 其他</option>`;
    } else {
      categorySelect.innerHTML = `
        <option value="餐饮">🍜 餐饮</option>
        <option value="交通">🚕 交通</option>
        <option value="购物">🛍️ 购物</option>
        <option value="娱乐">🎮 娱乐</option>
        <option value="居家">🏠 居家</option>
        <option value="医疗">💊 医疗</option>
        <option value="教育">📚 教育</option>
        <option value="人情">🧧 人情</option>
        <option value="其他">📝 其他</option>
      `;
    }
  });
});

document.getElementById("submitAdd").addEventListener("click", async () => {
  const amount = parseFloat(document.getElementById("addAmount").value);
  const category = document.getElementById("addCategory").value;
  const description = document.getElementById("addDescription").value.trim();
  const date = document.getElementById("addDate").value;

  if (!amount || amount <= 0) { showToast("请输入有效金额 ⚠️"); return; }
  if (!description) { showToast("请输入描述 ⚠️"); return; }

  const res = await apiFetch(`${API}/api/bills/add`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ amount, category, description, bill_type: addBillType, date, user_id: currentUserId })
  });
  const data = await res.json();
  if (data.success) {
    showToast("添加成功 ✅");
    document.getElementById("addAmount").value = "";
    document.getElementById("addDescription").value = "";
  }
});

// ========== 预算管理 ==========
async function loadBudget() {
  const [budgetRes, summaryRes] = await Promise.all([
    apiFetch(withUserId(`${API}/api/budget?year=${currentYear}&month=${currentMonth}`)),
    apiFetch(withUserId(`${API}/api/summary?year=${currentYear}&month=${currentMonth}`))
  ]);
  const budgetData = await budgetRes.json();
  const summaryData = await summaryRes.json();

  const budget = budgetData.budget || 0;
  const expense = summaryData.summary.expense || 0;

  document.getElementById("budgetInput").value = budget || "";
  document.getElementById("budgetAmount").textContent = formatAmount(budget);
  document.getElementById("budgetSpent").textContent = formatAmount(expense);
  document.getElementById("budgetRemain").textContent = formatAmount(budget - expense);

  // 圆形进度
  const circle = document.getElementById("budgetCircle");
  const percentEl = document.getElementById("budgetPercent");
  const circumference = 339.29;

  if (budget > 0) {
    const percent = Math.min(100, (expense / budget) * 100);
    const offset = circumference - (circumference * percent / 100);
    circle.style.strokeDashoffset = offset;
    circle.style.stroke = percent >= 90 ? "#ff6b6b" : "#6c63ff";
    percentEl.textContent = `${percent.toFixed(0)}%`;
  } else {
    circle.style.strokeDashoffset = circumference;
    percentEl.textContent = "0%";
  }
}

document.getElementById("saveBudget").addEventListener("click", async () => {
  const amount = parseFloat(document.getElementById("budgetInput").value);
  if (!amount || amount <= 0) { showToast("请输入有效预算金额 ⚠️"); return; }

  const res = await apiFetch(withUserId(`${API}/api/budget?year=${currentYear}&month=${currentMonth}`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ amount })
  });
  const data = await res.json();
  if (data.success) {
    showToast("预算保存成功 ✅");
    loadBudget();
  }
});

document.getElementById("refreshUsersBtn").addEventListener("click", loadUsers);

// ========== 初始化 ==========
initUserSwitcher();
initAdminAuth();
updateMonthLabel();
loadDashboard();
