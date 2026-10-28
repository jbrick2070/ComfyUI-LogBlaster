# ComfyUI-LogBlaster -- Hardened Architecture, Coding & Wiring Specification (Post-R2)

## [S1] Vision, Scope & Bounded Render Budget
`ComfyUI-LogBlaster` is a standalone ComfyUI custom-node pack serving a real-time Audio-Reactive WebGL2 GPU Log-Text Visualizer at `GET /logblaster`.

### Bounded Overhead & Memory Contract
1. **Best-Effort Low-Power GPU Hint + Honest Adapter Disclosure:** WebGL2 initializes with `powerPreference: "low-power"` (`?gpu=high` opt-in). Inspects `WEBGL_debug_renderer_info` (`UNMASKED_RENDERER_WEBGL`) and displays `FX: <adapter> (<fps>fps <fpsMode>)`. When the browser masks renderer info, displays `"WebGL2 (Adapter Masked)"`.
2. **Hard Render-Target Memory Ceiling, FBO Completeness & Context-Loss Recovery:**
   - Clamp render dimensions to `dpr <= 1.5`, `MAX_RENDER_WIDTH = 2240`, and `MAX_RENDER_PIXELS = 2_600_000` (scaling `(w, h)` proportionally when exceeded and storing `textCanvas._dpr = w / cssW`) so `textTex` + ping-pong RGBA8 FBOs (`texA`, `texB`) never exceed ~30 MiB combined even on 4K/5K monitors.
   - Check `gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE` in `makeRenderTarget()`; if incomplete, set `state.fboReady = false` and fall back automatically to the single-pass `u_textTex` composite path without feedback FBOs.
   - Handle `webglcontextlost` (`ev.preventDefault()`, cancel active rAF loop, mark `state.contextLost = true`) and `webglcontextrestored` (rebuild programs, VAO, `textTex`, `audioTex`, and ping-pong FBOs, set `state.textDirty = true`, and resume rAF).
3. **Eco Frame-Pacing Governor & True Hidden Pause:**
   - `ECO` (default): **30 FPS** while `BLASTING` or audio/scroll/pulse animations are active; **15 FPS** when quiet/idle. `60`: 60 FPS cap. `MAX`: uncapped monitor refresh.
   - When `document.hidden` is true, **stop scheduling `requestAnimationFrame` altogether** (`rafId = 0`, cancelling any pending frame on `visibilitychange`) and skip `/logblaster/telemetry` polling; resume cleanly on `visibilitychange` when `!document.hidden`.
   - When `!state.textDirty` (and `< 480ms` since last clock/cursor draw while not `BLASTING`), skip `renderTextTexture()` and `gl.texSubImage2D(u_textTex)`. When `fxIntensity < 0.02` or `!state.fboReady`, skip Pass 1 FBO draw.
   - All `localStorage` reads/writes go through `storageGet()` / `storageSet()` wrapped in `try / catch`.

## [S2] Explicit Auto-Popup Lifecycle, Bus Wiring & Cleanup
1. **Mode A (UI Synchronous Queue):** `js/log_blaster.js` wraps `api.queuePrompt` once (`api._logBlasterWrapped`). When `AUTO:ON` and `!isHudWindowAlive()`, opens `window.open("/logblaster", "ComfyUILogBlasterHUD", features)` synchronously inside the user gesture.
2. **Mode B (Open-UI External API Queue):** On `api.addEventListener("execution_start")`, pings `BroadcastChannel("comfy-logblaster-bus")` (180ms grace); if no `pong` arrives, attempts `window.open` and falls back to `openEmbeddedDock()` (`<iframe>` drawer with `Pop Out Window` and `X` close).
3. **Mode C (Headless Server, No UI Tab Open):** Opt-in local companion `scripts/watch_and_pop.ps1 -HostName 127.0.0.1 -Port 8000` polling `GET /queue` and invoking `launch_log_blaster.bat <Port> <HostName>`.
4. **Unsubscribe, Timer & Backoff Hygiene:**
   - Track `state.logSubscribed = true` upon HTTP `2xx` from `PATCH /internal/logs/subscribe`.
   - On `pagehide`, clear reconnect/bus/telemetry/rate/demo timers, call `disconnectExternalAudio()`, and whenever `state.logSubscribed` is `true` (regardless of `state.wsConnected`), set `state.logSubscribed = false` and send `fetch("/internal/logs/subscribe", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ clientId: state.clientId, enabled: false }), keepalive: true })`. (Never use `navigator.sendBeacon`, which is POST-only.)
   - Reconnect `/ws` with capped exponential backoff (`1500ms -> 3000ms -> 6000ms -> 12000ms`, reset to `1500ms` on socket `open`).

## [S3] Backend Routes, Startup Diagnostics & Multi-Platform Telemetry
1. **Package-Owned Routes (`__init__.py`):**
   - Read-only `GET /logblaster` and `GET /logblaster/telemetry` (`Cache-Control: no-store`, no wildcard CORS).
   - Dual idempotency guards: module-level `_comfyui_logblaster_singleton` in `sys.modules` (skips duplicate custom-node junction loads) AND per-server `_logblaster_routes_registered` attribute inside `register_routes(prompt_server)`.
   - Startup import catches `(ImportError, ModuleNotFoundError)` silently when imported outside ComfyUI (e.g. `pytest`), and logs `[LogBlaster] Failed to register HTTP routes: <exc>` on any unexpected runtime error.
2. **Upstream ComfyUI Log APIs (Verified Live on ComfyUI `0.43.0` / Frontend `1.59.0`):**
   - `GET /internal/logs/raw` returns `{ "entries": [{ "t": "<ISO-8601>", "m": "<chunk>" }], ... }`; guarded against non-2xx, malformed JSON, or non-array `entries`.
   - `PATCH /internal/logs/subscribe` `{ clientId, enabled: true }` triggers `/ws?clientId=...` messages `{ "type": "logs", "data": { "entries": [{ "t", "m" }] } }`. On non-2xx or network error, sets `state.logStreamAvailable = false` and emits a one-time `[WARNING]` line, synthesizing workflow-progress log lines from `/ws` `executing` / `progress` / `execution_error` / `execution_success` events.
3. **Hardware Telemetry (`telemetry.py` + `GET /system_stats` Fallback):**
   - `telemetry.py` `NvmlReader.snapshot()` returns `{ ok, available, metrics_mode: "nvml" | "unavailable", gpu_name, gpu_util, mem_util, vram_used_mib, vram_total_mib, temp_c, power_w }` with `0.4s` TTL cache and explicit `ctypes` `c_int` return types. Resolves physical GPU index from `LOGBLASTER_NVML_INDEX` or leading integer in `CUDA_VISIBLE_DEVICES` (falling back to `0`).
   - When `available` is `false`, `pollTelemetry()` queries ComfyUI `GET /system_stats`, reading `devices` (`Array.isArray(sysData?.devices) ? sysData.devices : sysData?.system?.devices`), preferring the first non-CPU device (or `devices[0]`), validating finite non-negative byte counts (`vram_total`, `vram_free`), stripping `cuda:0 ` / ` : cudaMallocAsync` prefixes/suffixes from `name`, and setting `metrics_mode: "vram_only"`.

## [S4] Bounded Ring Buffer, Streamer Privacy Redaction, Cached Filter & Error-Jump
1. **Bounded Line & Buffer Memory:**
   - `MAX_LINES = 3500`, `MAX_LINE_CHARS = 2048` (lines longer than `2048` chars are truncated with ` … [truncated]` prior to tokenization, bounding worst-case retained text below ~14 MB).
   - Each line record stores `{ id, ts, timeStr, origExpanded, viewRaw, viewLower, spans, baseColor, kind, energy, _smoothH }`. `state.errorCount` is updated authoritatively in `appendOrUpdateLine()` across append, `\r` `replaceLast`, and `MAX_LINES` eviction.
2. **Safe Prefix-Preserving Redaction (`redactSensitiveText`, default `REDACT: ON`):**
   - Masks known token families preserving at most a 3–4 char prefix:
     - `sk-[A-Za-z0-9_-]{3}***REDACTED***`, `gh[pousr]_[A-Za-z0-9]{3}***REDACTED***`, `hf_[A-Za-z0-9]{3}***REDACTED***`, `AIza[0-9A-Za-z_-]{3}***REDACTED***`, `AKIA[0-9A-Z]{16}` -> `AKIA***REDACTED***`.
     - Space-delimited `Authorization: Bearer <token>` and standalone `Bearer <token>` (`8+` non-whitespace token chars).
     - Key-value & URL query secrets (`api_key`, `apikey`, `access_token`, `auth_token`, `token`, `secret`, `password` followed by `:` or `=` and `6+` value chars).
     - Windows & POSIX user home directories (`C:\Users\<user>\`, `C:/Users/<user>/`, `/Users/<user>/`, `/home/<user>/`) -> `~\` / `~/`.
3. **O(1) Cached Live Filter (`/` Hotkey) & Derived Error-Jump (`E` Hotkey):**
   - `state.linesVersion` increments on every line mutation or redaction toggle. `getFilteredLines()` caches the filtered array for `(state.linesVersion, q)` using precomputed `line.viewLower`, performing zero per-frame allocations when idle.
   - `jumpToNextError()` (`E` hotkey / `ERR: N` button) derives matching `kind === "error"` indices from `getFilteredLines()` on demand and spotlights the target error directly in the Hero slot (`marginFromBottom = 0` in `CASCADE`/`DUAL`/`DRUM`, `4` in `FLAT`).

## [S5] Zero-Echo Audio-Reactive Engine & Media Lifecycle
1. **Lazy Creation & Zero-Echo Graph:**
   - `AudioContext` is created only on user gesture (`SYNTH`, `AUDIO`, or valid audio file `drop`).
   - `analyser` (`fftSize=512`) is a terminal sink **never** connected to `audioCtx.destination`.
   - `synthMasterGain` and `fileMediaNode` connect to both `analyser` and `audioCtx.destination`; `externalSourceNode` (`SYS`/`MIC`) connects **only** to `analyser`.
2. **Validated File Drop & Complete Cleanup:**
   - `drop` handler checks `file.type.startsWith("audio/")` or `.(mp3|wav|ogg|flac|m4a|aac|opus|webm)$` before mutating audio state; non-audio drops display a toast and leave existing audio untouched.
   - `fileAudioEl` attaches `"ended"` and `"error"` handlers plus `play().catch(...)`, all invoking `disconnectExternalAudio()` (which pauses media, revokes `state.currentFileObjectUrl`, resets `state.audioInMode = "OFF"`, and updates `state.audioActive = state.synthEnabled`).
   - `getDisplayMedia` stops all tracks immediately if `getAudioTracks().length === 0`.

## [S6] Multi-Scale Hero Typography (`VIEW`), 4 Shaders (`1-4`), & Comfy Black & Yellow Palette (`TINT` / `Y`)
1. **`VIEW` Modes (`CASCADE | DUAL | DRUM | FLAT`, Hotkey `V`):**
   - **`CASCADE` (Default):** Bottom-up layout (`logBottom = vh - 58`, above `#controlDock`) with exponential font taper from `heroMaxFs` (`~46px` bold) down to `~9px`, trapezoidal perspective convergence, depth-graded alpha, single-row Hero auto-fit (`<= maxCols * 1.18`), orphan-tail prevention in `wrapSpansMonospace()`, smoothed block heights (`line._smoothH`), and subtle WebGL2 glass arch (`u_viewMode == 0`).
   - **`DUAL`:** Layer-1 dense scrolling stream (`52%` alpha) + `destination-out` vertical alpha fade + Layer-2 colossal `~56px–68px` foreground Hero Marquee inside HUD corner brackets.
   - **`DRUM`:** `CASCADE` font taper + pronounced 3D cylindrical drum & perspective foreshortening curvature in `COMPOSITE_FRAG` (`u_viewMode == 2` below `u_headerSplit`).
   - **`FLAT`:** Uniform `1.0x` monospace terminal.
2. **5 Color Palettes (`TINT_NAMES = ["AUTO", "COMFY B&Y", "AMBER P3", "GREEN P31", "ICE CYAN"]`, Hotkeys `T` & `Y`):**
   - **`COMFY B&Y` (`tintMode == 1`, Hotkey `Y` one-key toggle, `?tint=comfy`):** Full-stack **Pitch-Black & Electric Comfy Yellow** studio palette across CSS (`#controlDock`/`#toastMsg`), 2D HUD (`#ffd60a` electric yellow hero/badges, `#ffe869` warm yellow body, `#fff9c4` white-gold hot metrics, `#9e8624` muted gold timestamps, `#ff453a` high-alert crimson errors, `#ff9f0a` tangerine warnings), and WebGL2 shaders (`u_tintMode == 1` in both `FEEDBACK_FRAG` and `COMPOSITE_FRAG`: true obsidian `#050504` backgrounds, electric yellow Matrix digital rain, golden Tektronix oscilloscope trace, yellow 3D synthwave horizon grid, and yellow shockwave rings).
   - **`AUTO` (`0`), `AMBER P3` (`2`), `GREEN P31` (`3`), `ICE CYAN` (`4`):** Native multi-color per-shader palettes or classic monochrome CRT phosphor overrides, persisted by name (`logblaster.tintName`) so saved preferences never drift.

## [S7] Verification Matrix
1. `pytest` (`tests/test_logblaster.py`):
   - UTF-8 / no-BOM / non-zero bytes / Python `ast.parse`.
   - `telemetry.py` NVML schema (`metrics_mode`), path-free output, device-index env resolution (`LOGBLASTER_NVML_INDEX` & `CUDA_VISIBLE_DEVICES`), and unavailable fallback.
   - `LogBlaster_StageCue` passthrough & newline sanitization.
   - Route idempotency (`register_routes` twice -> `True` then `False`), `Cache-Control: no-store`, no wildcard CORS, GLSL `#version 300 es` shaders, `COMFY B&Y` / `u_tintMode` uniforms, zero-echo audio invariants.
   - `node --check` on `js/log_blaster.js` and inline `<script>` in `web_app/index.html`, **plus executable Node unit tests** exercising `redactSensitiveText()` (positive & negative secret/header/path vectors) and `wrapSpansMonospace()` (span color preservation & orphan-tail prevention).
2. Live ComfyUI `:8000` + Chrome DevTools WebGL2 verification across shaders, view modes, and `COMFY B&Y` with 0 console errors.
