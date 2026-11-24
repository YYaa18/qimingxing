(() => {
  const PROTOCOL_VERSION = "1.0";
  const EMOTION_META = {
    neutral: { emoji: "🙂", label: "平静 neutral" },
    happy: { emoji: "😄", label: "开心 happy" },
    thinking: { emoji: "🤔", label: "思考 thinking" },
    surprised: { emoji: "😲", label: "惊讶 surprised" },
    confused: { emoji: "😕", label: "疑惑 confused" },
    warning: { emoji: "⚠️", label: "警示 warning" }
  };

  const connDot = document.getElementById("connDot");
  const connText = document.getElementById("connText");
  const wsUrlInput = document.getElementById("wsUrlInput");
  const profileSelect = document.getElementById("profileSelect");
  const privacyBtns = document.querySelectorAll(".privacy-btn");
  const sensesBadge = document.getElementById("sensesBadge");
  const windowSummaryEl = document.getElementById("windowSummary");
  const captureIndicator = document.getElementById("captureIndicator");
  const captureLabel = document.getElementById("captureLabel");
  const toolStatusBar = document.getElementById("toolStatusBar");
  const toolStepBadge = document.getElementById("toolStepBadge");
  const toolStatusLabel = document.getElementById("toolStatusLabel");
  const emergencyStopBtn = document.getElementById("emergencyStopBtn");
  const permissionBanner = document.getElementById("permissionBanner");
  const permissionText = document.getElementById("permissionText");
  const usageAlertBanner = document.getElementById("usageAlertBanner");
  const usageAlertText = document.getElementById("usageAlertText");
  const dismissUsageBtn = document.getElementById("dismissUsageBtn");
  const proactiveNoticeBanner = document.getElementById("proactiveNoticeBanner");
  const proactiveNoticeText = document.getElementById("proactiveNoticeText");
  const dismissNoticeBtn = document.getElementById("dismissNoticeBtn");
  const chatPanel = document.querySelector(".chat-panel");
  const attachmentBar = document.getElementById("attachmentBar");
  const fileInput = document.getElementById("fileInput");
  const attachBtn = document.getElementById("attachBtn");
  const avatarFace = document.getElementById("avatarFace");
  const emotionBadge = document.getElementById("emotionBadge");
  const activityText = document.getElementById("activityText");
  const emotionGrid = document.getElementById("emotionGrid");
  const errorBanner = document.getElementById("errorBanner");
  const errorText = document.getElementById("errorText");
  const dismissErrorBtn = document.getElementById("dismissErrorBtn");
  const messagesEl = document.getElementById("messages");
  const chatForm = document.getElementById("chatForm");
  const chatInput = document.getElementById("chatInput");
  const sendBtn = document.getElementById("sendBtn");
  const askScreenBtn = document.getElementById("askScreenBtn");
  const stopBtn = document.getElementById("stopBtn");
  const clearChatBtn = document.getElementById("clearChatBtn");
  const cardsList = document.getElementById("cardsList");
  const clearCardsBtn = document.getElementById("clearCardsBtn");

  let ws = null;
  let reconnectAttempt = 0;
  let reconnectTimer = null;
  let pingTimer = null;
  let activeTurnId = null;
  let isGenerating = false;
  const bubbleByTurn = new Map();
  const pendingApprovalCleanups = new Map();
  let pendingAttachments = [];

  function renderAttachmentBar() {
    if (!attachmentBar) return;
    attachmentBar.innerHTML = "";
    if (!pendingAttachments.length) {
      attachmentBar.classList.remove("visible");
      return;
    }
    attachmentBar.classList.add("visible");
    for (const att of pendingAttachments) {
      const chip = document.createElement("span");
      chip.className = "attachment-chip";
      const icon = att.kind === "image" ? "🖼️" : att.kind === "pdf" ? "📄" : "📝";
      const label = document.createElement("span");
      label.textContent = `${icon} ${att.filename || att.artifact_id}`;
      const rmBtn = document.createElement("button");
      rmBtn.type = "button";
      rmBtn.title = "移除附件";
      rmBtn.textContent = "×";
      rmBtn.addEventListener("click", () => {
        pendingAttachments = pendingAttachments.filter((item) => item.artifact_id !== att.artifact_id);
        renderAttachmentBar();
      });
      chip.appendChild(label);
      chip.appendChild(rmBtn);
      attachmentBar.appendChild(chip);
    }
  }

  async function uploadAttachment(file) {
    if (!file) return;
    const form = new FormData();
    form.append("file", file, file.name || "pasted_file.png");
    try {
      const resp = await fetch("/api/artifacts", { method: "POST", body: form });
      if (!resp.ok) {
        let detail = `HTTP ${resp.status}`;
        try {
          const errJson = await resp.json();
          if (errJson && errJson.detail) detail = errJson.detail;
        } catch (_ignored) {}
        showError(`上传附件失败：${detail}`);
        return;
      }
      const data = await resp.json();
      pendingAttachments.push({
        artifact_id: data.artifact_id,
        kind: data.kind || "text",
        filename: data.filename || file.name || "attachment"
      });
      renderAttachmentBar();
      hideError();
    } catch (err) {
      showError(`上传附件异常：${err && err.message ? err.message : "网络错误"}`);
    }
  }

  function showProactiveNotice(payload) {
    if (!proactiveNoticeBanner) return;
    const badge = payload.kind === "llm_summary" ? "📊 定时摘要" : "⏰ 定时提醒";
    proactiveNoticeText.textContent = `${badge}【${payload.title || "启明星"}】${payload.content || ""}`;
    proactiveNoticeBanner.classList.add("visible");
    if ("Notification" in window && Notification.permission === "granted") {
      try {
        new Notification(payload.title || "启明星提醒", { body: payload.content || "" });
      } catch (_ignored) {}
    }
  }

  function hideProactiveNotice() {
    if (!proactiveNoticeBanner) return;
    proactiveNoticeBanner.classList.remove("visible");
  }

  function initEmotionGrid() {
    emotionGrid.innerHTML = "";
    for (const [key, meta] of Object.entries(EMOTION_META)) {
      const el = document.createElement("div");
      el.className = "emotion-chip" + (key === "neutral" ? " active" : "");
      el.dataset.emotion = key;
      el.textContent = `${meta.emoji} ${key}`;
      emotionGrid.appendChild(el);
    }
  }

  function makeId(prefix = "evt") {
    const rand = Math.random().toString(16).slice(2, 10);
    return `${prefix}_${Date.now().toString(16)}_${rand}`;
  }

  function sendEnvelope(type, payload = {}, turnId = null) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    const env = {
      v: PROTOCOL_VERSION,
      id: makeId("evt"),
      ts: Date.now(),
      source: "body",
      type,
      payload
    };
    if (turnId) env.turn_id = turnId;
    ws.send(JSON.stringify(env));
    return true;
  }

  function setConnectionUI(state, detail) {
    connDot.className = "conn-dot " + state;
    connText.textContent = detail;
  }

  function updateAvatar(emotion, activity) {
    const safeEmotion = EMOTION_META[emotion] ? emotion : "neutral";
    const meta = EMOTION_META[safeEmotion];
    avatarFace.textContent = meta.emoji;
    emotionBadge.textContent = meta.label;
    activityText.textContent = `状态：${activity || "idle"}`;
    for (const chip of emotionGrid.querySelectorAll(".emotion-chip")) {
      chip.classList.toggle("active", chip.dataset.emotion === safeEmotion);
    }
  }

  function updatePrivacyUI(payload) {
    const mode = payload.mode || "meta_only";
    for (const btn of privacyBtns) {
      btn.classList.toggle("active", btn.dataset.mode === mode);
    }

    const online = Boolean(payload.senses_connected);
    sensesBadge.className = "senses-badge " + (online ? "online" : "offline");
    const plat = payload.platform && payload.platform !== "unknown" ? ` (${payload.platform})` : "";
    sensesBadge.textContent = online ? `桌面感官在线${plat}` : "桌面感官未连接";

    if (mode === "off") {
      windowSummaryEl.textContent = "已关闭桌面感知（不会读取或上报窗口信息）";
    } else if (payload.active_window_summary) {
      windowSummaryEl.textContent = `当前感知：${payload.active_window_summary}`;
    } else {
      windowSummaryEl.textContent = online ? "等待切换工作窗口..." : "未收到窗口上下文";
    }

    if (payload.permission_hint && online && mode === "full_on_demand") {
      permissionText.textContent = `系统权限提示：${payload.permission_hint}`;
      permissionBanner.classList.add("visible");
    } else {
      permissionBanner.classList.remove("visible");
    }
  }

  function setCaptureIndicator(visible, label = "正在查看当前桌面…") {
    captureLabel.textContent = label;
    captureIndicator.classList.toggle("visible", visible);
  }

  function setToolBar(visible, toolName = "", label = "", step = null, maxSteps = null) {
    if (!toolStatusBar) return;
    if (!visible) {
      toolStatusBar.classList.remove("visible");
      if (emergencyStopBtn) emergencyStopBtn.hidden = true;
      return;
    }
    const stepText = step && maxSteps ? `步骤 ${step}/${maxSteps}` : "工具执行";
    toolStepBadge.textContent = `${toolName || "tool"} · ${stepText}`;
    toolStatusLabel.textContent = label || "执行中…";
    toolStatusBar.classList.add("visible");
    if (emergencyStopBtn) emergencyStopBtn.hidden = false;
  }

  function showUsageAlert(msg) {
    if (!usageAlertBanner) return;
    usageAlertText.textContent = msg;
    usageAlertBanner.classList.add("visible");
  }

  function hideUsageAlert() {
    if (!usageAlertBanner) return;
    usageAlertBanner.classList.remove("visible");
  }

  function settlePendingApprovals(reasonLabel) {
    for (const [aid, finishFn] of pendingApprovalCleanups.entries()) {
      finishFn(reasonLabel);
      pendingApprovalCleanups.delete(aid);
    }
  }

  function renderApprovalCard(payload, turnId) {
    clearEmptyHint(messagesEl);
    const approvalId = payload.approval_id;
    const timeoutSec = Math.max(5, Number(payload.timeout_sec || 60));
    let remaining = timeoutSec;

    const card = document.createElement("div");
    card.className = "approval-card";

    const head = document.createElement("div");
    head.className = "approval-head";
    const titleSpan = document.createElement("span");
    titleSpan.textContent = `🔐 需要确认操作：${payload.tool_name || "tool"}`;
    const timerSpan = document.createElement("span");
    timerSpan.className = "approval-timer";
    timerSpan.textContent = `剩余 ${remaining}s`;
    head.appendChild(titleSpan);
    head.appendChild(timerSpan);

    const summaryEl = document.createElement("div");
    summaryEl.className = "approval-summary";
    summaryEl.textContent = payload.summary || "请确认是否允许执行该操作";

    const detailsObj = payload.details || {};
    let detailsText;
    if (detailsObj.command) {
      detailsText = `命令: ${detailsObj.command}\n目录: ${detailsObj.cwd || "~"}`;
    } else if (detailsObj.server && detailsObj.tool) {
      detailsText = `MCP 服务: ${detailsObj.server}\n工具: ${detailsObj.tool}\n参数: ${JSON.stringify(detailsObj.arguments || {}, null, 2)}`;
    } else {
      detailsText = JSON.stringify(detailsObj, null, 2);
    }
    const detailsEl = document.createElement("div");
    detailsEl.className = "approval-details";
    detailsEl.textContent = detailsText;

    const actionsRow = document.createElement("div");
    actionsRow.className = "approval-actions";

    const approveBtn = document.createElement("button");
    approveBtn.type = "button";
    approveBtn.className = "btn";
    approveBtn.textContent = "允许执行";

    const rememberBtn = document.createElement("button");
    rememberBtn.type = "button";
    rememberBtn.className = "btn btn-secondary";
    rememberBtn.textContent = "本会话记住并允许";

    const denyBtn = document.createElement("button");
    denyBtn.type = "button";
    denyBtn.className = "btn btn-danger";
    denyBtn.textContent = "拒绝";

    const resultBadge = document.createElement("span");
    resultBadge.className = "approval-result";

    let timerId = null;
    const lockCard = (statusLabel) => {
      if (timerId) {
        clearInterval(timerId);
        timerId = null;
      }
      approveBtn.disabled = true;
      rememberBtn.disabled = true;
      denyBtn.disabled = true;
      timerSpan.textContent = statusLabel;
      resultBadge.textContent = statusLabel;
    };

    pendingApprovalCleanups.set(approvalId, lockCard);

    const submitDecision = (approved, rememberForSession, statusLabel) => {
      pendingApprovalCleanups.delete(approvalId);
      lockCard(statusLabel);
      sendEnvelope(
        "tool.approval_response",
        {
          approval_id: approvalId,
          turn_id: turnId || payload.turn_id || activeTurnId || "",
          approved,
          remember_for_session: rememberForSession
        },
        turnId || activeTurnId
      );
    };

    approveBtn.addEventListener("click", () => submitDecision(true, false, "✅ 已允许执行"));
    rememberBtn.addEventListener("click", () =>
      submitDecision(true, true, "✅ 已允许（本会话记住）")
    );
    denyBtn.addEventListener("click", () => submitDecision(false, false, "⛔ 已拒绝"));

    timerId = setInterval(() => {
      remaining -= 1;
      if (remaining <= 0) {
        pendingApprovalCleanups.delete(approvalId);
        lockCard("⌛ 审批已超时拒绝");
      } else {
        timerSpan.textContent = `剩余 ${remaining}s`;
      }
    }, 1000);

    const allowRemember = detailsObj.allow_remember !== false;
    actionsRow.appendChild(approveBtn);
    if (allowRemember) {
      actionsRow.appendChild(rememberBtn);
    } else {
      rememberBtn.disabled = true;
    }
    actionsRow.appendChild(denyBtn);
    actionsRow.appendChild(resultBadge);

    card.appendChild(head);
    card.appendChild(summaryEl);
    card.appendChild(detailsEl);
    card.appendChild(actionsRow);
    messagesEl.appendChild(card);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function setGenerating(flag) {
    isGenerating = flag;
    stopBtn.disabled = !flag;
    chatInput.disabled = false;
    sendBtn.disabled = false;
    askScreenBtn.disabled = false;
    if (!flag) {
      setCaptureIndicator(false);
      setToolBar(false);
      settlePendingApprovals("已结束");
    } else if (emergencyStopBtn) {
      emergencyStopBtn.hidden = false;
    }
  }

  function showError(msg) {
    errorText.textContent = msg;
    errorBanner.classList.add("visible");
  }

  function hideError() {
    errorBanner.classList.remove("visible");
  }

  function clearEmptyHint(container) {
    const hint = container.querySelector(".empty-hint");
    if (hint) hint.remove();
  }

  function renderInitialHint() {
    messagesEl.innerHTML =
      '<div class="empty-hint">试试直接和启明星对话，或开启顶部「按需看屏」后提问：<br/>• 日常伴随：我刚才这十几分钟主要在忙什么？<br/>• 意图看屏：帮我看看屏幕上这个报错怎么修？<br/>• 手动兜底：点击输入框右侧「附带屏幕提问」</div>';
  }

  function appendUserBubble(text) {
    clearEmptyHint(messagesEl);
    const div = document.createElement("div");
    div.className = "bubble user";
    div.textContent = text;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function ensureAssistantBubble(turnId, metaText = "启明星") {
    if (bubbleByTurn.has(turnId)) return bubbleByTurn.get(turnId);
    clearEmptyHint(messagesEl);
    const wrapper = document.createElement("div");
    wrapper.className = "bubble assistant";

    const meta = document.createElement("div");
    meta.className = "bubble-meta";
    meta.textContent = metaText;

    const content = document.createElement("div");
    content.textContent = "";

    wrapper.appendChild(meta);
    wrapper.appendChild(content);
    messagesEl.appendChild(wrapper);
    messagesEl.scrollTop = messagesEl.scrollHeight;

    const record = { wrapper, meta, content };
    bubbleByTurn.set(turnId, record);
    return record;
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function renderInlineMd(escaped) {
    return escaped
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>");
  }

  function renderMarkdownSafe(md) {
    const lines = String(md || "").replace(/\r\n/g, "\n").split("\n");
    const html = [];
    let inCode = false;
    let inList = false;
    let tableRows = [];

    const flushList = () => {
      if (inList) {
        html.push("</ul>");
        inList = false;
      }
    };
    const flushTable = () => {
      if (!tableRows.length) return;
      html.push("<table><thead><tr>");
      const headers = tableRows[0];
      for (const h of headers) html.push(`<th>${renderInlineMd(escapeHtml(h))}</th>`);
      html.push("</tr></thead><tbody>");
      for (let i = 1; i < tableRows.length; i++) {
        const row = tableRows[i];
        if (row.every((c) => /^:?-+:?$/.test(c.trim()))) continue;
        html.push("<tr>");
        for (const c of row) html.push(`<td>${renderInlineMd(escapeHtml(c))}</td>`);
        html.push("</tr>");
      }
      html.push("</tbody></table>");
      tableRows = [];
    };

    for (const line of lines) {
      if (line.trim().startsWith("```")) {
        flushList();
        flushTable();
        if (!inCode) {
          inCode = true;
          html.push("<pre><code>");
        } else {
          inCode = false;
          html.push("</code></pre>");
        }
        continue;
      }
      if (inCode) {
        html.push(escapeHtml(line) + "\n");
        continue;
      }
      if (line.trim().startsWith("|") && line.trim().endsWith("|")) {
        flushList();
        const cells = line.trim().slice(1, -1).split("|").map((s) => s.trim());
        tableRows.push(cells);
        continue;
      } else {
        flushTable();
      }
      const headingMatch = line.match(/^(#{1,3})\s+(.*)$/);
      if (headingMatch) {
        flushList();
        const lvl = headingMatch[1].length + 2;
        html.push(`<h${lvl} style="margin:6px 0">${renderInlineMd(escapeHtml(headingMatch[2]))}</h${lvl}>`);
        continue;
      }
      if (/^[-*]\s+/.test(line.trim())) {
        if (!inList) {
          inList = true;
          html.push("<ul style='margin:6px 0;padding-left:18px'>");
        }
        html.push(`<li>${renderInlineMd(escapeHtml(line.trim().replace(/^[-*]\s+/, "")))}</li>`);
        continue;
      }
      flushList();
      if (line.trim()) {
        html.push(`<p style="margin:6px 0">${renderInlineMd(escapeHtml(line))}</p>`);
      }
    }
    flushList();
    flushTable();
    if (inCode) html.push("</code></pre>");
    return html.join("");
  }

  function renderCard(payload) {
    clearEmptyHint(cardsList);
    const cardEl = document.createElement("div");
    cardEl.className = "aux-card " + (payload.card_type || "markdown");
    cardEl.dataset.cardId = payload.card_id || "";

    const head = document.createElement("div");
    head.className = "aux-card-head";

    const title = document.createElement("div");
    title.className = "aux-card-title";
    title.textContent = payload.title || "辅助卡片";

    const typeBadge = document.createElement("span");
    typeBadge.className = "aux-card-type";
    typeBadge.textContent = payload.card_type || "markdown";

    head.appendChild(title);
    head.appendChild(typeBadge);

    const body = document.createElement("div");
    body.className = "aux-card-body";
    body.innerHTML = renderMarkdownSafe(payload.content_markdown || "");

    const actionsBar = document.createElement("div");
    actionsBar.className = "aux-card-actions";

    const actions = Array.isArray(payload.actions) && payload.actions.length
      ? payload.actions
      : [{ id: "dismiss", label: "知道了，收起" }];

    for (const act of actions) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn-ghost";
      btn.textContent = act.label || act.id;
      btn.addEventListener("click", () => {
        if (act.id === "dismiss") {
          const collapsed = body.classList.toggle("collapsed");
          btn.textContent = collapsed ? "展开内容" : (act.label || "知道了，收起");
        } else {
          if (activeTurnId && isGenerating) cancelActiveTurn("new_action");
          const newTurn = makeId("turn");
          activeTurnId = newTurn;
          setGenerating(true);
          sendEnvelope("input.action", { action: act.id, card_id: payload.card_id }, newTurn);
        }
      });
      actionsBar.appendChild(btn);
    }

    cardEl.appendChild(head);
    cardEl.appendChild(body);
    cardEl.appendChild(actionsBar);
    cardsList.prepend(cardEl);
  }

  function handleWelcome(payload) {
    profileSelect.innerHTML = "";
    const profiles = Array.isArray(payload.profiles) ? payload.profiles : [];
    for (const p of profiles) {
      const opt = document.createElement("option");
      opt.value = p.name;
      const visionBadge = p.supports_vision ? " · 👁️视觉" : "";
      opt.textContent = `${p.label || p.name} (${p.model || p.provider}${visionBadge})`;
      if (p.name === payload.active_profile) opt.selected = true;
      profileSelect.appendChild(opt);
    }
  }

  function handleMessage(raw) {
    let env;
    try {
      env = JSON.parse(raw);
    } catch (err) {
      console.debug("Ignoring malformed JSON frame", err);
      return;
    }
    const type = env.type;
    const payload = env.payload || {};
    const turnId = env.turn_id || null;

    if (type === "system.welcome") {
      handleWelcome(payload);
      return;
    }
    if (type === "system.privacy_state") {
      updatePrivacyUI(payload);
      return;
    }
    if (type === "system.pong") {
      return;
    }
    if (type === "system.error") {
      if (payload.code === "USAGE_SOFT_ALERT") {
        showUsageAlert(payload.message || "已触发用量软提醒，可继续使用。");
        return;
      }
      showError(`[${payload.code || "ERROR"}] ${payload.message || "未知错误"}`);
      setGenerating(false);
      updateAvatar("warning", "idle");
      return;
    }

    if (turnId && activeTurnId && turnId !== activeTurnId) {
      console.debug("Dropping stale event for old turn_id:", turnId, type);
      return;
    }

    if (type === "output.turn_start") {
      hideError();
      setGenerating(true);
      if (turnId) {
        ensureAssistantBubble(turnId, `启明星 · ${payload.profile || ""} (${payload.model || ""})`);
      }
      return;
    }
    if (type === "avatar.state") {
      updateAvatar(payload.emotion, payload.activity);
      return;
    }
    if (type === "output.text_delta") {
      setCaptureIndicator(false);
      if (turnId && payload.delta) {
        const rec = ensureAssistantBubble(turnId);
        rec.content.textContent += payload.delta;
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }
      return;
    }
    if (type === "output.card") {
      renderCard(payload);
      return;
    }
    if (type === "output.turn_end") {
      setGenerating(false);
      if (payload.finish_reason === "cancelled" && turnId) {
        const rec = bubbleByTurn.get(turnId);
        if (rec) rec.meta.textContent += " · [已打断]";
      }
      return;
    }
    if (type === "output.tool_status") {
      activityText.textContent = `状态：${payload.label || payload.tool || "tool_running"}`;
      if (payload.status === "running" && (payload.tool === "screen_capture" || payload.tool === "deepseek_ocr")) {
        setCaptureIndicator(true, payload.label || "正在查看当前桌面…");
      } else {
        setCaptureIndicator(false);
      }
      setToolBar(true, payload.tool, payload.label, payload.step, payload.max_steps);
      return;
    }
    if (type === "tool.approval_request") {
      if (emergencyStopBtn) emergencyStopBtn.hidden = false;
      renderApprovalCard(payload, turnId);
      return;
    }
    if (type === "output.proactive_notice") {
      showProactiveNotice(payload);
      return;
    }

    console.debug("Ignoring unrecognized event type:", type, env);
  }

  function cancelActiveTurn(reason = "user_cancelled") {
    if (!activeTurnId) return;
    const cancellingTurn = activeTurnId;
    settlePendingApprovals("🚨 已急停取消");
    sendEnvelope("input.cancel", { reason }, cancellingTurn);
    const rec = bubbleByTurn.get(cancellingTurn);
    if (rec) rec.meta.textContent += " · [已打断]";
    activeTurnId = makeId("turn");
    setGenerating(false);
    updateAvatar("neutral", "idle");
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectAttempt += 1;
    const delayMs = Math.min(1000 * Math.pow(2, reconnectAttempt - 1), 5000);
    setConnectionUI("disconnected", `已断开，${(delayMs / 1000).toFixed(1)}s 后重连...`);
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delayMs);
  }

  function connect() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      ws.close();
    }
    const url = wsUrlInput.value.trim() || "ws://127.0.0.1:18790/ws";
    setConnectionUI("", "连接中...");
    ws = new WebSocket(url);

    ws.onopen = () => {
      reconnectAttempt = 0;
      setConnectionUI("connected", "已连接 Brain");
      sendEnvelope("system.hello", {
        client_name: "simple-dialogue-box",
        role: "body",
        capabilities: {
          avatar_mode: "simple_2d",
          supported_emotions: Object.keys(EMOTION_META),
          streaming_text: true,
          ui_cards: ["markdown", "spoiler_steps"],
          audio_output: false,
          lip_sync: false
        }
      });
      if (pingTimer) clearInterval(pingTimer);
      pingTimer = setInterval(() => {
        sendEnvelope("system.ping", { client_ts: Date.now() });
      }, 15000);
    };

    ws.onmessage = (event) => handleMessage(event.data);

    ws.onerror = () => {
      setConnectionUI("disconnected", "连接异常");
    };

    ws.onclose = () => {
      if (pingTimer) {
        clearInterval(pingTimer);
        pingTimer = null;
      }
      setGenerating(false);
      scheduleReconnect();
    };
  }

  for (const btn of privacyBtns) {
    btn.addEventListener("click", () => {
      const mode = btn.dataset.mode;
      if (mode) {
        sendEnvelope("system.privacy_set", { mode });
      }
    });
  }

  profileSelect.addEventListener("change", () => {
    if (profileSelect.value) {
      sendEnvelope("system.config_set", { profile: profileSelect.value });
    }
  });

  stopBtn.addEventListener("click", () => {
    cancelActiveTurn("stop_button");
  });

  if (emergencyStopBtn) {
    emergencyStopBtn.addEventListener("click", () => {
      cancelActiveTurn("kill_switch");
    });
  }

  if (dismissUsageBtn) {
    dismissUsageBtn.addEventListener("click", hideUsageAlert);
  }

  if (dismissNoticeBtn) {
    dismissNoticeBtn.addEventListener("click", hideProactiveNotice);
  }

  if (attachBtn && fileInput) {
    attachBtn.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      for (const f of Array.from(fileInput.files || [])) {
        uploadAttachment(f);
      }
      fileInput.value = "";
    });
  }

  if (chatPanel) {
    chatPanel.addEventListener("dragover", (e) => {
      e.preventDefault();
      chatPanel.classList.add("drag-over");
    });
    chatPanel.addEventListener("dragleave", () => {
      chatPanel.classList.remove("drag-over");
    });
    chatPanel.addEventListener("drop", (e) => {
      e.preventDefault();
      chatPanel.classList.remove("drag-over");
      const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
      for (const f of files) uploadAttachment(f);
    });
  }

  chatInput.addEventListener("paste", (e) => {
    const items = Array.from((e.clipboardData && e.clipboardData.items) || []);
    const fileItems = items.filter((it) => it.kind === "file");
    if (fileItems.length > 0) {
      e.preventDefault();
      for (const it of fileItems) {
        const f = it.getAsFile();
        if (f) uploadAttachment(f);
      }
    }
  });

  clearChatBtn.addEventListener("click", () => {
    if (isGenerating && activeTurnId) {
      cancelActiveTurn("cleared_history");
    }
    bubbleByTurn.clear();
    renderInitialHint();
    sendEnvelope("input.action", { action: "clear_history" });
  });

  askScreenBtn.addEventListener("click", () => {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      showError("尚未连接到 Brain WebSocket 服务，请等待自动重连。");
      return;
    }
    const raw = chatInput.value.trim();
    const text = raw || "帮我看看当前屏幕上的内容";
    if (isGenerating && activeTurnId) {
      cancelActiveTurn("superseded_by_screen_action");
    }
    chatInput.value = "";
    appendUserBubble(`[附带屏幕] ${text}`);
    const turnId = makeId("turn");
    activeTurnId = turnId;
    setGenerating(true);
    sendEnvelope("input.action", { action: "ask_with_screen", data: { text } }, turnId);
  });

  dismissErrorBtn.addEventListener("click", hideError);

  clearCardsBtn.addEventListener("click", () => {
    cardsList.innerHTML = '<div class="empty-hint">已清空辅助卡片。</div>';
  });

  wsUrlInput.addEventListener("change", () => {
    reconnectAttempt = 0;
    connect();
  });

  chatForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const rawText = chatInput.value.trim();
    if (!rawText && pendingAttachments.length === 0) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      showError("尚未连接到 Brain WebSocket 服务，请等待自动重连。");
      return;
    }
    if (isGenerating && activeTurnId) {
      cancelActiveTurn("superseded_by_new_input");
    }
    const text = rawText || "请分析并总结我发送的附件内容。";
    const attIds = pendingAttachments.map((a) => a.artifact_id);
    const attNames = pendingAttachments.map((a) => `[附件:${a.filename}]`).join(" ");
    const displayBubble = attNames ? `${attNames}\n${text}` : text;

    chatInput.value = "";
    pendingAttachments = [];
    renderAttachmentBar();
    appendUserBubble(displayBubble);

    const turnId = makeId("turn");
    activeTurnId = turnId;
    setGenerating(true);
    const payload = attIds.length ? { text, attachments: attIds } : { text };
    sendEnvelope("input.text", payload, turnId);
  });

  initEmotionGrid();
  renderInitialHint();
  connect();
})();
