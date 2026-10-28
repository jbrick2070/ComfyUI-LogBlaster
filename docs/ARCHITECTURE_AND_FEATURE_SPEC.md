# ComfyUI-LogBlaster -- Hardened Architecture & Coding Specification

## [S1] Vision, Scope & Bounded Render Budget
`ComfyUI-LogBlaster` is a standalone ComfyUI custom-node pack (`ComfyUI-LogBlaster`) serving a real-time WebGL2 GPU Log-Text Visualizer at `GET /logblaster`.

### Bounded Overhead Contract
1. **Best-effort Low-Power GPU Hint + Honest Disclosure:** WebGL2 initializes with `powerPreference: "low-power"` (`?gpu=high` opt-in). Because Chromium ANGLE on Windows shares whatever GPU adapter the browser's GPU process booted on, the HUD inspects `WEBGL_debug_renderer_info` and displays the **actual active WebGL adapter** (`FX: <adapter> (<fps>fps <fpsMode>)`).
2. **Eco Frame-Pacing Governor (`fpsMode`):**
   - `ECO` (default): caps rendering to **30 FPS** during active ComfyUI workflow execution (`runState === "BLASTING"`) or active audio/scroll animations, and **15 FPS** when `IDLE`/`COMPLETE` with all pulse uniforms decayed (`< 0.02`) and audio inactive.
   - `60`: caps rendering to **60 FPS** (prevents wasteful 120Hz/240Hz monitor overdrive).
   - `MAX`: uncapped `requestAnimationFrame` for high-refresh showcase.
3. **Dirty-Gated 2D Canvas Upload & `FX=0` Fast Path:**
   - Track `state.textDirty` (set on log ingest, resize, control change, telemetry update, or while spectrum/scroll/birthFlash animations are active, plus a 480ms clock/cursor tick). When `!textDirty`, skip `renderTextTexture()` and `gl.texSubImage2D(u_textTex)` entirely.
   - When `fxIntensity < 0.02` (`FX = 0%`), skip Pass 1 feedback FBO draw and blit `u_textTex` directly in Pass 2 with flat background.
   - When `document.hidden` is true, pause both `requestAnimationFrame` draws and `/logblaster/telemetry` HTTP polling.

## [S2] Explicit Auto-Popup Lifecycle & Delivery Modes
Three supported launch modes with deterministic session lifecycle:
1. **Mode A (UI Synchronous Queue):** `js/log_blaster.js` wraps `api.queuePrompt`. When `AUTO:ON` and `!isHudWindowAlive()`, calls `window.open("/logblaster", "ComfyUILogBlasterHUD", features)` synchronously inside the user's click/shortcut gesture.
2. **Mode B (Open-UI External API Queue):** When `api.addEventListener("execution_start")` fires without a user click (external CLI/API prompt while ComfyUI tab is open), pings `BroadcastChannel("comfy-logblaster-bus")` (180ms window); if no HUD responds, attempts `window.open` and falls back to `openEmbeddedDock()` (`<iframe>` drawer with `Pop Out Window` and `X` close).
3. **Mode C (Headless Server, No UI Tab Open):** Requires opt-in companion `scripts/watch_and_pop.ps1 -HostName 127.0.0.1 -Port 8000` (or manual `launch_log_blaster.bat`), clearly documented as an opt-in local watcher rather than automatic server-spawned UI.
4. **Unsubscribe & Reconnect Hygiene:** `web_app/index.html` sends `fetch("/internal/logs/subscribe", { method: "PATCH", keepalive: true })` `{ clientId, enabled: false }` on `pagehide` (ComfyUI `TerminalService` also auto-prunes disconnected sockets).

## [S3] Backend Routes, Security & Multi-Platform Telemetry
- **Package-Owned Routes (`__init__.py`):** Read-only `GET /logblaster` and `GET /logblaster/telemetry` (`Cache-Control: no-store`, no `Access-Control-Allow-Origin: *`, singleton guard `_comfyui_logblaster_singleton`).
- **Upstream ComfyUI APIs Consumed by Frontend:**
  - `GET /internal/logs/raw` & `PATCH /internal/logs/subscribe` (ComfyUI `1.2+` / core `TerminalService`, verified on ComfyUI `1.52+`).
  - **Degradation Contract:** `backfillAndSubscribeLogs()` checks HTTP status codes. If `/internal/logs/subscribe` fails or returns non-2xx, logs a `[WARNING]` banner once and sets `state.logStreamAvailable = false`, emitting `[ComfyUI] Executing <nodeTitle>` lines from `/ws` `executing` events so the visualizer never goes blank.
- **Universal Hardware Telemetry Fallback (`telemetry.py` + `GET /system_stats`):**
  - Primary (NVIDIA Windows/Linux): `telemetry.py` `NvmlReader.snapshot()` via `ctypes` (`nvml.dll` / `libnvidia-ml.so.1`, `0.4s` cache, zero `torch.cuda` touch, zero subprocesses).
  - Fallback (macOS MPS, AMD ROCm, Intel XPU, CPU): When `/logblaster/telemetry` returns `available: false`, `pollTelemetry()` queries ComfyUI's standard `GET /system_stats` (`devices[0]`: `name`, `type`, `vram_total`, `vram_free`), populating `vram_used_mib` and `vram_total_mib` with `metrics_mode: "vram_only"`.

## [S4] Streamer/Privacy Redaction, Search/Filter & Error-Jump (`web_app/index.html`)
1. **Privacy & Secret Redaction (`state.redactEnabled`, default `true`, toggle `REDACT: ON/OFF`):**
   - `redactSensitiveText(str)` runs on display & clipboard copy when `redactEnabled` is `true` (raw text is never persisted to `localStorage`):
     - Masks secret tokens (`sk-...`, `gh[pousr]_...`, `hf_...`, `AIza...`) and `Bearer` / `api_key` / `access_token` / `secret` key-value pairs.
     - Collapses Windows/macOS/Linux user home directories (`C:\Users\<user>\`, `/Users/<user>/`, `/home/<user>/`) to `~\` / `~/`.
2. **In-Shader Live Filter (`/` Hotkey) & Error-Jump (`E` Hotkey):**
   - Compact filter input in `#controlDock` (`placeholder="Filter (/)"`). Filters `state.lines` case-insensitively inside `renderTextTexture()` and displays `[FILTER "<q>" M/N]` in the GPU header.
   - `ERR: <count>` button (`E` hotkey) pauses live follow and spotlights the next error directly in the giant foreground Hero slot (`fromEnd = activeList.length - 1 - targetIdx` in `CASCADE`/`DUAL`/`DRUM`, or `- 4` in `FLAT`).
3. **URL Query Boot Overrides:**
   - Supports `?preset=1..4&view=cascade|dual|drum|flat&tint=0..3&fx=0..160&fps=eco|60|max&redact=0|1&gpu=high`.

## [S5] Audio-Reactive Web Audio Graph (Zero-Echo Topology) & 4 GPU Shaders
- **Lazy Initialization:** `AudioContext` is NEVER created until the user clicks `SYNTH`, clicks `AUDIO IN`, or drops an audio file.
- **Zero-Echo Audio Graph Topology:**
  - `analyser` (`fftSize=512`) is a pure silent tap sink NOT connected to `audioCtx.destination`.
  - `synthMasterGain` and `fileMediaNode` connect to BOTH `analyser` and `audioCtx.destination`.
  - `externalSourceNode` (`SYS` / `MIC`) connects ONLY to `analyser` (zero speaker playback, zero feedback/echo).
- **Dropped-File ObjectURL Hygiene:** Revokes previous `state.currentFileObjectUrl` via `URL.revokeObjectURL(...)` on new drop or disconnect.
- **4 Switchable WebGL2 `#version 300 es` Shaders (`1` CRT Phosphor, `2` Cyberpunk Glitch/Matrix, `3` Vintage Radio Oscilloscope, `4` Arcade Neon/Synthwave):**
  - Driven by `u_textTex` (RGBA8 transparent-alpha glyph texture), `u_feedbackTex` (ping-pong RGBA8 FBO), and `u_audioTex` (`256x2` `R8` texture: row 0 waveform, row 1 FFT), with foreground glyph alpha suppressing stale feedback trails (`trail * (0.54 * (1.0 - tC.a * 0.72))`) so giant scrolling letters remain razor-sharp.

## [S6] Multi-Scale Hero Typography & 3D Depth Modes (`VIEW: CASCADE | DUAL | DRUM | FLAT`, Hotkey `V`)
To make the newest real-time streaming log text visually commanding at cinema scale while older history recedes into depth, `web_app/index.html` implements 4 switchable depth/typography modes (`default = CASCADE`):
1. **`CASCADE` (Default — Giant Live Foreground -> Shrinking Perspective Trail):**
   - Renders bottom-up from `logBottom = vh - 58` (cleanly above `#controlDock`).
   - Continuous vertical distance `distNorm = clamp(distPx / availH, 0, 1)` drives an exponential vector font-size taper from **`heroMaxFs` (~`46px` bold, `3.25x` base font)** on the newest real-time streaming line smoothly down through `35px -> 29px -> 25px -> 22px -> 19px -> 16px -> 13px -> 9px` as lines scroll upward, paired with trapezoidal perspective margin convergence, depth-graded luminance (`1.0 -> 0.28`), and subtle WebGL2 glass curvature (`u_viewMode == 0`).
   - Span-preserving smart monospace word-wrapper (`wrapSpansMonospace(spans, maxCols, maxWrap)`) wraps giant foreground lines across up to 3 sub-rows (`maxWrap = 3` on hero row, `2` on `lineFs >= 23`) with smoothed block-height transitions (`line._smoothH`) so long ComfyUI log lines never clip off the right edge or pop mid-scroll.
2. **`DUAL` (Dual-Layer Colossal Hero Marquee + Recessed Dense Stream):**
   - Layer 1 renders the full dense scrolling terminal across the viewport at `52%` alpha.
   - Layer 2 applies a `destination-out` soft vertical alpha mask (dimming Layer-1 glyphs behind the foreground stage without blocking the WebGL2 procedural background!) and renders the newest streaming message in **colossal `~56px–68px` bold typography** (`3.85x` base font, wrapped up to 2 rows) inside glowing HUD corner brackets.
3. **`DRUM` (3D Curved Perspective Cylinder + Tapered Stream):**
   - Combines `CASCADE` vector font tapering with pronounced 3D cylindrical drum & perspective foreshortening curvature in `COMPOSITE_FRAG` (`u_viewMode == 2`, bounded below `u_headerSplit` so the top telemetry HUD stays undistorted).
4. **`FLAT` (Classic Uniform Terminal):**
   - Uniform `1.0x` monospace lines for dense stack-trace inspection.

## [S7] Verification & Acceptance Matrix
1. Automated `pytest` suite (`tests/test_logblaster.py`):
   - UTF-8 / no-BOM across all text assets.
   - `telemetry.py` NVML schema, path-free output, and unavailable fallback.
   - `LogBlaster_StageCue` wildcard passthrough & newline sanitization.
   - HTTP routes (`GET /logblaster`, `GET /logblaster/telemetry`), no wildcard CORS, GLSL `#version 300 es` shader presence, `VIEW_MODES` (`CASCADE`, `DUAL`, `DRUM`, `FLAT`) + `wrapSpansMonospace` + `u_viewMode`, redaction helper presence, zero-echo audio wiring check, and JS syntax check (`node --check js/log_blaster.js`).
2. Live Hardware + Browser Integration Matrix:
   - Headless ComfyUI boot on `:8000` with duplicate-folder junction active -> 0 duplicate-route errors.
   - Live `/prompt` execution -> `logs` + `executing` + `execution_success` stream over `/ws` into WebGL2 canvas with 0 console errors.
   - Visual verification across all 4 shader presets + `CASCADE` / `DUAL` / `DRUM` giant-to-small streaming typography + `SYNTH` audio reactivity.
