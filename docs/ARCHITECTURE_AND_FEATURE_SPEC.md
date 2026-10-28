# ComfyUI-LogBlaster -- Hardened Architecture, Coding & Wiring Specification (Post-R3)

## [S1] Vision, Scope & Bounded Render Budget
`ComfyUI-LogBlaster` is a standalone ComfyUI custom-node pack serving a real-time Audio-Reactive WebGL2 GPU Log-Text Visualizer at `GET /logblaster`.

### Bounded Overhead & Memory Contract
1. **Best-Effort Low-Power GPU Hint + Honest Adapter Disclosure:** WebGL2 initializes with `powerPreference: "low-power"` (`?gpu=high` opt-in). Inspects `WEBGL_debug_renderer_info` (`UNMASKED_RENDERER_WEBGL`) and displays `FX: <adapter> (<fps>fps <fpsMode>)` (`"WebGL2 (Adapter Masked)"` when masked).
2. **Hard Render-Target Memory Ceiling, FBO Completeness & Context-Loss Recovery:**
   - Clamp render dimensions to `dpr <= 1.5`, `MAX_RENDER_WIDTH = 2240`, and `MAX_RENDER_PIXELS = 2_600_000` (storing `textCanvas._dpr = w / cssW`) so `textTex` + ping-pong RGBA8 FBOs (`texA`, `texB`) never exceed ~30 MiB combined even on 4K/5K displays.
   - Check `gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE`; fall back automatically to single-pass `u_textTex` rendering if incomplete.
   - Handle `webglcontextlost` (`ev.preventDefault()`, cancel active rAF, mark `state.contextLost = true`) and `webglcontextrestored` (rebuild programs, VAO, textures, and FBOs, resume rAF).
3. **Eco Frame-Pacing Governor & True Hidden Pause:**
   - `ECO` (default): **30 FPS** while `BLASTING` or audio/scroll/pulse animations are active; **15 FPS** when quiet/idle. `60`: 60 FPS cap. `MAX`: uncapped refresh.
   - When `document.hidden` is true, cancel and stop scheduling `requestAnimationFrame` (`rafId = 0`) and skip telemetry polling; resume on `visibilitychange`.
   - Dirty-gate 2D canvas rasterization + `gl.texSubImage2D(u_textTex)` (`480ms` idle cursor refresh) and skip Pass 1 FBO draw when `fxIntensity < 0.02` or `!state.fboReady`.
   - All `localStorage` access in both `/logblaster` and `js/log_blaster.js` is wrapped in `try / catch` with in-memory fallback.

## [S2] Explicit Auto-Popup Lifecycle, Directed Event Forwarding & Teardown Hygiene
1. **Mode A (UI Synchronous Queue -- `js/log_blaster.js`):**
   - Wraps `api.queuePrompt` once (`api._logBlasterWrapped`), isolating all pre-hook logic inside `try { ... } catch (_) {}` so no HUD/DOM/storage exception can ever prevent `origQueuePrompt(...args)` from submitting a prompt.
   - Calls `openLogBlasterWindow(false)` (does not steal focus when the HUD is already open). Uses `window.open("", "ComfyUILogBlasterHUD", features)` and navigates to `/logblaster` only when `opened.location.href === "about:blank"`, re-attaching existing background-throttled popup windows without reloading or clearing their 3,500-line history.
2. **Mode B (Open-UI External API Queue):**
   - Triggers auto-open check on both `execution_start` and broadcast `status` (`queue_remaining` rising `0 -> >0`, covering headless API callers that omit `client_id`).
   - Pings `BroadcastChannel("comfy-logblaster-bus")` (180ms grace); if unopened and popup-blocked, opens `openEmbeddedDock()` (`<iframe>` with `allow="clipboard-write; fullscreen; microphone; display-capture; autoplay"`, `allowFullscreen = true`, and `iframe.src = "about:blank"` teardown on close).
   - Answers `{ type: "hello" }` from `/logblaster` with `{ type: "node_map", replace: true, nodes }` (reading `app.graph.nodes || app.graph._nodes`), and relays workflow events (`comfy_event`) over `BroadcastChannel`.
3. **Mode C (Headless Server, No UI Tab Open):**
   - Opt-in `scripts/watch_and_pop.ps1 -HostName 127.0.0.1 -Port 8000` polling lightweight `GET /prompt` (`exec_info.queue_remaining`), logging a one-time reachability warning on connection error, checking `Win32_Process` (`msedge.exe`/`chrome.exe` command lines) before spawning `launch_log_blaster.bat` (Edge `--app`, Chrome `--app`, or default browser fallback).
4. **Directed Execution Event Observer Tap (`__init__.py`):**
   - ComfyUI `PromptExecutor` sends `execution_start`, `executing`, `progress`, `execution_error`, `execution_success`, and `execution_interrupted` with `sid = self.server.client_id` (`PromptServer.send_json` only broadcasts when `sid is None`).
   - `register_routes(prompt_server)` installs an idempotent wrapper on `prompt_server.send_sync` (`_logblaster_send_sync_wrapped`) that forwards `_OBSERVED_EXECUTION_EVENTS` (`execution_start`, `executing`, `progress`, `execution_error`, `execution_success`, `execution_interrupted`, `execution_cached`) when `sid is not None` to all connected `logblaster-*` observer sockets in `prompt_server.sockets`.
   - `/logblaster` keeps its own unique `logblaster-<uuid>` `clientId` (never colliding with `server.py`'s `self.sockets.pop(sid, None)`), deduplicates events across `/ws` + `BroadcastChannel` via `lastEventSigByType`, and handles `status` (`queue_remaining`) and `execution_interrupted`.
5. **Subscribe-Before-Backfill, Reconnect Gap Recovery & `pagehide`/`pageshow` Hygiene:**
   - On `/ws` `open`, `syncLogsAfterOpen(gen)` records `state.logSubAttempted = true`, sends `PATCH /internal/logs/subscribe` `{ enabled: true }` (4s `AbortController`) **before** `GET /internal/logs/raw` (4s `AbortController`), buffers live `/ws` `logs` messages during backfill, tracks `state.lastLogIsoT` to backfill missed entries across reconnects while skipping already-ingested timestamps (`<= cutoffIso`), and drains buffered live entries newer than `lastLogIsoT`.
   - On `pagehide`, sets `state.shuttingDown = true`, increments `state.lifecycleGen`, cancels rAF and all timers, disconnects external audio, closes `ws` and `bus` without scheduling reconnects, and sends `PATCH /internal/logs/subscribe` (`enabled: false, keepalive: true`) whenever `state.logSubscribed || state.logSubAttempted`. Reloads cleanly on `pageshow` (`if (ev.persisted && state.shuttingDown)`).

## [S3] Backend Routes, Non-Blocking I/O, Companion Node & Hardware Telemetry
1. **Package-Owned Routes (`__init__.py`):**
   - Uses `if __package__ not in (None, ""):` for package-relative imports so internal import errors never fall through to ComfyUI core's top-level `nodes` module.
   - Dual idempotency guards: `_comfyui_logblaster_singleton` in `sys.modules` + `_logblaster_routes_registered` on `prompt_server`.
   - Both `GET /logblaster` (file read) and `GET /logblaster/telemetry` (`get_gpu_telemetry`) execute via `await asyncio.to_thread(...)` so ComfyUI's `aiohttp` event loop never blocks on disk I/O or first-call `ctypes.CDLL` / `nvmlInit_v2`.
2. **Companion Node (`nodes.py` -- `LogBlaster_StageCue`):**
   - Pass-through stage marker (`banner`, `fx_trigger`, optional `passthrough`, optional `always_fire=False`).
   - `IS_CHANGED` returns `float("nan")` **only** when `always_fire` is `True` (otherwise `f"{fx_trigger}:{banner}"`), preserving ComfyUI's downstream execution cache by default when wired inline.
3. **Hardware Telemetry (`telemetry.py` + Single-Flight `GET /system_stats` Fallback):**
   - `telemetry.py` resolves target GPU via `resolve_nvml_target()`: respects explicit `LOGBLASTER_NVML_INDEX` (digits or `GPU-...` UUID) **without** silently falling back to GPU 0 when invalid/out-of-range; supports integer index or `GPU-...` UUID (`nvmlDeviceGetHandleByUUID`) in `CUDA_VISIBLE_DEVICES`; includes `"device_index"` in snapshot.
   - `pollTelemetry()` uses a single-flight guard (`telemetryInFlight`) and latches `state.nvmlUnavailable = true` once `/logblaster/telemetry` reports `available: false`, querying `/system_stats` directly on non-NVIDIA hosts without double-fetching.

## [S4] Partial-Line Chunk Framing, Streamer Privacy Redaction, Cached Filter & Error-Jump
1. **Stateful Partial-Line Chunk Framing (`ingestLogChunk`):**
   - `app/logger.py` emits raw `sys.stdout.write(data)` chunks (`print("x")` -> `"x"` then `"\n"`).
   - `ingestLogChunk()` maintains `state.partialLineOpen`, `state.partialCrPending`, and `state.partialRawText` (tokenizing via `/[^\r\n]+|\r|\n/g`), concatenating split non-newline chunks onto the open line (and re-running `redactSensitiveText` across the joined line so secrets split across chunk boundaries are redacted immediately), closing open lines cleanly on `"\n"` or split `"\r"` + `"\n"`, and overwriting progress lines in place on `\r`.
2. **Bounded Ring Buffer & Prefix-Preserving Redaction (`MAX_LINES = 3500`, `MAX_LINE_CHARS = 2048`):**
   - `redactSensitiveText()` (default `REDACT: ON`) masks `sk-`, `gh[pousr]_`, `hf_`, `AIza`, `AKIA`, `Authorization: Bearer` / `Bearer`, key/query secrets (`api_key`, `token`, `secret`, `password`), and Windows/POSIX home paths (`C:\Users\<user>\`, `/Users/<user>/`, `/home/<user>/`).
3. **O(1) Cached Filter (`/` Hotkey) & Error-Jump (`E` Hotkey):**
   - `getFilteredLines()` caches results per `(state.linesVersion, query)`. `jumpToNextError()` spotlights errors directly in the Hero slot (`marginFromBottom = 0` in non-`FLAT` views).

## [S5] Zero-Echo Audio-Reactive Engine & Async Capture Generation Guard
1. **Lazy Creation & Zero-Echo Graph:**
   - `AudioContext` created only on user gesture. `analyser` (`fftSize=512`) is a terminal sink **never** connected to `audioCtx.destination`. `externalSourceNode` (`SYS`/`MIC`) connects **only** to `analyser`; `synthMasterGain` and `fileMediaNode` connect to both `analyser` and `audioCtx.destination`.
2. **Validated File Drop, Track Cleanup & `audioCaptureGen` Concurrency Guard:**
   - `audioCaptureGen` increments on every `disconnectExternalAudio()`, `toggleExternalAudio()`, and `drop` so overlapping permission dialogs or stale `fileAudioEl.play()` callbacks immediately stop orphaned tracks and never clobber a newer audio source.

## [S6] Multi-Scale Hero Typography (`VIEW`), 4 Shaders (`1-4`), & Comfy Black & Yellow Palette (`TINT` / `Y`)
1. **`VIEW` Modes (`CASCADE | DUAL | DRUM | FLAT`, Hotkey `V`, Default `CASCADE`):**
   - Giant newest foreground text (`~46px` in `CASCADE`/`DRUM`, `~56px–68px` in `DUAL`) tapering smoothly into older lines with single-row Hero auto-fit, orphan-tail prevention (`wrapSpansMonospace`), smoothed block heights, and WebGL2 glass/drum curvature.
2. **5 Color Palettes (`TINT_NAMES = ["AUTO", "COMFY B&Y", "AMBER P3", "GREEN P31", "ICE CYAN"]`, Hotkeys `Y` & `T`):**
   - **`COMFY B&Y` (`tintMode == 1`, Hotkey `Y`, `?tint=comfy`):** Full-stack **Pitch-Black & Electric Comfy Yellow** (`#050504` obsidian canvas + `#ffd60a` electric yellow / `#ffe869` warm yellow / `#fff9c4` white-gold hot metrics / `#ff453a` crimson errors) across WebGL2 `FEEDBACK_FRAG` + `COMPOSITE_FRAG` (`u_tintMode == 1`), 2D canvas typography, and `#controlDock` CSS variables.

## [S7] Verification Matrix
1. `pytest` (`tests/test_logblaster.py`) + Bug Bible regression suite:
   - UTF-8 / no-BOM / non-zero bytes / Python `ast.parse`.
   - `telemetry.py` NVML schema, explicit invalid `LOGBLASTER_NVML_INDEX` non-fallback, `CUDA_VISIBLE_DEVICES` index/UUID resolution, and unavailable fallback.
   - `LogBlaster_StageCue` passthrough, newline sanitization, and `IS_CHANGED` (`always_fire=False` deterministic vs `always_fire=True` `NaN`).
   - Route idempotency, `send_sync` observer forwarding to `logblaster-*` sockets without echoing back to sender `sid`, non-blocking `asyncio.to_thread` routes, GLSL `#version 300 es` shaders, `COMFY B&Y` uniforms, and zero-echo audio invariants.
   - `node --check` on `js/log_blaster.js` and `web_app/index.html`, plus executable Node unit tests for `redactSensitiveText()`, `wrapSpansMonospace()`, and `ingestLogChunk()` (split chunks `"hel"` + `"lo\n"`, `print` `"msg"` + `"\n"`, split CRLF `"a\r"` + `"\nb\n"`, `\r` progress bar overwrites, and split-chunk `Bearer` secret redaction).
2. Live ComfyUI `:8000` + Chrome DevTools WebGL2 visual verification across `COMFY B&Y`, `CASCADE`, `DUAL`, `DRUM`, and shaders `1-4` with 0 console errors.
