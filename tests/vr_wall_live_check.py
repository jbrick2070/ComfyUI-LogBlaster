"""Live check of the VR wall (a maintainer tool): the real /logblaster page from a
running ComfyUI, in headless Chrome with WebGL2 (SwiftShader), with a simulated
headset injected before the page loads. Prints one PASS/FAIL line per check and
saves the headset view as PNGs. Nothing is rendered and no workflow is queued.

    python tests/vr_wall_live_check.py --url http://127.0.0.1:8188/logblaster

Needs Chrome and the websocket-client package (pip install websocket-client);
neither is a dependency of the node pack. CHROME may point at the browser.
"""
import argparse
import base64
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket

ARGS = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ARGS.add_argument("--url", default="http://127.0.0.1:8188/logblaster")
ARGS.add_argument("--out", default=None, help="where the headset views are saved (default: a temp folder)")
ARGS.add_argument("--port", type=int, default=9335, help="Chrome's DevTools port")
OPTS = ARGS.parse_args()
URL = OPTS.url
OUT = Path(OPTS.out or tempfile.mkdtemp(prefix="logblaster_vr_live_"))
OUT.mkdir(parents=True, exist_ok=True)
CHROME = os.environ.get("CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
PORT = OPTS.port

FAKE_XR = r"""
(() => {
  try { localStorage.setItem("logblaster.helpSeen", "1"); } catch (_) {}
  const log = [];
  const listeners = {};
  let pending = null;
  window.__xrLog = log;
  window.__winRaf = 0;
  const realRaf = window.requestAnimationFrame.bind(window);
  window.requestAnimationFrame = (cb) => { window.__winRaf++; return realRaf(cb); };
  const session = {
    inputSources: [],
    renderState: {},
    updateRenderState(s) { log.push("renderState"); Object.assign(this.renderState, s); },
    async requestReferenceSpace(type) { log.push("ref:" + type); return { type }; },
    requestAnimationFrame(cb) { pending = cb; return 1; },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    async end() { log.push("end"); for (const fn of listeners.end || []) fn({}); },
  };
  window.__xr = {
    session,
    fire(type) { for (const fn of listeners[type] || []) fn({}); },
    step(t, frame) { const cb = pending; pending = null; if (cb) cb(t, frame); return Boolean(pending); },
  };
  Object.defineProperty(navigator, "xr", { configurable: true, value: {
    async isSessionSupported(mode) { return mode === "immersive-vr"; },
    async requestSession(mode) { log.push("session:" + mode); return session; },
  } });
  // The GPU switch makeXRCompatible can make on a two-GPU laptop: a real loss and
  // restore of the page's own context, which the page must ride through.
  WebGL2RenderingContext.prototype.makeXRCompatible = async function () {
    log.push("makeXRCompatible");
    const ext = this.getExtension("WEBGL_lose_context");
    const canvas = this.canvas;
    const restored = new Promise((resolve) => canvas.addEventListener("webglcontextrestored", resolve, { once: true }));
    ext.loseContext();
    await new Promise((r) => setTimeout(r, 60));
    ext.restoreContext();
    await restored;
    log.push("restored");
  };
  window.XRWebGLLayer = class {
    constructor(s, gl) { this.gl = gl; this.framebuffer = null; }
    getViewport() { return { x: 0, y: 0, width: this.gl.drawingBufferWidth, height: this.gl.drawingBufferHeight }; }
  };
})();
"""

FRAME = r"""
(yaw, t) => {
  const gl = document.getElementById("glCanvas").getContext("webgl2");
  const aspect = gl.drawingBufferWidth / gl.drawingBufferHeight;
  const f = 1 / Math.tan(Math.PI / 6), near = 0.05, far = 50;
  const P = new Float32Array([f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) / (near - far), -1,
                              0, 0, 2 * far * near / (near - far), 0]);
  const c = Math.cos(yaw), s = Math.sin(yaw);
  const inv = new Float32Array([c, 0, -s, 0, 0, 1, 0, 0, s, 0, c, 0, 0, 0, 0, 1]);   // rotY(yaw)
  return window.__xr.step(t, { getViewerPose: () => ({ views: [{ projectionMatrix: P, transform: { inverse: { matrix: inv } } }] }) });
}
"""


class Page:
    def __init__(self):
        self.proc = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
                                      "--window-size=1920,1080", "--hide-scrollbars", "--use-angle=swiftshader",
                                      "--enable-unsafe-swiftshader", f"--user-data-dir={OUT / 'profile'}",
                                      "about:blank"])
        tab = None
        for _ in range(100):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
                tab = next(t for t in tabs if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.2)
        if tab is None:
            self.proc.terminate()
            raise SystemExit(f"Chrome did not open its DevTools port {PORT}; is CHROME right ({CHROME})?")
        self.ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=120, suppress_origin=True)
        self.n = 0
        self.errors = []

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("method") == "Runtime.exceptionThrown":
                self.errors.append(msg["params"]["exceptionDetails"].get("exception", {}).get("description", "?"))
            if msg.get("method") == "Runtime.consoleAPICalled" and msg["params"]["type"] == "error":
                self.errors.append(" ".join(str(a.get("value", a.get("description", ""))) for a in msg["params"]["args"]))
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description"))
        return r.get("result", {}).get("value")

    def shot(self, name):
        (OUT / name).write_bytes(base64.b64decode(self.call("Page.captureScreenshot", format="png")["data"]))


results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""))


page = Page()
try:
    page.call("Page.enable")
    page.call("Runtime.enable")
    page.call("Emulation.setDeviceMetricsOverride", width=1920, height=1080, deviceScaleFactor=1, mobile=False)
    page.call("Page.addScriptToEvaluateOnNewDocument", source=FAKE_XR)
    page.call("Page.navigate", url=URL)
    time.sleep(4)
    page.js("document.getElementById('btnTestBlast').click()")
    # The synth builds the Web Audio graph, so a real browser runs every wiring
    # call; a wiring that throws shows up in the "no page errors" check below.
    page.js("document.getElementById('btnSynth').click()")
    time.sleep(2.5)
    check("the synth's audio graph is built", page.js("document.getElementById('btnSynth').textContent") == "SYNTH: ON")
    check("VR button shows with WebGL2 and a reported headset", page.js("!document.getElementById('btnVR').hidden"))
    page.shot("flat_before.png")
    flat_size = page.js("[document.getElementById('glCanvas').width, document.getElementById('glCanvas').height]")
    raf0 = page.js("window.__winRaf"); time.sleep(0.6); raf1 = page.js("window.__winRaf")
    check("the flat loop runs before VR", raf1 > raf0, f"{raf0}->{raf1}")

    page.js("document.getElementById('btnVR').click()")
    for _ in range(50):
        if page.js("document.getElementById('btnVR').textContent") == "EXIT VR":
            break
        time.sleep(0.1)
    log = page.js("window.__xrLog")
    check("entry: session first, then the GPU switch, layer and local space",
          log[:5] == ["session:immersive-vr", "makeXRCompatible", "restored", "renderState", "ref:local"], str(log))
    check("button reads EXIT VR", page.js("document.getElementById('btnVR').textContent") == "EXIT VR")
    vr_size = page.js("[document.getElementById('glCanvas').width, document.getElementById('glCanvas').height]")
    check("VR raster is its own size, not the window's", vr_size == [2149, 1209], f"flat {flat_size}, vr {vr_size}")
    raf0 = page.js("window.__winRaf"); time.sleep(0.8); raf1 = page.js("window.__winRaf")
    check("the window loop stays off in VR, through the context restore", raf1 - raf0 <= 1, f"{raf0}->{raf1}")

    t = 100000.0
    def frames(yaw_from, yaw_to, count):
        global t
        alive = True
        for i in range(count):
            y = yaw_from + (yaw_to - yaw_from) * (i + 1) / count
            alive = page.js(f"({FRAME})({y}, {t})")
            t += 1000 / 72
        return alive
    check("headset frames keep requesting the next", frames(0, 0, 40))
    page.shot("vr_ahead.png")
    frames(0, math.radians(-45), 30); page.shot("vr_look_left.png")
    frames(math.radians(-45), math.radians(45), 50); page.shot("vr_look_right.png")
    frames(math.radians(45), 0, 30)

    preset0 = page.js("localStorage.getItem('logblaster.preset')")
    page.js("window.__xr.fire('selectstart')"); frames(0, 0, 3)
    check("trigger: next shader", page.js("localStorage.getItem('logblaster.preset')") != preset0,
          f"{preset0}->{page.js('localStorage.getItem(`logblaster.preset`)')}")
    view0 = page.js("localStorage.getItem('logblaster.viewMode')")
    page.js("window.__xr.fire('squeezestart')"); frames(0, 0, 3)
    check("grip: next view", page.js("localStorage.getItem('logblaster.viewMode')") != view0)
    arc0 = int(page.js("localStorage.getItem('logblaster.vrArcDeg')") or 110)
    page.js("window.__xr.session.inputSources = [{ gamepad: { axes: [0, 0, 0.9, 0] } }]"); frames(0, 0, 2)
    page.js("window.__xr.session.inputSources = []"); frames(0, 0, 30)
    arc1 = int(page.js("localStorage.getItem('logblaster.vrArcDeg')") or 110)
    check("thumbstick right: wider wall, one step", arc1 == min(160, arc0 + 10), f"{arc0}->{arc1}")
    page.shot("vr_wider.png")

    page.js("window.__xr.session.end()")
    time.sleep(1.0)
    raf0 = page.js("window.__winRaf"); time.sleep(0.8); raf1 = page.js("window.__winRaf")
    check("exit: the window loop resumes", raf1 > raf0, f"{raf0}->{raf1}")
    check("exit: the flat raster returns to the window's size",
          page.js("[document.getElementById('glCanvas').width, document.getElementById('glCanvas').height]") == flat_size)
    check("exit: button reads VR", page.js("document.getElementById('btnVR').textContent") == "VR")
    page.shot("flat_after.png")

    # A context lost while a session runs ends that session; the restore brings back the flat page.
    page.js("window.__xrLog.length = 0; document.getElementById('btnVR').click()")
    time.sleep(1.5)
    frames(0, 0, 5)
    page.js("window.__lose = document.getElementById('glCanvas').getContext('webgl2').getExtension('WEBGL_lose_context'); window.__lose.loseContext()")
    time.sleep(0.3)
    check("a lost context ends a running session", "end" in page.js("window.__xrLog"), str(page.js("window.__xrLog")))
    page.js("window.__lose.restoreContext()")
    time.sleep(1.0)
    raf0 = page.js("window.__winRaf"); time.sleep(0.8); raf1 = page.js("window.__winRaf")
    check("after the restore the flat loop runs again", raf1 > raf0, f"{raf0}->{raf1}")
    check("no page errors", not page.errors, "; ".join(page.errors[:3]))
finally:
    page.proc.terminate()
print(f"{sum(results)}/{len(results)} checks passed; views in {OUT}")
sys.exit(0 if all(results) else 1)
