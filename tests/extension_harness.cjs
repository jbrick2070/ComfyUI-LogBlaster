const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../js/log_blaster.js"), "utf8")
  .replace(/^import .*;\r?\n/gm, "");

async function makeExtension(useFileUrl = true) {
  const posts = [];
  const channels = [];
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
  const context = vm.createContext({
    api, URL, Date, Map,
    app: { registerExtension(value) { extension = value; } },
    localStorage: { getItem() { return "0"; }, setItem() {} },
    document: { getElementById() { return {}; }, body: { contains() { return false; } } },
    window: { location: { href: "https://example.test/comfy/" }, screen: {},
      open() { openCount++; return popup; } },
    setTimeout() { return 1; }, clearTimeout() {},
    BroadcastChannel: class {
      constructor(name) { this.name = name; channels.push(this); }
      addEventListener(type, fn) { this.listener = fn; }
      postMessage(msg) { posts.push(msg); }
    },
  });
  vm.runInContext(source + `\nthis.harness = {
    HUD_URL, WINDOW_NAME, openLogBlasterWindow, isHudWindowAlive,
    setPopup(ref) { popupRef = ref; lastPongMs = 0; },
    maps: promptNodeMaps,
  };`, context);
  await extension.setup();
  return { api, posts, channels, popup, harness: context.harness,
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
  console.log("EXTENSION_UNIT_OK");
})().catch(error => { console.error(error); process.exitCode = 1; });
