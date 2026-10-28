/**
 * ComfyUI-LogBlaster frontend extension.
 *
 * - Adds a "LOG BLASTER" launcher button + "AUTO" toggle to ComfyUI's top bar.
 * - Automatically pops open (or wakes without reloading) the WebGL2 GPU Shader
 *   Log HUD window whenever a workflow starts running (both via UI Queue Prompt
 *   and external/headless API runs).
 * - Sends submitted workflow titles with their prompt ID over BroadcastChannel
 *   so other open ComfyUI tabs cannot relabel the executing workflow.
 */
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const STORAGE_AUTO_KEY = "logblaster.autoOpen";
function resolveHudUrl() {
  try {
    if (typeof api.fileURL === "function") return api.fileURL("/logblaster");
  } catch (_) {}
  return `${String(api.api_base || "").replace(/\/$/, "")}/logblaster`;
}
const HUD_URL = resolveHudUrl();
const SERVER_BASE = new URL(HUD_URL, window.location.href).pathname.replace(/\/logblaster\/?$/, "");
const WINDOW_NAME = "ComfyUILogBlasterHUD" + (SERVER_BASE ? `:${SERVER_BASE}` : "");
const promptNodeMaps = new Map();

let popupRef = null;
let lastPongMs = 0;
let dockEl = null;
let bus = null;
let autoOpenFallback = true;
let lastQueueRemaining = 0;
let autoOpenGraceTimer = null;

function isAutoOpenEnabled() {
  try {
    const val = localStorage.getItem(STORAGE_AUTO_KEY);
    return val === null ? autoOpenFallback : val === "1";
  } catch (_) {
    return autoOpenFallback;
  }
}

function setAutoOpenEnabled(enabled) {
  autoOpenFallback = Boolean(enabled);
  try {
    localStorage.setItem(STORAGE_AUTO_KEY, enabled ? "1" : "0");
  } catch (_) {
    // Non-fatal in restricted storage contexts
  }
}

function isHudPopup(ref) {
  try {
    if (!ref || ref.closed) return false;
    const current = new URL(ref.location.href, window.location.href);
    const expected = new URL(HUD_URL, window.location.href);
    return current.origin === expected.origin && current.pathname === expected.pathname;
  } catch (_) {
    return false;
  }
}

function isHudWindowAlive() {
  if (popupRef && !popupRef.closed) return isHudPopup(popupRef);
  if (Date.now() - lastPongMs < 3800) return true;
  if (dockEl && document.body.contains(dockEl)) return true;
  return false;
}

function snapshotPromptNodeTitles(prompt) {
  const map = Object.create(null);
  for (const n of prompt?.workflow?.nodes || []) {
    if (n && n.id !== undefined) {
      map[String(n.id)] = String(n.title || n.type || `Node ${n.id}`);
    }
  }
  for (const [id, n] of Object.entries(prompt?.output || {})) {
    map[id] = String(n?._meta?.title || map[id] || n?.class_type || `Node ${id}`);
  }
  return map;
}

function publishPromptNodeTitles(promptId, nodes) {
  if (!bus) return;
  bus.postMessage({ type: "node_map", prompt_id: promptId, replace: true, nodes });
}

function rememberPromptNodeTitles(promptId, nodes) {
  if (typeof promptId !== "string" || !promptId || !nodes) return;
  promptNodeMaps.set(promptId, nodes);
  while (promptNodeMaps.size > 32) promptNodeMaps.delete(promptNodeMaps.keys().next().value);
  publishPromptNodeTitles(promptId, nodes);
}

function closeEmbeddedDock() {
  if (dockEl) {
    const iframe = dockEl.querySelector("iframe");
    if (iframe) {
      try { iframe.src = "about:blank"; } catch (_) {}
    }
    dockEl.remove();
    dockEl = null;
  }
}

function openEmbeddedDock() {
  if (dockEl && document.body.contains(dockEl)) return;

  dockEl = document.createElement("div");
  dockEl.style.cssText = [
    "position:fixed",
    "right:18px",
    "bottom:18px",
    "width:min(820px, 62vw)",
    "height:min(500px, 56vh)",
    "z-index:99999",
    "background:#05080d",
    "border:1px solid rgba(77,243,255,0.65)",
    "border-radius:10px",
    "box-shadow:0 16px 48px rgba(0,0,0,0.8)",
    "display:flex",
    "flex-direction:column",
    "overflow:hidden",
    "resize:both",
  ].join(";");

  const bar = document.createElement("div");
  bar.style.cssText = [
    "display:flex",
    "align-items:center",
    "justify-content:space-between",
    "padding:5px 10px",
    "background:#0a121c",
    "border-bottom:1px solid rgba(77,243,255,0.28)",
    "color:#bfefff",
    "font:600 11px 'Cascadia Mono','Consolas',monospace",
  ].join(";");

  const title = document.createElement("span");
  title.textContent = "COMFYUI // LOG BLASTER (GPU SHADER HUD)";

  const actions = document.createElement("div");
  actions.style.cssText = "display:flex;gap:6px;";

  const btnStyle = [
    "background:rgba(77,243,255,0.16)",
    "color:#e7fbff",
    "border:1px solid rgba(77,243,255,0.5)",
    "border-radius:5px",
    "padding:2px 8px",
    "font:600 11px 'Cascadia Mono','Consolas',monospace",
    "cursor:pointer",
  ].join(";");

  const popBtn = document.createElement("button");
  popBtn.type = "button";
  popBtn.textContent = "Pop Out Window";
  popBtn.style.cssText = btnStyle;
  popBtn.addEventListener("click", () => {
    closeEmbeddedDock();
    openLogBlasterWindow(true);
  });

  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.textContent = "X";
  closeBtn.style.cssText = btnStyle;
  closeBtn.addEventListener("click", closeEmbeddedDock);

  actions.append(popBtn, closeBtn);
  bar.append(title, actions);

  const iframe = document.createElement("iframe");
  iframe.allow = "clipboard-write; fullscreen; microphone; display-capture; autoplay";
  iframe.allowFullscreen = true;
  iframe.src = HUD_URL;
  iframe.style.cssText = "flex:1;width:100%;border:none;background:#030508;";

  dockEl.append(bar, iframe);
  document.body.appendChild(dockEl);
}

function openLogBlasterWindow(focusIfOpen = false) {
  if (isHudWindowAlive()) {
    try { bus?.postMessage({ type: "wake", ts: Date.now() }); } catch (_) {}
    if (focusIfOpen && popupRef && !popupRef.closed) {
      try { popupRef.focus(); } catch (_) {}
    }
    return;
  }

  const availW = Number(window.screen?.availWidth) || window.innerWidth || 1440;
  const availH = Number(window.screen?.availHeight) || window.innerHeight || 900;
  const w = Math.min(1380, Math.round(availW * 0.82));
  const h = Math.min(860, Math.round(availH * 0.82));
  const left = Math.max(20, Math.round((availW - w) / 2));
  const top = Math.max(20, Math.round((availH - h) / 2));
  const features = `popup=yes,width=${w},height=${h},left=${left},top=${top},resizable=yes`;

  let opened = null;
  try {
    // Open named target with empty URL first so an existing background-throttled
    // HUD popup is re-attached without reloading / wiping its 3,500-line buffer!
    opened = window.open("", WINDOW_NAME, features);
  } catch (_) {
    opened = null;
  }

  if (opened) {
    popupRef = opened;
    try {
      if (!isHudPopup(opened)) {
        opened.location.href = HUD_URL;
      } else {
        try { bus?.postMessage({ type: "wake", ts: Date.now() }); } catch (_) {}
        if (focusIfOpen) {
          try { opened.focus(); } catch (_) {}
        }
      }
    } catch (_) {
      try { opened.location.href = HUD_URL; } catch (_) {}
    }
    closeEmbeddedDock();
  } else {
    // Popup blocked (e.g. external API trigger without user gesture) -> show sleek embedded HUD dock
    openEmbeddedDock();
  }
}

function requestExternalRunAutoOpen() {
  if (!isAutoOpenEnabled() || isHudWindowAlive()) return;
  try { bus?.postMessage({ type: "ping" }); } catch (_) {}
  if (autoOpenGraceTimer) return;
  autoOpenGraceTimer = setTimeout(() => {
    autoOpenGraceTimer = null;
    if (isAutoOpenEnabled() && !isHudWindowAlive()) {
      openLogBlasterWindow(false);
    }
  }, 180);
}

function mountToolbarControls() {
  if (document.getElementById("comfy-logblaster-pill")) return;

  const pill = document.createElement("div");
  pill.id = "comfy-logblaster-pill";
  pill.style.cssText = [
    "display:inline-flex",
    "align-items:center",
    "gap:4px",
    "margin:0 6px",
    "padding:2px 4px",
    "border-radius:7px",
    "background:rgba(12, 22, 32, 0.88)",
    "border:1px solid rgba(77, 243, 255, 0.42)",
    "font-family:'Cascadia Mono','Consolas',monospace",
    "z-index:1000",
  ].join(";");

  const launchBtn = document.createElement("button");
  launchBtn.type = "button";
  launchBtn.textContent = "LOG BLASTER";
  launchBtn.title = "Open WebGL2 GPU Shader Log Visualizer (/logblaster)";
  launchBtn.style.cssText = [
    "background:linear-gradient(135deg, rgba(77,243,255,0.22), rgba(255,62,127,0.18))",
    "color:#e8fbff",
    "border:1px solid rgba(77,243,255,0.45)",
    "border-radius:5px",
    "padding:3px 9px",
    "font:700 11px 'Cascadia Mono','Consolas',monospace",
    "letter-spacing:0.4px",
    "cursor:pointer",
  ].join(";");
  launchBtn.addEventListener("click", () => openLogBlasterWindow(true));

  const autoBtn = document.createElement("button");
  autoBtn.type = "button";
  const syncAutoLabel = () => {
    const on = isAutoOpenEnabled();
    autoBtn.textContent = on ? "AUTO:ON" : "AUTO:OFF";
    autoBtn.title = on
      ? "Auto-popup on workflow run is ON (click to toggle)"
      : "Auto-popup on workflow run is OFF (click to toggle)";
    autoBtn.style.cssText = [
      on ? "background:rgba(56,255,179,0.2)" : "background:rgba(255,255,255,0.06)",
      on ? "color:#7affcf" : "color:#8ea5b5",
      on ? "border:1px solid rgba(56,255,179,0.5)" : "border:1px solid rgba(255,255,255,0.15)",
      "border-radius:5px",
      "padding:3px 6px",
      "font:600 10px 'Cascadia Mono','Consolas',monospace",
      "cursor:pointer",
    ].join(";");
  };
  syncAutoLabel();
  autoBtn.addEventListener("click", () => {
    setAutoOpenEnabled(!isAutoOpenEnabled());
    syncAutoLabel();
  });

  pill.append(launchBtn, autoBtn);

  // Try modern ComfyUI topbar targets first, then classic menu, then floating corner pill
  const modernBar =
    document.querySelector(".comfyui-menu-right") ||
    document.querySelector(".p-menubar-end") ||
    app?.menu?.settingsGroup?.element?.parentElement ||
    document.querySelector(".comfy-menu");

  if (modernBar) {
    modernBar.prepend(pill);
  } else {
    pill.style.position = "fixed";
    pill.style.top = "8px";
    pill.style.right = "140px";
    document.body.appendChild(pill);
  }
}

app.registerExtension({
  name: "ComfyUI.LogBlaster",
  async setup() {
    try {
      bus = new BroadcastChannel(`comfy-logblaster-bus:${SERVER_BASE}`);
      bus.addEventListener("message", (ev) => {
        const msgType = ev.data?.type;
        if (msgType === "pong") {
          lastPongMs = Date.now();
        } else if (msgType === "hello") {
          lastPongMs = Date.now();
          try {
            for (const [promptId, nodes] of promptNodeMaps) publishPromptNodeTitles(promptId, nodes);
          } catch (_) {}
        }
      });
      bus.postMessage({ type: "ping" });
    } catch (_) {
      bus = null;
    }

    mountToolbarControls();
    // Re-check toolbar mount after Vue topbar finishes async hydration
    setTimeout(mountToolbarControls, 1200);

    // Hook synchronous queuePrompt so clicking Run opens the window inside the user gesture,
    // without stealing focus if already open and never blocking prompt queueing on any UI error.
    if (api && typeof api.queuePrompt === "function" && !api._logBlasterWrapped) {
      const origQueuePrompt = api.queuePrompt.bind(api);
      api._logBlasterWrapped = true;
      api.queuePrompt = async function (...args) {
        let nodes = null;
        try { nodes = snapshotPromptNodeTitles(args[1]); } catch (_) {}
        try {
          if (isAutoOpenEnabled()) {
            openLogBlasterWindow(false);
          }
        } catch (_) {
          // Never prevent workflow submission
        }
        const result = await origQueuePrompt(...args);
        try { rememberPromptNodeTitles(result?.prompt_id, nodes); } catch (_) {}
        return result;
      };
    }

    // Catch external/headless API runs while ComfyUI UI is open.
    // Events ComfyUI actually emits reach /logblaster over /ws via _install_observer_tap.
    // Without client_id, core can omit executing/terminal events; broadcast status/progress
    // still provide observed timing for those submissions.
    api.addEventListener("execution_start", () => {
      requestExternalRunAutoOpen();
    });

    // `status` is always broadcast (`sid=None`), catching headless runs queued without `client_id`
    api.addEventListener("status", (ev) => {
      const rem = Number(ev?.detail?.exec_info?.queue_remaining ?? ev?.detail?.status?.exec_info?.queue_remaining ?? 0);
      if (Number.isFinite(rem)) {
        if (rem > 0 && lastQueueRemaining === 0) {
          requestExternalRunAutoOpen();
        }
        lastQueueRemaining = rem;
      }
    });
  },
});
