/**
 * ComfyUI-LogBlaster frontend extension.
 *
 * - Adds a "LOG BLASTER" launcher button + "AUTO" toggle to ComfyUI's top bar.
 * - Automatically pops open (or wakes) the WebGL2 GPU Shader Log HUD window
 *   whenever a workflow starts running (both via UI Queue Prompt and API runs).
 * - Bridges human-readable node titles from `app.graph` over BroadcastChannel
 *   so the HUD displays real node titles alongside blasting logs.
 */
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const STORAGE_AUTO_KEY = "logblaster.autoOpen";
const HUD_URL = "/logblaster";
const WINDOW_NAME = "ComfyUILogBlasterHUD";

let popupRef = null;
let lastPongMs = 0;
let dockEl = null;
let bus = null;

function isAutoOpenEnabled() {
  const val = localStorage.getItem(STORAGE_AUTO_KEY);
  return val === null ? true : val === "1";
}

function setAutoOpenEnabled(enabled) {
  localStorage.setItem(STORAGE_AUTO_KEY, enabled ? "1" : "0");
}

function isHudWindowAlive() {
  if (popupRef && !popupRef.closed) return true;
  if (Date.now() - lastPongMs < 3500) return true;
  if (dockEl && document.body.contains(dockEl)) return true;
  return false;
}

function publishGraphNodeTitles() {
  if (!bus) return;
  try {
    const nodes = app?.graph?._nodes;
    if (!Array.isArray(nodes)) return;
    const map = {};
    for (const n of nodes) {
      if (n && n.id !== undefined) {
        map[String(n.id)] = String(n.title || n.comfyClass || n.type || `Node ${n.id}`);
      }
    }
    bus.postMessage({ type: "node_map", nodes: map });
  } catch (_) {
    // Non-fatal
  }
}

function closeEmbeddedDock() {
  if (dockEl) {
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
  popBtn.textContent = "Pop Out Window";
  popBtn.style.cssText = btnStyle;
  popBtn.addEventListener("click", () => {
    closeEmbeddedDock();
    openLogBlasterWindow(true);
  });

  const closeBtn = document.createElement("button");
  closeBtn.textContent = "X";
  closeBtn.style.cssText = btnStyle;
  closeBtn.addEventListener("click", closeEmbeddedDock);

  actions.append(popBtn, closeBtn);
  bar.append(title, actions);

  const iframe = document.createElement("iframe");
  iframe.src = HUD_URL;
  iframe.style.cssText = "flex:1;width:100%;border:none;background:#030508;";

  dockEl.append(bar, iframe);
  document.body.appendChild(dockEl);
}

function openLogBlasterWindow(fromUserClick = false) {
  publishGraphNodeTitles();

  if (isHudWindowAlive()) {
    try { bus?.postMessage({ type: "wake", ts: Date.now() }); } catch (_) {}
    if (fromUserClick && popupRef && !popupRef.closed) {
      try { popupRef.focus(); } catch (_) {}
    }
    return;
  }

  const w = Math.min(1380, Math.round(window.screen.availWidth * 0.82));
  const h = Math.min(860, Math.round(window.screen.availHeight * 0.82));
  const left = Math.max(20, Math.round((window.screen.availWidth - w) / 2));
  const top = Math.max(20, Math.round((window.screen.availHeight - h) / 2));
  const features = `popup=yes,width=${w},height=${h},left=${left},top=${top},resizable=yes`;

  let opened = null;
  try {
    opened = window.open(HUD_URL, WINDOW_NAME, features);
  } catch (_) {
    opened = null;
  }

  if (opened) {
    popupRef = opened;
    closeEmbeddedDock();
    setTimeout(publishGraphNodeTitles, 500);
  } else {
    // Popup blocked (e.g. external API trigger without user gesture) -> show sleek embedded HUD dock
    openEmbeddedDock();
  }
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
      bus = new BroadcastChannel("comfy-logblaster-bus");
      bus.addEventListener("message", (ev) => {
        if (ev.data?.type === "pong") {
          lastPongMs = Date.now();
        }
      });
      bus.postMessage({ type: "ping" });
    } catch (_) {
      bus = null;
    }

    mountToolbarControls();
    // Re-check toolbar mount after Vue topbar finishes async hydration
    setTimeout(mountToolbarControls, 1200);

    // Hook synchronous queuePrompt so clicking Run opens the window inside the user gesture!
    if (api && typeof api.queuePrompt === "function" && !api._logBlasterWrapped) {
      const origQueuePrompt = api.queuePrompt.bind(api);
      api._logBlasterWrapped = true;
      api.queuePrompt = async function (...args) {
        if (isAutoOpenEnabled()) {
          openLogBlasterWindow(true);
        } else {
          publishGraphNodeTitles();
        }
        return origQueuePrompt(...args);
      };
    }

    // Also catch external / headless API runs while ComfyUI UI is open
    api.addEventListener("execution_start", () => {
      publishGraphNodeTitles();
      if (isAutoOpenEnabled() && !isHudWindowAlive()) {
        // Brief grace period for BroadcastChannel pong before opening
        try { bus?.postMessage({ type: "ping" }); } catch (_) {}
        setTimeout(() => {
          if (isAutoOpenEnabled() && !isHudWindowAlive()) {
            openLogBlasterWindow(false);
          }
        }, 180);
      }
    });
  },
});
