const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../js/log_blaster.js"), "utf8")
  .replace(/^import .*;\r?\n/gm, "");

/** Just enough DOM for the embedded dock: elements that hold children, take
 *  listeners and can be found and removed, and a body that knows what it holds. */
function makeDom() {
  const attached = new Set();
  const make = (tag) => {
    const node = {
      tag, style: {}, children: [], listeners: {}, textContent: "", title: "",
      append(...kids) { this.children.push(...kids); },
      appendChild(kid) { this.children.push(kid); },
      addEventListener(type, fn) { this.listeners[type] = fn; },
      querySelector(selector) {
        for (const kid of this.children) {
          if (kid.tag === selector) return kid;
          const deeper = kid.querySelector?.(selector);
          if (deeper) return deeper;
        }
        return null;
      },
      remove() { attached.delete(this); },
    };
    return node;
  };
  // One object per id, so what the script sets on an element can be read back.
  // The toolbar pill "exists", so mountToolbarControls leaves the stub alone.
  const byId = new Map();
  return {
    attached,
    document: {
      getElementById(id) {
        if (!byId.has(id)) byId.set(id, { id, title: id === "comfy-logblaster-launch" ? "Open LogBlaster" : "" });
        return byId.get(id);
      },
      createElement: make,
      body: { appendChild(node) { attached.add(node); }, contains(node) { return attached.has(node); } },
    },
  };
}

async function makeExtension(useFileUrl = true, { desktop = false, observers = 0, auto = false, ...extra } = {}) {
  const posts = [];
  const channels = [];
  const opens = [];
  const apiHandlers = {};
  const timers = [];
  let extension;
  let submit = async () => ({ prompt_id: "first" });
  const popup = { closed: false, location: { href: "about:blank" }, focus() {} };
  let openCount = 0;
  const api = {
    api_base: "/comfy",
    queuePrompt(...args) { return submit(...args); },
    addEventListener(type, fn) { apiHandlers[type] = fn; },
  };
  if (useFileUrl) api.fileURL = (route) => "/comfy" + route;
  const dom = makeDom();
  const window = { location: { href: "https://example.test/comfy/" }, screen: {},
    // Comfy Desktop hands every window.open to the system and answers null.
    open(...args) { openCount++; opens.push(args); return desktop ? null : popup; } };
  if (desktop) window.__comfyDesktop2 = {};
  if ("electronAPI" in extra) window.electronAPI = extra.electronAPI;
  const fetches = [];
  const context = vm.createContext({
    api, URL, Date, Map,
    app: { registerExtension(value) { extension = value; } },
    localStorage: { getItem() { return auto ? "1" : "0"; }, setItem() {} },
    document: dom.document,
    window,
    async fetch(url) { fetches.push(url); return { ok: true, async json() { return { observers, version: "9.9.9" }; } }; },
    setTimeout(fn) { timers.push(fn); return timers.length; }, clearTimeout() {},
    BroadcastChannel: class {
      constructor(name) { this.name = name; channels.push(this); }
      addEventListener(type, fn) { this.listener = fn; }
      postMessage(msg) { posts.push(msg); }
    },
  });
  vm.runInContext(source + `\nthis.harness = {
    HUD_URL, TELEMETRY_URL, WINDOW_NAME, openLogBlasterWindow, isHudWindowAlive,
    popOutHud, openEmbeddedDock, dockUnlessWatched,
    dock: () => dockEl,
    setPopup(ref) { popupRef = ref; lastPongMs = 0; },
    maps: promptNodeMaps,
  };`, context);
  await extension.setup();
  return { api, posts, channels, popup, opens, fetches, dom, apiHandlers, harness: context.harness,
    setSubmit(fn) { submit = fn; }, openCount() { return openCount; },
    /** Run every timer set so far (the toolbar mount, the run grace period), then
     *  let the fetches they start settle. */
    async flushTimers() {
      for (const fn of timers.splice(0)) await fn();
      await new Promise(resolve => setImmediate(resolve));
    } };
}

(async () => {
  const env = await makeExtension();
  assert.equal(env.harness.HUD_URL, "/comfy/logblaster");
  assert.equal(env.harness.WINDOW_NAME, "ComfyUILogBlasterHUD:/comfy");
  assert.equal(env.channels[0].name, "comfy-logblaster-bus:/comfy");
  assert.equal((await makeExtension(false)).harness.HUD_URL, "/comfy/logblaster");
  let finish;
  env.setSubmit(() => new Promise(resolve => { finish = resolve; }));
  const prompt = { workflow: { nodes: [{ id: 7, title: "Submitted workflow" }] },
    output: { "7": { class_type: "StageCue" } } };
  const pending = env.api.queuePrompt(0, prompt);
  prompt.workflow.nodes[0].title = "Different tab or edited graph";
  assert.equal(env.posts.filter(x => x.type === "node_map").length, 0);
  const result = { prompt_id: "run-a" };
  finish(result);
  assert.equal(await pending, result);
  assert.equal(env.posts.at(-1).nodes["7"], "Submitted workflow");
  assert.equal(env.posts.at(-1).prompt_id, "run-a");

  env.setSubmit(async () => ({ prompt_id: "run-b" }));
  await env.api.queuePrompt(0, { output: { "7": { _meta: { title: "Other workflow" } } } });
  assert.equal(env.posts.at(-1).prompt_id, "run-b");
  assert.equal(env.posts.at(-1).nodes["7"], "Other workflow");
  env.posts.length = 0;
  env.channels[0].listener({ data: { type: "hello" } });
  assert.deepEqual(env.posts.map(x => x.prompt_id), ["run-a", "run-b"]);
  assert.equal(env.posts[0].nodes["7"], "Submitted workflow");

  const failure = new Error("expected queue rejection");
  env.setSubmit(async () => { throw failure; });
  await assert.rejects(env.api.queuePrompt(0, {}), error => error === failure);
  assert.equal(env.harness.maps.size, 2);

  env.harness.setPopup(env.popup);
  env.popup.location.href = "https://example.test/comfy/logblaster?view=dual";
  env.harness.openLogBlasterWindow(true);
  assert.equal(env.openCount(), 0);
  assert.equal(env.popup.location.href, "https://example.test/comfy/logblaster?view=dual");
  env.popup.location.href = "https://example.test/comfy/another-page";
  assert.equal(env.harness.isHudWindowAlive(), false);
  env.harness.openLogBlasterWindow(true);
  assert.equal(env.openCount(), 1);
  assert.equal(env.popup.location.href, "/comfy/logblaster");

  env.channels[0].postMessage = () => { throw new Error("channel unavailable"); };
  env.setSubmit(async () => result);
  assert.equal(await env.api.queuePrompt(0, {}), result);
  assert.equal(env.harness.TELEMETRY_URL, "/comfy/logblaster/telemetry");

  // Pop Out from the dock, in a browser (2026-10-30): the docked HUD answers the
  // ping (it says it is in a frame), Pop Out closes the dock, and that answer must
  // not stand in for a live window -- it did, and Pop Out opened nothing.
  const browser = await makeExtension();
  browser.harness.openEmbeddedDock();
  assert.ok(browser.harness.dock(), "the dock opens");
  browser.channels[0].listener({ data: { type: "pong", embedded: true } });
  browser.channels[0].listener({ data: { type: "hello", embedded: true } });
  assert.equal(browser.harness.isHudWindowAlive(), true, "the open dock is a live HUD");
  const popBtn = browser.harness.dock().querySelector("div").children[1].children[0];
  assert.equal(popBtn.textContent, "Pop Out Window");
  popBtn.listeners.click();
  assert.equal(browser.harness.dock(), null, "Pop Out closes the dock");
  assert.equal(browser.openCount(), 1, "Pop Out opens a window after closing the dock");
  assert.equal(browser.opens[0][1], browser.harness.WINDOW_NAME);

  // A HUD in a tab of its own keeps counting when the dock closes, so a run right
  // after does not open a second HUD (Codex QA of 8778e8f: clearing every answer on
  // dock close did exactly that).
  const twoHuds = await makeExtension(true, { auto: true });
  twoHuds.harness.openEmbeddedDock();
  twoHuds.channels[0].listener({ data: { type: "pong", embedded: true } });
  twoHuds.channels[0].listener({ data: { type: "pong" } });
  twoHuds.harness.dock().querySelector("div").children[1].children[1].listeners.click();
  assert.equal(twoHuds.harness.dock(), null, "X closes the dock");
  assert.equal(twoHuds.harness.isHudWindowAlive(), true, "the HUD tab still counts");
  await twoHuds.api.queuePrompt(0, {});
  assert.equal(twoHuds.openCount(), 0, "a run wakes the HUD tab instead of opening another");
  assert.ok(twoHuds.posts.some(m => m.type === "wake"));

  // Comfy Desktop hands every window.open to the system: never an empty URL
  // (about:blank -> "you'll need a new app to open this about link").
  const desk = await makeExtension(true, { desktop: true });
  desk.harness.openLogBlasterWindow(true);
  assert.ok(desk.harness.dock(), "the top-bar button docks the HUD inside Comfy Desktop");
  assert.equal(desk.openCount(), 0, "and asks the system for no window");
  const deskPop = desk.harness.dock().querySelector("div").children[1].children[0];
  assert.equal(deskPop.textContent, "Open in Browser");
  deskPop.listeners.click();
  assert.equal(desk.harness.dock(), null);
  assert.deepEqual(desk.opens, [["https://example.test/comfy/logblaster", "_blank"]],
    "Pop Out hands the HUD's real address to the browser, once");

  // A run in Comfy Desktop docks the HUD only when no HUD is watching (the server
  // counts the one in the browser), and never opens a window of its own.
  const watched = await makeExtension(true, { desktop: true, observers: 1 });
  watched.harness.openLogBlasterWindow(false);
  await watched.harness.dockUnlessWatched();
  assert.equal(watched.harness.dock(), null, "a HUD in the browser keeps the dock shut");
  assert.deepEqual(watched.fetches.slice(0, 1), ["/comfy/logblaster/telemetry"]);
  const unwatched = await makeExtension(true, { desktop: true, observers: 0 });
  await unwatched.harness.dockUnlessWatched();
  assert.ok(unwatched.harness.dock(), "with nothing watching, a run docks the HUD");

  // The paths a run actually takes in Comfy Desktop, with AUTO on: Run (the
  // queuePrompt hook), a headless run's execution_start, and the queue's status
  // edge -- each through its grace timer. None may ask the system for a window.
  const running = await makeExtension(true, { desktop: true, observers: 0, auto: true });
  await running.api.queuePrompt(0, {});
  await running.flushTimers();
  assert.ok(running.harness.dock(), "Run docks the HUD inside Comfy Desktop");
  running.harness.dock().querySelector("div").children[1].children[1].listeners.click();
  running.apiHandlers.execution_start();
  await running.flushTimers();
  assert.ok(running.harness.dock(), "a headless run docks it again after the grace period");
  running.harness.dock().querySelector("div").children[1].children[1].listeners.click();
  running.apiHandlers.status({ detail: { exec_info: { queue_remaining: 0 } } });
  running.apiHandlers.status({ detail: { exec_info: { queue_remaining: 2 } } });
  await running.flushTimers();
  assert.ok(running.harness.dock(), "and so does the queue filling");
  const launcherTitle = running.dom.document.getElementById("comfy-logblaster-launch").title;
  assert.ok(String(launcherTitle).includes("LogBlaster v9.9.9"), "the top-bar tooltip names the version");

  // A stray null or primitive window.electronAPI is not Comfy Desktop: the button
  // still opens the named HUD window. The older Desktop's real bridge object is.
  for (const value of [null, 0, "yes", undefined]) {
    const stray = await makeExtension(true, { electronAPI: value });
    stray.harness.openLogBlasterWindow(true);
    assert.equal(stray.harness.dock(), null, `electronAPI=${String(value)} docks nothing`);
    assert.deepEqual(stray.opens.map(args => args[1]), [stray.harness.WINDOW_NAME],
      `electronAPI=${String(value)} opens the named window`);
  }
  const olderDesktop = await makeExtension(true, { electronAPI: { getPlatform() {} } });
  olderDesktop.harness.openLogBlasterWindow(true);
  assert.ok(olderDesktop.harness.dock(), "the older Desktop's bridge docks the HUD");
  assert.equal(olderDesktop.openCount(), 0);
  for (const env2 of [desk, watched, unwatched, running]) {
    assert.equal(env2.openCount(), env2 === desk ? 1 : 0, "a run in Comfy Desktop opens no window");
    assert.ok(!env2.opens.some(args => args[0] === "" || args[0] === "about:blank"),
      "Comfy Desktop is never asked to open about:blank");
  }
  console.log("EXTENSION_UNIT_OK");
})().catch(error => { console.error(error); process.exitCode = 1; });
