(function () {
  "use strict";

  const body = document.body;
  const role = body.dataset.currentRole || "GUEST";
  const path = body.dataset.requestPath || window.location.pathname;

  const isStorePage = role === "STORE" && path.startsWith("/store");
  const isDriverPage = role === "DRIVER" && path.startsWith("/driver");

  if (!isStorePage && !isDriverPage) {
    return;
  }

  const endpoint = isStorePage ? "/store/realtime/status" : "/driver/realtime/status";
  const storageKey = isStorePage ? "fugo_store_realtime_state" : "fugo_driver_realtime_state";
  const dismissedKey = isStorePage ? "fugo_store_realtime_dismissed" : "fugo_driver_realtime_dismissed";
  const bellEnabledKey = "fugo_big_bell_enabled";

  const VISIBLE_POLL_MS = 5000;
  const HIDDEN_POLL_MS = 45000;
  const IDLE_POLL_MS = 30000;
  const OFFLINE_DRIVER_POLL_MS = 30000;
  const FETCH_TIMEOUT_MS = 8000;
  const MAX_RING_MS = 90000;
  const SNOOZE_MS = 120000;

  const banner = document.getElementById("realtime-banner");
  const titleEl = document.getElementById("realtime-title");
  const messageEl = document.getElementById("realtime-message");
  const linkEl = document.getElementById("realtime-link");
  const stopBtn = document.getElementById("realtime-stop");

  const enableWrap = document.getElementById("realtime-enable-wrap");
  const enableBtn = document.getElementById("realtime-enable");
  const testBtn = document.getElementById("realtime-test");

  const controlPanel = document.getElementById("realtime-control-panel");
  const controlStatus = document.getElementById("realtime-control-status");
  const controlEnableBtn = document.getElementById("realtime-control-enable");
  const controlTestBtn = document.getElementById("realtime-control-test");

  const overlay = document.getElementById("bigRingbellOverlay");
  const bigTitle = document.getElementById("bigRingTitle");
  const bigSubtitle = document.getElementById("bigRingSubtitle");
  const bigIcon = document.getElementById("bigRingIcon");

  const bigCountLabel = document.getElementById("bigRingCountLabel");
  const bigCount = document.getElementById("bigRingCount");
  const bigOrderCode = document.getElementById("bigRingOrderCode");
  const bigExtraLabel = document.getElementById("bigRingExtraLabel");
  const bigExtra = document.getElementById("bigRingExtra");
  const bigMetaLabel = document.getElementById("bigRingMetaLabel");
  const bigMeta = document.getElementById("bigRingMeta");

  const bigAudioWarning = document.getElementById("bigRingAudioWarning");
  const bigPrimaryBtn = document.getElementById("bigRingPrimaryBtn");
  const bigStopBtn = document.getElementById("bigRingStopBtn");
  const bigLaterBtn = document.getElementById("bigRingLaterBtn");

  let audioCtx = null;
  let ringTimer = null;
  let ringStopAt = 0;
  let currentSignature = "";
  let lastSignature = readState();
  let firstPoll = true;

  let pollTimer = null;
  let inFlightController = null;
  let lastPayload = null;
  let stopped = false;

  injectRingbellProStyle();
  ensureDriverDetailPanel();

  function readState() {
    try {
      return localStorage.getItem(storageKey) || "";
    } catch (e) {
      return "";
    }
  }

  function writeState(value) {
    try {
      localStorage.setItem(storageKey, value || "");
    } catch (e) {}
  }

  function readDismissed() {
    try {
      const raw = localStorage.getItem(dismissedKey) || "";
      if (!raw) return null;
      return JSON.parse(raw);
    } catch (e) {
      return null;
    }
  }

  function writeDismissed(signature) {
    try {
      localStorage.setItem(
        dismissedKey,
        JSON.stringify({
          signature: signature || "",
          at: Date.now()
        })
      );
    } catch (e) {}
  }

  function isDismissedRecently(signature) {
    const data = readDismissed();

    if (!data || !data.signature || !signature) {
      return false;
    }

    if (data.signature !== signature) {
      return false;
    }

    return Date.now() - Number(data.at || 0) < SNOOZE_MS;
  }

  function isBellEnabled() {
    try {
      return localStorage.getItem(bellEnabledKey) === "1";
    } catch (e) {
      return false;
    }
  }

  function setBellEnabled(value) {
    try {
      localStorage.setItem(bellEnabledKey, value ? "1" : "0");
    } catch (e) {}
  }

  function updateEnableUI() {
    const enabled = isBellEnabled();

    if (enableWrap) {
      enableWrap.hidden = enabled;
    }

    if (controlPanel) {
      controlPanel.hidden = false;
    }

    if (controlStatus) {
      controlStatus.textContent = enabled
        ? "已啟用大聲通知。新訂單會顯示大畫面並播放大聲鈴聲。"
        : "尚未啟用。為了避免漏接訂單，請先啟用鈴聲。";
    }

    if (bigAudioWarning) {
      bigAudioWarning.hidden = enabled;
    }
  }

  function ensureAudioContext() {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;

    if (!AudioContextClass) {
      return null;
    }

    if (!audioCtx) {
      audioCtx = new AudioContextClass();
    }

    if (audioCtx.state === "suspended") {
      audioCtx.resume().catch(function () {});
    }

    return audioCtx;
  }

  function playTone(freq, startOffset, duration, gainValue, type) {
    const ctx = ensureAudioContext();

    if (!ctx) {
      return;
    }

    const osc = ctx.createOscillator();
    const gain = ctx.createGain();

    osc.type = type || "square";
    osc.frequency.value = freq;

    const start = ctx.currentTime + startOffset;
    const end = start + duration;

    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(gainValue, start + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.0001, end);

    osc.connect(gain);
    gain.connect(ctx.destination);

    osc.start(start);
    osc.stop(end + 0.03);
  }

  function playLoudPattern() {
    const isDriver = role === "DRIVER";

    if (isDriver) {
      playTone(1040, 0.00, 0.18, 0.48, "square");
      playTone(780, 0.20, 0.18, 0.42, "square");
      playTone(1040, 0.42, 0.20, 0.48, "square");
      playTone(520, 0.68, 0.24, 0.40, "sawtooth");
    } else {
      playTone(880, 0.00, 0.18, 0.42, "square");
      playTone(660, 0.20, 0.18, 0.38, "square");
      playTone(1040, 0.42, 0.20, 0.42, "square");
      playTone(660, 0.68, 0.22, 0.35, "sawtooth");
    }
  }

  function vibrateAlert() {
    if (!navigator.vibrate) {
      return;
    }

    try {
      navigator.vibrate([500, 160, 500, 160, 900]);
    } catch (e) {}
  }

  function stopBell() {
    if (ringTimer) {
      clearInterval(ringTimer);
      ringTimer = null;
    }

    ringStopAt = 0;

    if (navigator.vibrate) {
      try {
        navigator.vibrate(0);
      } catch (e) {}
    }
  }

  function startBell() {
    if (!isBellEnabled()) {
      updateEnableUI();
      return;
    }

    ensureAudioContext();
    stopBell();

    ringStopAt = Date.now() + MAX_RING_MS;
    playLoudPattern();
    vibrateAlert();

    ringTimer = setInterval(function () {
      if (Date.now() >= ringStopAt) {
        stopBell();
        return;
      }

      playLoudPattern();
      vibrateAlert();
    }, 1200);
  }

  function testBell() {
    setBellEnabled(true);
    ensureAudioContext();
    updateEnableUI();

    playLoudPattern();

    setTimeout(function () {
      playLoudPattern();
    }, 1200);

    vibrateAlert();
  }

  function enableBell() {
    setBellEnabled(true);
    ensureAudioContext();
    updateEnableUI();
    testBell();
  }

  function moneyText(value) {
    const n = Number(value || 0);
    return `NT$${n}`;
  }

  function kmText(value) {
    const raw = String(value || "").trim();

    if (!raw) {
      return "-";
    }

    const n = Number(raw);

    if (!Number.isFinite(n) || n <= 0) {
      return "-";
    }

    return `${n.toFixed(1)} km`;
  }

  function safeText(value, fallback) {
    const text = String(value || "").trim();
    return text || fallback || "-";
  }

  function buildSignature(data) {
    if (!data || !data.ok || !data.role) {
      return "";
    }

    if (data.role === "STORE") {
      return [
        "STORE",
        data.new_orders || 0,
        data.latest_order_code || "",
        data.latest_total_twd || 0,
        data.latest_payment_method || ""
      ].join("|");
    }

    if (data.role === "DRIVER") {
      return [
        "DRIVER",
        data.is_online ? "1" : "0",
        data.available_orders || data.waiting_orders || 0,
        data.latest_order_code || "",
        data.latest_store_name || "",
        data.latest_store_address || "",
        data.latest_delivery_address || "",
        data.city_block || ""
      ].join("|");
    }

    return "";
  }

  function buildTargetUrl(data) {
    if (!data || !data.role) {
      return "#";
    }

    if (data.target_url) {
      return data.target_url;
    }

    if (data.role === "STORE") {
      const code = safeText(data.latest_order_code, "");
      return code ? `/store#${encodeURIComponent(code)}` : "/store";
    }

    if (data.role === "DRIVER") {
      const code = safeText(data.latest_order_code, "");
      return code ? `/driver#${encodeURIComponent(code)}` : "/driver";
    }

    return "#";
  }

  function ensureDriverDetailPanel() {
    if (!overlay) {
      return null;
    }

    let panel = overlay.querySelector("[data-ring-detail-panel='1']");

    if (panel) {
      return panel;
    }

    panel = document.createElement("div");
    panel.setAttribute("data-ring-detail-panel", "1");
    panel.className = "ring-detail-panel";
    panel.hidden = true;

    if (bigSubtitle && bigSubtitle.parentNode) {
      bigSubtitle.parentNode.insertBefore(panel, bigSubtitle.nextSibling);
    } else {
      overlay.appendChild(panel);
    }

    return panel;
  }

  function ringRow(label, value, strong) {
    return `
      <div class="ring-info-row${strong ? " strong" : ""}">
        <span>${label}</span>
        <b>${safeText(value, "-")}</b>
      </div>
    `;
  }

  function fillStoreOverlay(data, targetUrl) {
    const panel = ensureDriverDetailPanel();

    if (panel) {
      panel.hidden = true;
      panel.innerHTML = "";
    }

    overlay.classList.add("store-alert");
    overlay.classList.remove("driver-alert");

    if (bigIcon) {
      bigIcon.textContent = "單";
    }

    if (bigTitle) {
      bigTitle.textContent = "有新訂單";
    }

    if (bigSubtitle) {
      bigSubtitle.textContent = "店家有新的訂單需要處理";
    }

    if (bigCountLabel) {
      bigCountLabel.textContent = "新訂單";
    }

    if (bigCount) {
      bigCount.textContent = `${Number(data.new_orders || 0)} 筆`;
    }

    if (bigOrderCode) {
      bigOrderCode.textContent = safeText(data.latest_order_code, "-");
    }

    if (bigExtraLabel) {
      bigExtraLabel.textContent = "金額";
    }

    if (bigExtra) {
      bigExtra.textContent = moneyText(data.latest_total_twd);
    }

    if (bigMetaLabel) {
      bigMetaLabel.textContent = "付款";
    }

    if (bigMeta) {
      bigMeta.textContent = [
        safeText(data.latest_payment_method, "-"),
        safeText(data.latest_payment_status, "")
      ].filter(Boolean).join(" / ");
    }

    if (bigPrimaryBtn) {
      bigPrimaryBtn.textContent = "查看新訂單";
      bigPrimaryBtn.href = targetUrl;
      bigPrimaryBtn.classList.add("ring-primary");
      bigPrimaryBtn.classList.remove("ring-secondary");
    }

    if (bigLaterBtn) {
      bigLaterBtn.textContent = "暫不處理";
      bigLaterBtn.classList.add("ring-secondary");
    }

    if (bigStopBtn) {
      bigStopBtn.textContent = "停止鈴聲";
      bigStopBtn.classList.add("ring-secondary");
    }
  }

  function fillDriverOverlay(data, targetUrl) {
    const panel = ensureDriverDetailPanel();

    overlay.classList.remove("store-alert");
    overlay.classList.add("driver-alert");

    if (bigIcon) {
      bigIcon.textContent = "送";
    }

    if (bigTitle) {
      bigTitle.textContent = "有新配送單";
    }

    if (bigSubtitle) {
      bigSubtitle.textContent = "請確認取貨地點、送達地址與配送費";
    }

    if (bigCountLabel) {
      bigCountLabel.textContent = "可接單";
    }

    if (bigCount) {
      bigCount.textContent = `${Number(data.available_orders || data.waiting_orders || 0)} 筆`;
    }

    if (bigOrderCode) {
      bigOrderCode.textContent = safeText(data.latest_order_code, "-");
    }

    if (bigExtraLabel) {
      bigExtraLabel.textContent = "配送費";
    }

    if (bigExtra) {
      bigExtra.textContent = moneyText(data.latest_delivery_fee_twd);
    }

    if (bigMetaLabel) {
      bigMetaLabel.textContent = "店家";
    }

    if (bigMeta) {
      bigMeta.textContent = safeText(data.latest_store_name, "-");
    }

    if (panel) {
      panel.hidden = false;
      panel.innerHTML = [
        ringRow("店家", data.latest_store_name, true),
        ringRow("取貨", data.latest_store_address, false),
        ringRow("送達", data.latest_delivery_address, false),
        ringRow("距離", kmText(data.latest_distance_km), false),
        ringRow("訂單", data.latest_order_code, true),
        ringRow("配送費", moneyText(data.latest_delivery_fee_twd), true),
        ringRow("總金額", moneyText(data.latest_total_twd), false),
        ringRow(
          "付款",
          [
            safeText(data.latest_payment_method, "-"),
            safeText(data.latest_payment_status, "")
          ].filter(Boolean).join(" / "),
          false
        ),
        ringRow("SmartRoad", data.latest_smartroad_lane, false)
      ].join("");
    }

    if (bigPrimaryBtn) {
      bigPrimaryBtn.textContent = "接單 / 查看訂單";
      bigPrimaryBtn.href = targetUrl;
      bigPrimaryBtn.classList.add("ring-primary");
      bigPrimaryBtn.classList.remove("ring-secondary");
    }

    if (bigLaterBtn) {
      bigLaterBtn.textContent = "暫不處理";
      bigLaterBtn.classList.add("ring-secondary");
    }

    if (bigStopBtn) {
      bigStopBtn.textContent = "停止鈴聲";
      bigStopBtn.classList.add("ring-secondary");
    }
  }

  function fillOverlay(data) {
    if (!overlay) {
      return;
    }

    const targetUrl = buildTargetUrl(data);

    if (data.role === "DRIVER") {
      fillDriverOverlay(data, targetUrl);
    } else {
      fillStoreOverlay(data, targetUrl);
    }

    if (bigPrimaryBtn) {
      bigPrimaryBtn.href = targetUrl;
    }

    if (linkEl) {
      linkEl.href = targetUrl;
    }

    overlay.dataset.targetUrl = targetUrl;
    updateEnableUI();
  }

  function showOverlay(data, shouldPlay) {
    fillOverlay(data);

    if (overlay) {
      overlay.hidden = false;
    }

    if (banner) {
      banner.hidden = false;
    }

    const isStore = data.role === "STORE";
    const isDriver = data.role === "DRIVER";

    const countText = isStore
      ? `新訂單 ${data.new_orders || 0} 筆`
      : `可接單 ${data.available_orders || data.waiting_orders || 0} 筆`;

    const latest = data.latest_order_code
      ? `｜${data.latest_order_code}`
      : "";

    if (titleEl) {
      titleEl.textContent = isDriver ? "有新配送單" : "有新訂單";
    }

    if (messageEl) {
      if (isDriver) {
        messageEl.textContent = `${countText}${latest}｜${safeText(data.latest_store_name, "-")}`;
      } else {
        messageEl.textContent = `${countText}${latest}`;
      }
    }

    if (shouldPlay) {
      startBell();
    }
  }

  function hideOverlay() {
    if (overlay) {
      overlay.hidden = true;
    }

    if (banner) {
      banner.hidden = true;
    }
  }

  function dismissCurrent() {
    stopBell();

    if (currentSignature) {
      writeDismissed(currentSignature);
    }

    hideOverlay();
  }

  function navigateCurrent() {
    const targetUrl = overlay && overlay.dataset.targetUrl
      ? overlay.dataset.targetUrl
      : buildTargetUrl(lastPayload);

    stopBell();

    if (currentSignature) {
      writeDismissed(currentSignature);
    }

    hideOverlay();

    if (targetUrl && targetUrl !== "#") {
      window.location.href = targetUrl;
    }
  }

  function shouldTriggerAlert(data, signature) {
    if (!data || !data.ok || !data.should_ring) {
      return false;
    }

    if (!signature) {
      return false;
    }

    if (isDismissedRecently(signature)) {
      return false;
    }

    if (firstPoll && signature === lastSignature) {
      return false;
    }

    return signature !== lastSignature || !overlay || overlay.hidden;
  }

  function handleNoRing(data) {
    if (!data || !data.ok || data.should_ring) {
      return;
    }

    stopBell();
    hideOverlay();
  }

  function getNextPollDelay(data) {
    if (document.hidden) {
      return HIDDEN_POLL_MS;
    }

    if (!data || !data.ok) {
      return IDLE_POLL_MS;
    }

    if (data.role === "DRIVER" && !data.is_online) {
      return OFFLINE_DRIVER_POLL_MS;
    }

    if (data.should_ring) {
      return VISIBLE_POLL_MS;
    }

    if (data.role === "STORE") {
      const hasStoreWork =
        Number(data.new_orders || 0) > 0 ||
        Number(data.accepted_orders || 0) > 0 ||
        Number(data.waiting_driver_orders || 0) > 0 ||
        Number(data.delivery_orders || 0) > 0 ||
        Number(data.held_orders || 0) > 0;

      return hasStoreWork ? VISIBLE_POLL_MS : IDLE_POLL_MS;
    }

    if (data.role === "DRIVER") {
      const hasDriverWork =
        Number(data.available_orders || data.waiting_orders || 0) > 0 ||
        Number(data.active_orders || 0) > 0;

      return hasDriverWork ? VISIBLE_POLL_MS : IDLE_POLL_MS;
    }

    return IDLE_POLL_MS;
  }

  function clearPollTimer() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  function scheduleNextPoll(delayMs) {
    if (stopped) {
      return;
    }

    clearPollTimer();

    pollTimer = setTimeout(function () {
      pollRealtime();
    }, Number(delayMs || VISIBLE_POLL_MS));
  }

  function abortInFlight() {
    if (inFlightController) {
      try {
        inFlightController.abort();
      } catch (e) {}
      inFlightController = null;
    }
  }

  function pollRealtime() {
    if (stopped) {
      return;
    }

    abortInFlight();

    const controller = new AbortController();
    inFlightController = controller;

    const timeoutId = setTimeout(function () {
      try {
        controller.abort();
      } catch (e) {}
    }, FETCH_TIMEOUT_MS);

    fetch(endpoint, {
      method: "GET",
      credentials: "same-origin",
      signal: controller.signal,
      headers: {
        "Accept": "application/json",
        "X-FUMAP-Realtime": "1"
      }
    })
      .then(function (res) {
        if (!res.ok) {
          throw new Error("HTTP " + res.status);
        }

        return res.json();
      })
      .then(function (data) {
        lastPayload = data;

        const signature = buildSignature(data);
        currentSignature = signature;

        if (shouldTriggerAlert(data, signature)) {
          showOverlay(data, true);
        } else if (data && data.ok && data.should_ring && signature && !isDismissedRecently(signature)) {
          showOverlay(data, false);
        } else {
          handleNoRing(data);
        }

        if (signature) {
          lastSignature = signature;
          writeState(signature);
        }

        firstPoll = false;
        scheduleNextPoll(getNextPollDelay(data));
      })
      .catch(function () {
        firstPoll = false;
        scheduleNextPoll(document.hidden ? HIDDEN_POLL_MS : IDLE_POLL_MS);
      })
      .finally(function () {
        clearTimeout(timeoutId);

        if (inFlightController === controller) {
          inFlightController = null;
        }
      });
  }

  function injectRingbellProStyle() {
    if (document.getElementById("fumap-ringbell-pro-style")) {
      return;
    }

    const style = document.createElement("style");
    style.id = "fumap-ringbell-pro-style";
    style.textContent = `
      #bigRingbellOverlay {
        cursor: pointer;
      }

      #bigRingbellOverlay .ring-detail-panel {
        margin-top: 12px;
        padding: 10px 12px;
        border-radius: 16px;
        background: rgba(255, 255, 255, 0.92);
        border: 1px solid rgba(15, 23, 42, 0.12);
      }

      #bigRingbellOverlay .ring-info-row {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 10px;
        padding: 8px 0;
        border-bottom: 1px solid rgba(15, 23, 42, 0.10);
        font-size: 15px;
        line-height: 1.35;
      }

      #bigRingbellOverlay .ring-info-row:last-child {
        border-bottom: 0;
      }

      #bigRingbellOverlay .ring-info-row span {
        flex: 0 0 auto;
        color: #4b5563;
        font-weight: 800;
        white-space: nowrap;
      }

      #bigRingbellOverlay .ring-info-row b {
        flex: 1 1 auto;
        text-align: right;
        color: #111827;
        font-weight: 950;
        word-break: break-word;
      }

      #bigRingbellOverlay .ring-info-row.strong b {
        color: #166534;
      }

      #bigRingbellOverlay.driver-alert .big-ring-card,
      #bigRingbellOverlay.driver-alert .ring-card {
        border: 3px solid #16a34a !important;
      }

      #bigRingbellOverlay.driver-alert #bigRingTitle {
        font-size: 26px;
        font-weight: 950;
      }

      #bigRingbellOverlay .ring-primary,
      #bigRingbellOverlay #bigRingPrimaryBtn {
        min-height: 58px !important;
        border-radius: 18px !important;
        background: #16a34a !important;
        color: #ffffff !important;
        font-size: 20px !important;
        font-weight: 950 !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        text-decoration: none !important;
        border: 0 !important;
      }

      #bigRingbellOverlay .ring-secondary,
      #bigRingbellOverlay #bigRingLaterBtn,
      #bigRingbellOverlay #bigRingStopBtn {
        min-height: 50px !important;
        border-radius: 16px !important;
        background: #f3f4f6 !important;
        color: #111827 !important;
        font-size: 17px !important;
        font-weight: 850 !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        text-decoration: none !important;
        border: 1px solid #d1d5db !important;
      }
    `;

    document.head.appendChild(style);
  }

  if (enableBtn) {
    enableBtn.addEventListener("click", enableBell);
  }

  if (testBtn) {
    testBtn.addEventListener("click", testBell);
  }

  if (controlEnableBtn) {
    controlEnableBtn.addEventListener("click", enableBell);
  }

  if (controlTestBtn) {
    controlTestBtn.addEventListener("click", testBell);
  }

  if (stopBtn) {
    stopBtn.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      dismissCurrent();
    });
  }

  if (bigStopBtn) {
    bigStopBtn.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      dismissCurrent();
    });
  }

  if (bigLaterBtn) {
    bigLaterBtn.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      dismissCurrent();
    });
  }

  if (bigPrimaryBtn) {
    bigPrimaryBtn.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      navigateCurrent();
    });
  }

  if (overlay) {
    overlay.addEventListener("click", function (event) {
      const target = event.target;

      if (
        target === bigStopBtn ||
        target === bigLaterBtn ||
        target === bigPrimaryBtn ||
        (target && target.closest && target.closest("#bigRingStopBtn, #bigRingLaterBtn, #bigRingPrimaryBtn"))
      ) {
        return;
      }

      navigateCurrent();
    });
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      stopBell();
      abortInFlight();
      scheduleNextPoll(HIDDEN_POLL_MS);
      return;
    }

    scheduleNextPoll(1000);
  });

  window.addEventListener("pagehide", function () {
    stopBell();
    abortInFlight();
    clearPollTimer();
    stopped = true;
  });

  window.addEventListener("pageshow", function () {
    stopped = false;
    scheduleNextPoll(1000);
  });

  window.FUMAP_BIG_RINGBELL = {
    enable: enableBell,
    test: testBell,
    stop: stopBell,
    dismiss: dismissCurrent,
    pollNow: function () {
      scheduleNextPoll(0);
    },
    lastPayload: function () {
      return lastPayload;
    }
  };

  updateEnableUI();
  pollRealtime();
})();
