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
  return {
    attached,
    document: {
      getElementById() { return {}; },
      createElement: make,
      body: { appendChild(node) { attached.add(node); }, contains(node) { return attached.has(node); } },
    },
  };
}

async function makeExtension(useFileUrl = true, { desktop = false, observers = 0 } = {}) {
  const posts = [];
  const channels = [];
  const opens = [];
  let extension;
  let submit = async () => ({ prompt_id: "first" });
  const popup = { closed: false, location: { href: "about:blank" }, focus() {} };
  let openCount = 0;
  const api = {
    api_base: "/comfy",
    queuePrompt(...args) { return submit(...args); },
    addEventListener() {},
  };
  if (useFileUrl) api.fileURL = (route) => "/comfy" + route;
  const dom = makeDom();
  const window = { location: { href: "https://example.test/comfy/" }, screen: {},
    // Comfy Desktop hands every window.open to the system and answers null.
    open(...args) { openCount++; opens.push(args); return desktop ? null : popup; } };
  if (desktop) window.__comfyDesktop2 = {};
  const fetches = [];
  const context = vm.createContext({
    api, URL, Date, Map,
    app: { registerExtension(value) { extension = value; } },
    localStorage: { getItem() { return "0"; }, setItem() {} },
    document: dom.document,
    window,
    async fetch(url) { fetches.push(url); return { ok: true, async json() { return { observers, version: "9.9.9" }; } }; },
    setTimeout() { return 1; }, clearTimeout() {},
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
  return { api, posts, channels, popup, opens, fetches, dom, harness: context.harness,
    setSubmit(fn) { submit = fn; }, openCount() { return openCount; } };
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
  // ping, Pop Out closes the dock, and that answer must not stand in for a live
  // window -- it did, and Pop Out closed the dock and opened nothing.
  const browser = await makeExtension();
  browser.harness.openEmbeddedDock();
  assert.ok(browser.harness.dock(), "the dock opens");
  browser.channels[0].listener({ data: { type: "pong" } });
  assert.equal(browser.harness.isHudWindowAlive(), true);
  const popBtn = browser.harness.dock().querySelector("div").children[1].children[0];
  assert.equal(popBtn.textContent, "Pop Out Window");
  popBtn.listeners.click();
  assert.equal(browser.harness.dock(), null, "Pop Out closes the dock");
  assert.equal(browser.openCount(), 1, "Pop Out opens a window after closing the dock");
  assert.equal(browser.opens[0][1], browser.harness.WINDOW_NAME);

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
  for (const env2 of [desk, watched, unwatched]) {
    assert.ok(!env2.opens.some(args => args[0] === "" || args[0] === "about:blank"),
      "Comfy Desktop is never asked to open about:blank");
  }
  console.log("EXTENSION_UNIT_OK");
})().catch(error => { console.error(error); process.exitCode = 1; });
