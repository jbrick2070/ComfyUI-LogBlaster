const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../web_app/index.html"), "utf8");
const inline = html.match(/<script>([\s\S]*)<\/script>/)[1];
const hook = `\nthis.__hudTest = {state, ingestLogChunk, getViewEndIndex, getFilteredLines,
  handleComfyEvent, pollTelemetry, renderTextTexture, connectComfySocket,
  getTelemetryFreshness, getConnectionStatus, getLogBottom, redactSensitiveText,
  ensureAudioGraph, playSynthTick, textCanvas,
  synthNodes: () => ({audioCtx, synthMasterGain, droneGain, droneOsc})};\n})();`;
assert.ok(inline.trimEnd().endsWith("})();"));
const source = inline.trimEnd().replace(/\}\)\(\);$/, hook);

class EventStub {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  async emit(type, event = {}) {
    for (const fn of this.listeners.get(type) || []) await fn(event);
  }
}

async function makeHud(options = {}) {
  let now = 1000;
  let copied = "";
  let nvmlAvailable = false;
  let fetchFailure = false;
  const storage = options.storage || new Map();
  const motionQuery = new EventStub();
  motionQuery.matches = Boolean(options.reducedMotion);
  const fetches = [], sockets = [], channels = [], draws = [], drawsAt = [], intervals = [];
  const timers = [];
  let nextTimerId = 0;
  const gradient = { addColorStop() {} };
  const context2d = new Proxy({}, {
    get(target, key) {
      if (key in target) return target[key];
      if (key === "measureText") return text => ({ width: String(text).length * 8 });
      if (key === "createLinearGradient" || key === "createRadialGradient") return () => gradient;
      if (key === "fillText" || key === "strokeText") {
        // Where each piece of text lands, and in what font, for the layout checks.
        return (text, x, y) => {
          draws.push(String(text));
          drawsAt.push({ text: String(text), x, y, font: String(target.font || "") });
        };
      }
      return () => {};
    },
    set(target, key, value) { target[key] = value; return true; },
  });
  class ElementStub extends EventStub {
    constructor(id = "") {
      super(); this.id = id; this.value = ""; this.textContent = ""; this.dataset = {};
      this.open = false;
      this.tagName = id.startsWith("btn") ? "BUTTON" : "DIV";
      this.style = { setProperty() {} };
      this.classList = { toggle() {}, add() {}, remove() {} };
    }
    getContext(type) { return type === "2d" ? context2d : null; }
    appendChild() {}
    focus() { document.activeElement = this; }
    blur() {}
    select() {}
    click() { return this.emit("click", {}); }
    showModal() { this.open = true; }
    close() { this.open = false; }
    getBoundingClientRect() { return this.rect || { top: 650, height: 56, width: 1100 }; }
  }
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, new ElementStub(id));
    return elements.get(id);
  };
  const document = new EventStub();
  Object.assign(document, {
    hidden: false, fullscreenElement: null,
    getElementById: element,
    querySelectorAll: () => [],
    createElement: () => new ElementStub(),
    body: new ElementStub(), documentElement: new ElementStub(),
  });
  const location = { search: options.search || "", pathname: "/comfy/logblaster", host: "example.test:8000",
    protocol: "http:", href: "http://example.test:8000/comfy/logblaster", reload() {} };
  const window = new EventStub();
  const audioNodes = [];
  class AudioParamStub {
    constructor() { this.value = 0; this.events = []; }
    cancelScheduledValues(t) { this.events.push(["cancel", t]); }
    setValueAtTime(value, t) { this.value = value; this.events.push(["set", value, t]); }
    linearRampToValueAtTime(value, t) { this.value = value; this.events.push(["linear", value, t]); }
    exponentialRampToValueAtTime(value, t) { this.value = value; this.events.push(["exp", value, t]); }
    setTargetAtTime(value, t) { this.value = value; this.events.push(["target", value, t]); }
  }
  class AudioNodeStub {
    constructor() {
      this.gain = new AudioParamStub(); this.frequency = new AudioParamStub();
      this.frequencyBinCount = 256; this.connections = []; audioNodes.push(this);
    }
    connect(node) { this.connections.push(node); }
    disconnect() {}
    start() {}
    stop() {}
    getByteFrequencyData(buffer) { buffer.fill(0); }
    getByteTimeDomainData(buffer) { buffer.fill(128); }
  }
  class AudioContextStub {
    constructor() { this.currentTime = 1; this.state = "running"; this.destination = {}; }
    createAnalyser() { return new AudioNodeStub(); }
    createGain() { return new AudioNodeStub(); }
    createOscillator() { return new AudioNodeStub(); }
    createBiquadFilter() { return new AudioNodeStub(); }
    resume() { this.state = "running"; return Promise.resolve(); }
    close() { this.state = "closed"; return Promise.resolve(); }
  }
  Object.assign(window, { location, innerWidth: 1280, innerHeight: 720, devicePixelRatio: 1,
    matchMedia: () => motionQuery, AudioContext: AudioContextStub });
  // A top-level page is its own top; options.framed puts it in a frame (the dock).
  window.self = window;
  window.top = options.framed ? new EventStub() : window;
  const context = vm.createContext({
    window, document, location, URL, URLSearchParams, AbortController,
    crypto: { randomUUID: () => "audit-stub" },
    performance: { now: () => now }, console,
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, String(value)) },
    navigator: { clipboard: { async writeText(text) { copied = text; } } },
    setTimeout(fn, delay) {
      const id = ++nextTimerId;
      timers.push({ id, fn, delay, canceled: false });
      return id;
    },
    clearTimeout(id) {
      const timer = timers.find(item => item.id === id);
      if (timer) timer.canceled = true;
    },
    // Kept, never run on their own: a test fires one when it means to (the heartbeat).
    setInterval(fn, delay) { intervals.push({ fn, delay }); return intervals.length; }, clearInterval() {},
    requestAnimationFrame: () => 1, cancelAnimationFrame() {},
    WebSocket: class extends EventStub {
      constructor(url) { super(); this.url = url; sockets.push(this); }
      close() {}
    },
    BroadcastChannel: class extends EventStub {
      constructor(name) { super(); this.name = name; this.posts = []; channels.push(this); }
      postMessage(msg) { this.posts.push(msg); }
      close() {}
    },
    async fetch(url, options = {}) {
      fetches.push({ url, options });
      if (fetchFailure) throw new Error("Simulated server unavailable");
      let payload = {};
      if (url.endsWith("/logblaster/telemetry")) {
        payload = { available: nvmlAvailable, gpu_name: "GPU stub", vram_total_mib: 8192,
          vram_used_mib: 2048, gpu_util: 42, metrics_mode: "nvml" };
      } else if (url.endsWith("/system_stats")) {
        payload = { devices: [{ type: "cuda", name: "GPU stub", vram_total: 8 * 1024 ** 3, vram_free: 6 * 1024 ** 3 }] };
      } else if (url.endsWith("/internal/logs/raw")) payload = { entries: [] };
      return { ok: true, status: 200, async json() { return payload; } };
    },
  });
  vm.runInContext(source, context, { filename: "logblaster-inline.js" });
  await new Promise(resolve => setImmediate(resolve));
  return { ...context.__hudTest, element, window, document, fetches, sockets, channels, draws, drawsAt, intervals, audioNodes, timers,
    setTime(value) { now = value; }, setNvmlAvailable(value) { nvmlAvailable = value; },
    setFetchFailure(value) { fetchFailure = value; },
    async setReducedMotion(value) { motionQuery.matches = value; await motionQuery.emit("change"); },
    copied: () => copied };
}

async function verifyDemoOwnership() {
  const snapshot = hud => ({
    runState: hud.state.runState, runStartMs: hud.state.runStartMs,
    activePromptId: hud.state.activePromptId, activeNodeTitle: hud.state.activeNodeTitle,
    stepVal: hud.state.stepVal, stepMax: hud.state.stepMax,
    lastElapsedSec: hud.state.lastElapsedSec, linesVersion: hud.state.linesVersion,
  });
  async function startDemo(hud) {
    const previous = hud.timers.length;
    await hud.element("btnTestBlast").click();
    return hud.timers.slice(previous);
  }
  const takeovers = [
    ["execution_start", { prompt_id: "live-start" }],
    ["executing", { prompt_id: "live-node", node: "7" }],
    ["progress", { prompt_id: "live-progress", value: 5, max: 20 }],
    ["status", { status: { exec_info: { queue_remaining: 1 } } }],
    ["execution_error", { prompt_id: "live-error", exception_message: "Test failure" }],
  ];
  for (const [type, data] of takeovers) {
    const hud = await makeHud();
    const callbacks = await startDemo(hud);
    assert.ok(callbacks.some(timer => timer.delay === 4680));
    hud.setTime(2000);
    hud.handleComfyEvent(type, data);
    assert.equal(hud.state.runStartMs, 2000, `${type} must start a server-owned interval`);
    assert.equal(hud.state.activePromptId, data.prompt_id || null);
    assert.ok(callbacks.every(timer => timer.canceled), `${type} did not cancel demo timers`);
    const serverState = snapshot(hud);
    // Force canceled callbacks to run too: cancellation alone is not the ownership guard.
    for (const timer of callbacks) timer.fn();
    assert.deepEqual(snapshot(hud), serverState, `${type} was overwritten by a stale demo callback`);
  }

  for (const knownPrompt of [true, false]) {
    const hud = await makeHud();
    if (knownPrompt) hud.handleComfyEvent("execution_start", { prompt_id: "already-running" });
    else hud.handleComfyEvent("status", { status: { exec_info: { queue_remaining: 1 } } });
    const serverState = snapshot(hud);
    hud.setTime(4000);
    const scheduled = await startDemo(hud);
    assert.deepEqual(snapshot(hud), serverState, "Demo changed an active server workflow");
    assert.ok(!scheduled.some(timer => timer.delay === 4680), "Demo was scheduled during a server workflow");
  }

  const hud = await makeHud();
  const firstDemo = await startDemo(hud);
  const demoState = snapshot(hud);
  hud.handleComfyEvent("status", { status: { exec_info: { queue_remaining: 0 } } });
  assert.deepEqual(snapshot(hud), demoState, "Idle server status ended the local demo");
  hud.setTime(3000);
  const secondDemo = await startDemo(hud);
  assert.ok(firstDemo.every(timer => timer.canceled));
  const restartedState = snapshot(hud);
  for (const timer of firstDemo) timer.fn();
  assert.deepEqual(snapshot(hud), restartedState, "Old demo generation changed its replacement");
  for (const timer of secondDemo.sort((a, b) => a.delay - b.delay)) {
    hud.setTime(3000 + timer.delay);
    if (!timer.canceled) timer.fn();
  }
  assert.equal(hud.state.runState, "COMPLETE");
  assert.equal(hud.state.activeNodeTitle, "Demo Blast Complete");
  assert.equal(hud.state.lastElapsedSec, 4.68);
  assert.equal(hud.state.activePromptId, null);
  const completedState = snapshot(hud);
  for (const timer of secondDemo) timer.fn();
  assert.deepEqual(snapshot(hud), completedState, "Completed demo callbacks remained active");

  const closingDemo = await startDemo(hud);
  await hud.window.emit("pagehide");
  assert.ok(closingDemo.every(timer => timer.canceled));
  const closedState = snapshot(hud);
  for (const timer of closingDemo) timer.fn();
  assert.deepEqual(snapshot(hud), closedState, "Demo callback changed an unloaded HUD");
}

async function verifyProductImprovements() {
  const storage = new Map();
  // The dock-clearance checks below measure the foot of the log, so this HUD
  // keeps the live line there; the default (mid-screen) is pinned after them.
  const hud = await makeHud({ storage, search: "?anchor=bottom" });
  assert.equal(hud.element("helpDialog").open, true, "First opening should explain the controls");
  await hud.element("btnHelpClose").click();
  assert.equal(hud.element("helpDialog").open, false);
  const returning = await makeHud({ storage });
  assert.equal(returning.element("helpDialog").open, false, "Help should not interrupt returning users");
  const key = (key, extra = {}) => hud.window.emit("keydown", {
    key, target: hud.document.body, preventDefault() {}, ...extra,
  });
  await key("?");
  assert.equal(hud.element("helpDialog").open, true);
  await key("Escape", { target: { tagName: "BUTTON" } });
  assert.equal(hud.element("helpDialog").open, false);
  await key("h");
  assert.equal(hud.element("helpDialog").open, true);
  await hud.element("btnHelpClose").click();

  hud.state.preset = 3;
  hud.state.paused = false;
  let fullscreenRequests = 0, captureRequests = 0;
  hud.document.documentElement.requestFullscreen = () => { fullscreenRequests++; };
  // A modified A shortcut must not even attempt to initialize capture/audio.
  for (const modifier of ["ctrlKey", "metaKey", "altKey"]) {
    await key("1", { [modifier]: true });
    await key("f", { [modifier]: true });
    await key("a", { [modifier]: true });
    assert.equal(hud.state.preset, 3);
  }
  assert.equal(fullscreenRequests, 0);
  assert.equal(hud.state.audioInMode, "OFF");
  assert.equal(hud.synthNodes().audioCtx, null);
  for (const tagName of ["BUTTON", "SELECT", "TEXTAREA", "INPUT", "SUMMARY", "A"]) {
    await key(" ", { target: { tagName }, preventDefault() { captureRequests++; } });
    assert.equal(hud.state.paused, false);
  }
  await key("1", { target: { isContentEditable: true } });
  assert.equal(hud.state.preset, 3);
  assert.equal(captureRequests, 0, "Focused controls keep native Space behavior");
  await key("1");
  assert.equal(hud.state.preset, 0, "Unmodified shortcuts still work");

  const more = hud.element("dockMore");
  const moreTools = hud.element("dockMoreTools");
  hud.element("controlDock").rect = { top: 752, height: 37 };
  moreTools.rect = { top: 709, height: 41, width: 150 };
  assert.equal(hud.getLogBottom(801, 90), 742, "Closed More panel must not reserve space");
  hud.state.textDirty = false;
  more.open = true;
  await more.emit("toggle");
  assert.equal(hud.state.textDirty, true, "Opening More must redraw without resizing the dock");
  assert.equal(hud.getLogBottom(801, 90), 699, "Logs must clear the open More panel");
  hud.state.textDirty = false;
  more.open = false;
  await more.emit("toggle");
  assert.equal(hud.state.textDirty, true);
  assert.equal(hud.getLogBottom(801, 90), 742, "Closing More must restore dock-only clearance");
  more.open = true;
  moreTools.rect = { top: 760, height: 41, width: 150 };
  assert.equal(hud.getLogBottom(801, 90), 742, "A lower panel must not shrink the viewport");
  moreTools.rect = { top: 0, height: 0, width: 0 };
  assert.equal(hud.getLogBottom(801, 90), 742, "A hidden panel must not reserve space");
  moreTools.rect = { top: NaN, height: 41, width: 150 };
  assert.equal(hud.getLogBottom(801, 90), 742, "Invalid panel geometry must fall back to the dock");
  more.open = false;
  hud.element("controlDock").rect = { top: 340, height: 146 };
  assert.ok(hud.getLogBottom(500, 90) <= 330, "Logs must clear a wrapped dock");

  // Mid-screen is the default (operator, 2026-10-30: "always middle"); the old
  // ?anchor=middle still means it, and only ?anchor=bottom puts the line at the foot.
  for (const [search, expected] of [["", 481], ["?anchor=middle", 481], ["?anchor=bottom", 742],
                                    ["?anchor=BOTTOM", 742]]) {
    const placed = await makeHud({ search });
    placed.element("controlDock").rect = { top: 752, height: 37 };
    assert.equal(placed.getLogBottom(801, 90), expected, `live line for "${search}"`);
  }
  // A short window (the in-app dock is one) cannot lift the line to 60%: that clipped
  // a wrapped live line under the header (Codex QA of 8778e8f, at 1280x310). Rendered,
  // not computed: every row of a long live line must sit below the clip at logTop,
  // in both views that draw it giant, in both anchors.
  // Three rows too (Codex QA of 61de5a9): no anchor has room for three rows at full
  // size in 1280x310, so the live line's font shrinks to fit.
  for (const [view, search, length] of [["CASCADE", "", 200], ["CASCADE", "?anchor=bottom", 200],
                                        ["DRUM", "", 200], ["DRUM", "?anchor=bottom", 200],
                                        ["CASCADE", "", 400], ["CASCADE", "?anchor=bottom", 400],
                                        ["DRUM", "", 400]]) {
    {
      const short = await makeHud({ search });
      short.state.viewMode = view;
      short.textCanvas.width = 1280;
      short.textCanvas.height = 310;
      short.element("controlDock").rect = { top: 261, height: 37 };
      short.ingestLogChunk(`${"Q".repeat(length)}\n`);
      // A new line slides in from below; measure once it has settled.
      for (let frame = 0; frame < 4; frame++) short.renderTextTexture(2000 + frame * 1000, 1.0);
      short.drawsAt.length = 0;
      short.renderTextTexture(7000, 1.0);
      const rows = short.drawsAt.filter(d => /^Q+$/.test(d.text) && d.text.length > 20);
      const label = `${view} ${search} ${length} chars`;
      assert.ok(rows.length >= (length > 300 ? 3 : 2), `${label}: the live line wraps (${rows.length} rows)`);
      assert.equal(rows.map(r => r.text.length).reduce((a, b) => a + b, 0), length, `${label}: every character drawn`);
      for (const row of rows) {
        const px = Number(/(\d+(?:\.\d+)?)px/.exec(row.font)?.[1] || 0);
        assert.ok(row.y - px * 0.8 >= 100, `${label}: a live row's top at ${row.y - px * 0.8} is under the header`);
      }
    }
  }
  {
    // FLAT draws its live line at the log's own size, so it keeps the 60% line where
    // CASCADE, with its giant line, needs more room (Codex QA of 61de5a9).
    const flat = await makeHud();
    flat.state.viewMode = "FLAT";
    flat.element("controlDock").rect = { top: 351, height: 37 };
    assert.equal(flat.getLogBottom(400, 90, 1280), 240, "FLAT keeps the 60% line");
    flat.state.viewMode = "CASCADE";
    assert.equal(flat.getLogBottom(400, 90, 1280), 275, "CASCADE makes room for its giant line");
  }
  // Every answer a HUD gives says whether it sits in a frame, top level and docked
  // (Codex QA of 61de5a9: with the flags deleted, both harnesses still passed).
  for (const framed of [false, true]) {
    const said = await makeHud({ framed });
    const hudBus = said.channels.find(c => String(c.name).startsWith("comfy-logblaster-bus:"));
    await hudBus.emit("message", { data: { type: "ping" } });
    // And the 1.5 s heartbeat, the answer the page hears most (Codex QA of ed6e097).
    const heartbeat = said.intervals.find(t => t.delay === 1500);
    assert.ok(heartbeat, "the HUD sets a 1.5 s heartbeat");
    const before = hudBus.posts.length;
    heartbeat.fn();
    assert.equal(hudBus.posts.length, before + 1, "the heartbeat answers");
    const answers = hudBus.posts.filter(m => m.type === "hello" || m.type === "pong");
    assert.ok(answers.some(m => m.type === "hello") && answers.filter(m => m.type === "pong").length >= 2,
      "a hello on load, a pong on a ping, and the heartbeat's");
    assert.ok(answers.every(m => m.embedded === framed), `framed=${framed}: every answer says so`);
  }

  // Common windows keep the live line full size; only a short one shrinks it
  // (Codex QA of ed6e097: a fit that always returned 1 still passed).
  for (const [w, h, dockTop, expectedPx] of [[1920, 1080, 1030, 46], [1366, 768, 718, 46],
                                             [820, 475, 360, 30], [1280, 310, 261, 36]]) {
    const sized = await makeHud();
    sized.textCanvas.width = w;
    sized.textCanvas.height = h;
    sized.element("controlDock").rect = { top: dockTop, height: 40 };
    // Long enough that no width auto-fits it onto one row (that path shrinks on purpose).
    sized.ingestLogChunk(`${"R".repeat(400)}\n`);
    for (let frame = 0; frame < 4; frame++) sized.renderTextTexture(2000 + frame * 1000, 1.0);
    sized.drawsAt.length = 0;
    sized.renderTextTexture(7000, 1.0);
    const live = sized.drawsAt.filter(d => /^R+$/.test(d.text) && d.text.length > 20);
    const sizes = [...new Set(live.map(d => Number(/(\d+(?:\.\d+)?)px/.exec(d.font)?.[1])))];
    assert.deepEqual(sizes, [expectedPx], `${w}x${h}: the live line at ${expectedPx}px`);
  }
  hud.state.activeNodeTitle = "Long workflow title ".repeat(6);
  hud.state.filterQuery = "needle";
  hud.ingestLogChunk("needle visible log\n");
  hud.draws.length = 0;
  hud.renderTextTexture(1000, 0.016);
  assert.ok(hud.draws.some(text => text.includes("FILTER")), "Long node titles must not hide filter state");

  await hud.setReducedMotion(true);
  assert.equal(hud.state.reducedMotion, true);
  assert.ok(hud.state.fxIntensity <= 0.6);
  assert.equal(hud.element("fxSlider").max, "60");
  hud.element("fxSlider").value = "160";
  await hud.element("fxSlider").emit("input");
  assert.ok(hud.state.fxIntensity <= 0.6);
  hud.handleComfyEvent("execution_start", { prompt_id: "reduced-start" });
  hud.ingestLogChunk("[ERROR] reduced motion event\n");
  assert.equal(hud.state.degaussWobble, 0);
  assert.equal(hud.state.glitchBurst, 0);
  assert.equal(hud.state.smoothScrollPx, 0);
  const reducedAtStartup = await makeHud({ reducedMotion: true });
  assert.ok(reducedAtStartup.state.fxIntensity <= 0.6);

  const telemetry = await makeHud();
  await telemetry.sockets[0].emit("open");
  await new Promise(resolve => setImmediate(resolve));
  telemetry.setTime(4000);
  await telemetry.pollTelemetry();
  assert.equal(telemetry.getTelemetryFreshness().kind, "fresh");
  telemetry.setFetchFailure(true);
  telemetry.setTime(10000);
  await telemetry.pollTelemetry();
  assert.equal(telemetry.getTelemetryFreshness().kind, "stale");
  assert.equal(telemetry.getTelemetryFreshness().ageSec, 6);
  telemetry.setTime(20000);
  await telemetry.pollTelemetry();
  assert.equal(telemetry.getTelemetryFreshness().kind, "offline");
  telemetry.draws.length = 0;
  telemetry.renderTextTexture(20000, 0.016);
  assert.ok(telemetry.draws.includes("TELEMETRY OFFLINE"));
  await telemetry.sockets[0].emit("close");
  telemetry.setTime(32000);
  assert.equal(telemetry.getConnectionStatus().offline, true);
  assert.equal(telemetry.getConnectionStatus().elapsed, "00:12");
  telemetry.setFetchFailure(false);
  await telemetry.pollTelemetry();
  assert.equal(telemetry.getTelemetryFreshness().kind, "fresh");

  const audio = await makeHud();
  audio.state.synthEnabled = true;
  audio.ensureAudioGraph();
  audio.state.blastRate = 1;
  audio.synthNodes().droneGain.gain.value = 0.2;
  audio.document.hidden = true;
  await audio.document.emit("visibilitychange");
  assert.equal(audio.state.blastRate, 0);
  const master = audio.synthNodes().synthMasterGain.gain;
  const drone = audio.synthNodes().droneGain.gain;
  assert.ok(master.events.some(e => e[0] === "linear" && e[1] === 0 && e[2] <= 1.1));
  assert.ok(drone.events.some(e => e[0] === "linear" && e[1] === 0 && e[2] <= 1.1));
  const nodeCount = audio.audioNodes.length;
  audio.playSynthTick("error", 1);
  assert.equal(audio.audioNodes.length, nodeCount, "Hidden synth must not create audible ticks");
  audio.document.hidden = false;
  await audio.document.emit("visibilitychange");
  assert.equal(master.value, 0.24);
  assert.equal(drone.value, 0);

  const privacy = await makeHud();
  for (const text of [
    '{"client_secret":"SYNTHETIC_VALUE"}', '{"refresh_token":"SYNTHETIC_VALUE"}',
    "db_password=SYNTHETIC_VALUE", "redis://:SYNTHETIC_VALUE@localhost:6379",
    "Authorization: Basic SYNTHETIC_VALUE==", '{"Authorization":"ApiKey SYNTHETIC_VALUE"}',
  ]) {
    resetLines(privacy);
    for (const ch of text) {
      privacy.ingestLogChunk(ch);
      assert.ok(!privacy.state.lines.at(-1).viewRaw.includes("SYNTHETIC"), `Streaming credential leaked: ${text.split(":")[0]}`);
    }
    assert.ok(privacy.state.lines.at(-1).viewRaw.includes("***REDACTED***"));
  }
  for (const path of ["C:\\Users\\fixture", "C:/Users/fixture", "/Users/fixture", "/home/fixture"]) {
    assert.equal(privacy.redactSensitiveText(path), "~");
    resetLines(privacy);
    for (const ch of path) privacy.ingestLogChunk(ch);
    assert.equal(privacy.state.lines.at(-1).viewRaw, "~");
  }
  assert.equal(privacy.redactSensitiveText("Basic setup: tokenizer=fast"), "Basic setup: tokenizer=fast");
}

function resetLines(hud) {
  Object.assign(hud.state, { lines: [], errorCount: 0, partialLineOpen: false,
    partialCrPending: false, partialRawText: "", paused: false, pausedAnchorId: null,
    scrollBackLines: 0, filterQuery: "" });
  hud.state.linesVersion++;
}

function viewedLine(hud) {
  const lines = hud.getFilteredLines();
  return lines[hud.getViewEndIndex(lines) - 1];
}

async function verifyTerminalTiming() {
  const hud = await makeHud();
  const event = (time, type, data) => {
    hud.setTime(time);
    hud.handleComfyEvent(type, data);
  };
  // Core sends success, task_done's queue status, then the finished prompt's null node.
  event(1000, "execution_start", { prompt_id: "core-a" });
  event(6000, "execution_success", { prompt_id: "core-a" });
  assert.equal(hud.state.lastElapsedSec, 5);
  event(6100, "status", { status: { exec_info: { queue_remaining: 1 } } });
  event(6200, "executing", { prompt_id: "core-a", node: null });
  assert.equal(hud.state.runState, "BLASTING");
  assert.equal(hud.state.runStartMs, 6100);
  assert.equal(hud.state.activePromptId, null);
  assert.equal(hud.state.lastElapsedSec, 5);
  event(7000, "execution_start", { prompt_id: "core-b" });
  event(12000, "execution_success", { prompt_id: "core-b" });
  assert.equal(hud.state.lastElapsedSec, 5);

  const terminals = [
    ["executing", { node: null }, "COMPLETE", "Workflow Complete"],
    ["execution_success", {}, "COMPLETE", "Workflow Complete"],
    ["execution_error", { node_id: "7", exception_message: "Test failure" }, "ERROR", "Execution Error"],
    ["execution_interrupted", {}, "IDLE", "Execution Interrupted"],
  ];
  let clock = 20000;
  for (const [type, data, expectedState, expectedTitle] of terminals) {
    const id = `normal-${type}`;
    event(clock, "execution_start", { prompt_id: id });
    event(clock + 5000, type, { ...data, prompt_id: id });
    assert.equal(hud.state.lastElapsedSec, 5, `${type} lost its observed duration`);
    event(clock + 8000, "executing", { prompt_id: id, node: null });
    event(clock + 10000, "execution_success", { prompt_id: id });
    assert.equal(hud.state.runState, expectedState, `${type} changed after a duplicate terminal`);
    assert.equal(hud.state.activeNodeTitle, expectedTitle);
    assert.equal(hud.state.lastElapsedSec, 5);

    event(clock + 12000, "execution_start", { prompt_id: `stale-${type}` });
    await hud.sockets[0].emit("close");
    event(clock + 90000, type, { ...data, prompt_id: `gap-${type}` });
    assert.equal(hud.state.activePromptId, `gap-${type}`);
    assert.equal(hud.state.runStartMs, clock + 90000);
    assert.equal(hud.state.lastElapsedSec, 0, `${type} reused a pre-disconnect timer`);
    assert.equal(hud.state.executionUncertain, false);
    assert.equal(hud.state.runState, expectedState);

    // A reconnect status supplies the observed start before a terminal reveals its ID.
    event(clock + 100000, "status", { status: { exec_info: { queue_remaining: 1 } } });
    event(clock + 105000, type, { ...data, prompt_id: `observed-${type}` });
    assert.equal(hud.state.activePromptId, `observed-${type}`);
    assert.equal(hud.state.runStartMs, clock + 100000);
    assert.equal(hud.state.lastElapsedSec, 5);
    assert.equal(hud.state.runState, expectedState);
    clock += 120000;
  }
  for (let i = 0; i < 40; i++) {
    event(clock + i * 2000, "execution_success", { prompt_id: `bounded-${i}` });
  }
  assert.equal(hud.state.completedPromptIds.size, 32);
}

(async () => {
  const hud = await makeHud();
  assert.equal(hud.channels[0].name, "comfy-logblaster-bus:/comfy");
  assert.match(hud.sockets[0].url, /^ws:\/\/example\.test:8000\/comfy\/ws\?clientId=/);
  assert.equal(hud.state.gpuTelemetry.metrics_mode, "vram_only");
  assert.ok(hud.fetches.some(x => x.url === "/comfy/system_stats"));
  hud.fetches.length = 0;
  hud.setTime(2000);
  await hud.pollTelemetry();
  assert.deepEqual(hud.fetches.map(x => x.url), ["/comfy/system_stats"]);
  hud.setTime(32001);
  hud.setNvmlAvailable(true);
  hud.fetches.length = 0;
  await hud.pollTelemetry();
  assert.deepEqual(hud.fetches.map(x => x.url), ["/comfy/logblaster/telemetry"]);
  assert.equal(hud.state.nvmlUnavailable, false);
  assert.equal(hud.state.gpuTelemetry.metrics_mode, "nvml");
  await hud.sockets[0].emit("open");
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(hud.fetches.some(x => x.url === "/comfy/internal/logs/subscribe"));
  assert.ok(hud.fetches.some(x => x.url === "/comfy/internal/logs/raw"));

  resetLines(hud);
  for (let i = 0; i < 20; i++) hud.ingestLogChunk(`line ${i}\n`);
  await hud.element("btnPause").click();
  const pausedId = viewedLine(hud).id;
  hud.ingestLogChunk("new output\nmore output\n");
  assert.equal(viewedLine(hud).id, pausedId);
  await hud.element("btnPause").click();
  await hud.window.emit("wheel", { deltaY: -1 });
  const scrolledId = viewedLine(hud).id;
  hud.ingestLogChunk("latest output\n");
  assert.equal(viewedLine(hud).id, scrolledId);

  resetLines(hud);
  hud.ingestLogChunk("partial");
  await hud.element("btnPause").click();
  const partialId = viewedLine(hud).id;
  hud.ingestLogChunk(" continued\nnext\n");
  assert.equal(viewedLine(hud).id, partialId);
  assert.equal(viewedLine(hud).viewRaw, "partial continued");

  resetLines(hud);
  for (let i = 0; i < 3500; i++) hud.ingestLogChunk(`history ${i}\n`);
  await hud.window.emit("wheel", { deltaY: -1 });
  const retainedId = viewedLine(hud).id;
  for (let i = 0; i < 100; i++) hud.ingestLogChunk(`appended ${i}\n`);
  assert.equal(hud.state.lines.length, 3500);
  assert.equal(viewedLine(hud).id, retainedId);
  for (let i = 0; i < 3500; i++) hud.ingestLogChunk(`replacement ${i}\n`);
  assert.equal(hud.getViewEndIndex(hud.getFilteredLines()), 1);
  assert.equal(viewedLine(hud).id, hud.state.lines[0].id);

  resetLines(hud);
  const json = JSON.stringify({ api_key: "SYNTHETIC_VALUE", password: 'SYNTHETIC "escaped" value' });
  // Every visible prefix, including one ending in an escape, must stay masked.
  for (const ch of json) {
    hud.ingestLogChunk(ch);
    const line = hud.state.lines.at(-1);
    assert.ok(!line.viewRaw.includes("SYNTHETIC"), "Streaming JSON leaked its value");
    assert.ok(!line.spans.map(s => s.text).join("").includes("SYNTHETIC"));
  }
  hud.ingestLogChunk("\n");
  assert.ok(hud.state.lines[0].viewRaw.includes("***REDACTED***"));
  hud.draws.length = 0;
  hud.renderTextTexture(40000, 0.016);
  assert.ok(!hud.draws.join(" ").includes("SYNTHETIC"));
  assert.ok(!hud.draws.join(" ").includes("escaped"));
  await hud.element("btnCopy").click();
  assert.ok(hud.copied().includes("***REDACTED***"));
  assert.ok(!hud.copied().includes("SYNTHETIC"));
  assert.ok(!hud.copied().includes("escaped"));

  const nodeMap = (prompt_id, title) => hud.channels[0].emit("message", {
    data: { type: "node_map", prompt_id, nodes: { 7: title } },
  });
  await nodeMap("run-a", "Workflow A");
  await nodeMap("run-b", "Workflow B");
  hud.setTime(50000);
  hud.handleComfyEvent("execution_start", { prompt_id: "run-a" });
  hud.handleComfyEvent("executing", { prompt_id: "run-a", node: "7" });
  assert.equal(hud.state.activeNodeTitle, "Workflow A (#7)");
  await nodeMap("run-b", "Changed Workflow B");
  await nodeMap(undefined, "Unscoped old extension");
  assert.equal(hud.state.activeNodeTitle, "Workflow A (#7)");
  hud.setTime(55000);
  hud.handleComfyEvent("execution_success", { prompt_id: "run-a" });
  hud.setTime(100000);
  hud.handleComfyEvent("executing", { prompt_id: "run-b", node: "7" });
  assert.equal(hud.state.runStartMs, 100000);
  assert.equal(hud.state.activeNodeTitle, "Changed Workflow B (#7)");
  hud.setTime(105000);
  hud.handleComfyEvent("execution_success", { prompt_id: "run-b" });
  assert.equal(hud.state.lastElapsedSec, 5);
  hud.setTime(200000);
  hud.handleComfyEvent("status", { status: { exec_info: { queue_remaining: 1 } } });
  assert.equal(hud.state.runStartMs, 200000);
  assert.equal(hud.state.activePromptId, null);

  // A disconnect can hide both the previous run's finish and the next run's start.
  await nodeMap("run-gap-a", "Workflow A");
  hud.setTime(300000);
  await hud.sockets[0].emit("message", { data: JSON.stringify({
    type: "execution_start", data: { prompt_id: "run-gap-a" },
  }) });
  await hud.sockets[0].emit("message", { data: JSON.stringify({
    type: "executing", data: { prompt_id: "run-gap-a", node: "7" },
  }) });
  assert.equal(hud.state.activeNodeTitle, "Workflow A (#7)");
  await hud.sockets[0].emit("close");
  assert.equal(hud.state.executionUncertain, true);
  hud.setTime(400000);
  hud.connectComfySocket();
  const reconnect = hud.sockets.at(-1);
  await reconnect.emit("open");
  await new Promise(resolve => setImmediate(resolve));
  await reconnect.emit("message", { data: JSON.stringify({
    type: "status", data: { status: { exec_info: { queue_remaining: 1 } } },
  }) });
  assert.equal(hud.state.runStartMs, 400000);
  assert.equal(hud.state.activePromptId, null);
  assert.equal(hud.state.nodeTitleMap.size, 0);
  hud.setTime(405000);
  await reconnect.emit("message", { data: JSON.stringify({
    type: "execution_success", data: { prompt_id: "run-after-gap" },
  }) });
  assert.equal(hud.state.lastElapsedSec, 5);
  assert.equal(hud.state.activePromptId, "run-after-gap");
  assert.equal(hud.state.nodeTitleMap.size, 0);
  assert.equal(hud.state.runState, "COMPLETE");
  await hud.window.emit("pagehide");
  assert.ok(hud.fetches.some(x => x.url === "/comfy/internal/logs/subscribe" && x.options.keepalive));
  assert.ok(hud.fetches.every(x => x.url.startsWith("/comfy/")));
  await verifyTerminalTiming();
  await verifyProductImprovements();
  await verifyDemoOwnership();
  console.log("HUD_INTEGRATION_OK");
})().catch(error => { console.error(error); process.exitCode = 1; });
