# ComfyUI-LogBlaster -- Architecture and Feature Specification

## [S1] Vision, Scope & Bounded Render Budget
`ComfyUI-LogBlaster` is a standalone ComfyUI custom-node pack serving a real-time Audio-Reactive WebGL2 GPU Log-Text Visualizer at `GET /logblaster`.

### Bounded Overhead & Memory Contract
1. **Best-Effort Low-Power GPU Hint + Honest Adapter Disclosure:** WebGL2 initializes with `powerPreference: "low-power"` (`?gpu=high` opt-in). Inspects `WEBGL_debug_renderer_info` (`UNMASKED_RENDERER_WEBGL`) and displays `FX: <adapter> (<fps>fps <fpsMode>)` (`"WebGL2 (Adapter Masked)"` when masked).
2. **Hard Render-Target Memory Ceiling, FBO Completeness & Context-Loss Recovery:**
   - Clamp render dimensions to `dpr <= 1.5`, `MAX_RENDER_WIDTH = 2240`, and `MAX_RENDER_PIXELS = 2_600_000` (storing `textCanvas._dpr = w / cssW`) so `textTex` + ping-pong RGBA8 FBOs (`texA`, `texB`) never exceed ~30 MiB combined even on 4K/5K displays.
   - Check `gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE`; fall back automatically to single-pass `u_textTex` rendering if incomplete.
   - Handle `webglcontextlost` (`ev.preventDefault()`, cancel active rAF, mark `state.contextLost = true`) and `webglcontextrestored` (rebuild programs, VAO, textures, and FBOs, resume rAF).
3. **Eco Frame-Pacing Governor & True Hidden Pause:**
   - `ECO` (default): **30 FPS** draw rate while `BLASTING` or audio/scroll/pulse animations are active; **15 FPS** draw rate when quiet/idle. `60`: 60 FPS cap. `MAX`: uncapped refresh.
   - When `document.hidden` is true, cancel and stop scheduling `requestAnimationFrame` (`rafId = 0`) and skip telemetry polling; resume on `visibilitychange`.
   - Dirty-gate 2D canvas rasterization + `gl.texSubImage2D(u_textTex)` (`480ms` idle cursor refresh) and skip Pass 1 FBO draw when `fxIntensity < 0.02` or `!state.fboReady`.
   - All `localStorage` access in both `/logblaster` and `js/log_blaster.js` is wrapped in `try / catch` with in-memory fallback.

## [S2] Explicit Auto-Popup Lifecycle, Directed Event Forwarding & Teardown Hygiene
1. **Mode A (UI Synchronous Queue -- `js/log_blaster.js`):**
   - Wraps `api.queuePrompt` once (`api._logBlasterWrapped`), isolating all pre-hook logic inside `try { ... } catch (_) {}` so no HUD/DOM/storage exception can ever prevent `origQueuePrompt(...args)` from submitting a prompt.
   - Calls `openLogBlasterWindow(false)` (does not steal focus when the HUD is already open). Uses a base-path-scoped window name, retaining `ComfyUILogBlasterHUD` at the root. Blank windows and windows navigated away from the same-origin HUD are restored to the HUD URL; an existing HUD retains its 3,500-line history.
2. **Mode B (Open-UI External API Queue):**
   - Triggers auto-open check on `execution_start` and broadcast `status` (`queue_remaining` rising `0 -> >0`, including initial load while a job is already active or headless API runs queued without `client_id`, where ComfyUI core broadcasts `status`, `progress`, and `execution_interrupted`).
   - For requests omitting `client_id`, ComfyUI may omit directed start/node/success events entirely. The HUD uses broadcast activity and observed elapsed time; the observer tap cannot forward events that core never emits.
   - Pings a base-path-scoped `BroadcastChannel` (180ms grace); if unopened and popup-blocked, opens `openEmbeddedDock()` (`<iframe>` with `allow="clipboard-write; fullscreen; microphone; display-capture; autoplay"`, `allowFullscreen = true`, and `iframe.src = "about:blank"` teardown on close). Capture permissions remain subject to the browser's user-consent flow; the iframe allow-list enables the shared SYS/MIC controls.
   - Uses a base-path-scoped `BroadcastChannel` strictly for `ping`/`pong`/`hello`/`node_map`/`wake`. Node maps snapshot the submitted workflow/output before queue submission and carry the returned `prompt_id`; both sides retain at most 32 prompt maps. The HUD selects only the active prompt's map, so other tabs cannot relabel it. Unknown/API prompts use generic node IDs. For subgraph execution IDs (`"parent:child"`), `resolveNodeTitle()` falls back to the root container ID `parent` (`${containerTitle} › #parent:child`).
   - Extension launcher, HUD HTTP and WebSocket URLs preserve reverse proxy base paths under the canonical same-origin `<base>/logblaster` URL (the watcher uses its configured host and port). A named popup navigated away from the HUD is restored; an actual HUD keeps its history. The headless watcher retries launches at most three times per active queue period, ten seconds apart, until an observer connects.
3. **Mode C (Headless Server, No UI Tab Open):**
   - Opt-in `scripts/watch_and_pop.ps1 -HostName 127.0.0.1 -Port 8000` polling lightweight `GET /prompt` (`exec_info.queue_remaining`), querying server-authoritative `observers` count from `GET /logblaster/telemetry`, and launching `launch_log_blaster.bat` (Edge `--app`, Chrome `--app`, or default browser) only when `observers == 0`.
4. **Directed Execution Event Observer Tap (`__init__.py`):**
   - `register_routes(prompt_server)` installs an idempotent wrapper on `prompt_server.send_sync` (`_logblaster_send_sync_wrapped`). `orig_send_sync(event, data, sid)` runs outside `try`; the observer-forwarding block (`sid is not None and event in _OBSERVED_EXECUTION_EVENTS` -> forward to `logblaster-*` keys in `prompt_server.sockets`) is completely wrapped in `try ... except Exception: pass` so it can never raise into `PromptExecutor`.
   - `/logblaster` keeps its own unique `logblaster-<uuid>` `clientId`, deduplicates `/ws` events via a bounded 1.5s multi-signature `recentEventSigs` map, and emits synthetic status banners via `injectLocalLine()` without touching `stdout` partial-line state.
5. **Generation-Owned Subscribe-Before-Backfill, Contiguous Reconnect & Teardown:**
   - Each socket connection owns a `lifecycleGen` token and an `AbortController` (`activeSyncAbort`, aborted on socket close or `pagehide`). Stale generations never mutate `backfillInFlight` or `liveLogBuffer` (bounded to `MAX_LIVE_BUFFER_CHARS = 524288` with overflow notice).
   - Socket close marks execution identity uncertain. The next observed active run resets its timer, step counters and stale node map; elapsed time after a missed start measures the observed portion of that run.
   - Terminal events share run finalization and retain at most 32 completed prompt IDs. A trailing `executing(node=null)` for a completed run cannot overwrite its elapsed time or turn an error/interruption into success, including when queue status has already announced another run.
   - `syncLogsAfterOpen(gen)` subscribes (`PATCH /internal/logs/subscribe`) **before** `GET /internal/logs/raw`, preserves `partialLineOpen`/`partialCrPending`/`partialRawText` across reconnects whenever `raw` overlaps `cutoffIso` (resetting with a resync notice only on a real history gap), and deduplicates boundary entries at both `cutoffIso` and `backfillMaxT` using `(t, m)` multiplicity counts (`seenAtLastIsoT`) so same-timestamp chunks (such as `print`'s `"msg"` + `"\n"`) are never dropped.

## [S3] Backend Routes, Non-Blocking I/O, Companion Node & Hardware Telemetry
1. **Package-Owned Routes (`__init__.py`):**
   - Uses `if __package__ not in (None, ""):` for package-relative imports.
   - Dual idempotency guards: `_comfyui_logblaster_singleton` in `sys.modules` + `_logblaster_routes_registered` on `prompt_server`.
   - Both `GET /logblaster` and `GET /logblaster/telemetry` execute blocking work via `await asyncio.to_thread(...)` and include `"observers"` (active `logblaster-*` socket count) in `/logblaster/telemetry`.
2. **Companion Node (`nodes.py` -- `LogBlaster_StageCue`):**
   - Pass-through stage marker (`banner`, `fx_trigger`, optional `passthrough`, optional `always_fire=False`). `IS_CHANGED` returns `float("nan")` only when `always_fire=True`, preserving downstream ComfyUI cache by default.
3. **Hardware Telemetry (`telemetry.py` + Single-Flight `GET /system_stats` Fallback):**
   - `resolve_nvml_target()` respects explicit `LOGBLASTER_NVML_INDEX` (digits or `GPU-...` UUID) without falling back to GPU 0 on invalid/out-of-range targets, supports `CUDA_VISIBLE_DEVICES` indices/UUIDs (noting CUDA vs PCI/NVML index order when `CUDA_DEVICE_ORDER` differs), and exposes `"device_index"`.
   - An unresolved CUDA UUID also reports unavailable rather than selecting an unrelated physical GPU 0. CUDA index remapping retains its documented container fallback.
   - `pollTelemetry()` is single-flight (`telemetryInFlight`), uses `/system_stats` when NVML reports unavailable, and probes NVML recovery every 30 seconds. Failed essential utilization/memory reads are unavailable, never valid zero metrics. The reader releases its NVML initialization reference and retries transient failures after 30 seconds; explicit invalid targets never fall back to another GPU.

## [S4] Isolated Partial-Line Framing, ANSI-Normalized Privacy Redaction, Cached Filter & Error-Jump
1. **Isolated Partial-Line Framing (`ingestLogChunk` vs `injectLocalLine`):**
   - `ingestLogChunk()` frames raw `stdout`/`stderr` chunks via `/[^\r\n]+|\r|\n/g` with `partialLineOpen`, `partialCrPending`, and pre-clamped `partialRawText` (`<= MAX_LINE_CHARS + 128`, discarding overflow until delimiter).
   - `injectLocalLine()` inserts local HUD status/warning/error lines independently (splicing before an open partial `stdout` tail entry so ongoing `stdout` chunks continue joining onto their own entry).
2. **ANSI-Normalized Redact-Before-Truncate (`redactSensitiveText`, default `REDACT: ON`):**
   - `rebuildLineView()` strips ANSI escapes first, runs `redactSensitiveText(rawPlain, entry.isPartial)` *before* `MAX_LINE_CHARS` (`2048`) truncation, masks `Authorization: Bearer <val>` and `api_key`/`token`/`secret`/`password` values from their first character (plus trailing incomplete `sk-`/`gh*_`/`hf_`/`AIza`/`AKIA` prefixes while `isPartial` is true), and re-tokenizes from redacted plain text whenever a secret or home path was masked.
3. **Cached Filter (`/` Hotkey, Constant-Time Cache Hits) & Error-Jump (`E` Hotkey):**
   - `getFilteredLines()` scans on `(linesVersion, query)` cache misses and returns in O(1) on cache hits. `jumpToNextError()` spotlights errors directly in the Hero stage.
   - PAUSED holds a stable line ID while new logs arrive; partial updates preserve IDs. If the anchor is evicted, the oldest remaining visible line becomes the anchor. Filter changes and wheel/error navigation establish a new anchor; Resume follows the newest line.
   - Key/value redaction includes quoted JSON and Python-style keys/values, spaces and escaped quotes, including partial values ending in a backslash. COPY uses the same redacted view as the canvas.
   - Underscore-prefixed keys (`client_secret`, `refresh_token`), Authorization Basic/Token/ApiKey headers, password-only URL userinfo and bare home directories are masked too. COPY exports the bounded visible history, including explicit truncation markers.

## [S5] Zero-Echo Audio-Reactive Engine & Async Capture Generation Guard
1. `AudioContext` is lazy on user gesture; `analyser` (`fftSize=512`) is never connected to `audioCtx.destination`; `externalSourceNode` (`SYS`/`MIC`) connects only to `analyser`; `synthMasterGain` and `fileMediaNode` connect to both `analyser` and `audioCtx.destination`.
2. `audioCaptureGen` guards overlapping `getUserMedia`, `getDisplayMedia`, and `fileAudioEl.play()` promises and stops orphaned tracks immediately.
3. Hidden pages fade SYNTH output to zero within 40ms, suppress synth ticks and clear stale drone automation. FILE audio keeps its own audible path. Revealing the page restores an enabled synth with a settled drone.

## [S6] Multi-Scale Hero Typography (`VIEW`), 4 Shaders (`1-4`), & Comfy Black & Yellow Palette (`TINT` / `Y`)
1. **`VIEW` Modes (`CASCADE | DUAL | DRUM | FLAT`, Hotkey `V`, Default `CASCADE`):** Giant newest foreground text (`~46px` in `CASCADE`/`DRUM`, `~56px-68px` in `DUAL`) tapering smoothly into older lines with single-row Hero auto-fit, orphan-tail prevention (`wrapSpansMonospace`), and WebGL2 glass/drum curvature.
2. **5 Color Palettes (`TINT_NAMES = ["AUTO", "COMFY B&Y", "AMBER P3", "GREEN P31", "ICE CYAN"]`, Hotkeys `Y` & `T`):** Full-stack **`COMFY B&Y`** (`#050504` obsidian canvas + `#ffd60a` electric yellow / `#ffe869` warm yellow / `#fff9c4` white-gold hot badges / `#ff453a` crimson errors) across WebGL2 `FEEDBACK_FRAG` + `COMPOSITE_FRAG` (`u_tintMode == 1`), 2D canvas typography, and `#controlDock` CSS.

## [S7] Verification Matrix
0. **Maintenance regression coverage:** `tests/extension_harness.cjs` and `tests/hud_harness.cjs` execute the actual frontend scripts in a simulated browser, exercising queue-result identity, real pause/wheel/copy handlers, redaction per arriving character, filtered history eviction, base paths, late node maps, missed-start timing and NVML fallback recovery. `tests/telemetry_failure_cases.py` injects NVML failures and checks cooldown, recovery, explicit target selection and balanced initialization/shutdown. All are collected by `tests/test_logblaster.py`. These simulations complement live WebGL2 checks.
1. **Automated Unit & Contract Suite (`pytest -q tests/test_logblaster.py`, requires Node >= 22 on PATH or explicitly skips via `pytest.skip`) + Cross-Project Survival Suite (`comfyui-custom-node-survival-guide\tests\bug_bible_regression.py`):**
   - UTF-8 / no-BOM / non-zero bytes / Python `ast.parse`.
   - `telemetry.py` schema, explicit invalid `LOGBLASTER_NVML_INDEX` non-fallback, and unavailable fallback.
   - `LogBlaster_StageCue` passthrough and `IS_CHANGED` (`always_fire=False` vs `True`).
   - Route idempotency, `asyncio.to_thread` non-blocking handlers, `"observers"` socket count, `_wrapped_send_sync` observer forwarding + exception isolation, GLSL `#version 300 es`, `COMFY B&Y` uniforms, and zero-echo audio invariants.
   - `node --check` on `js/log_blaster.js` and extracted `<script>` from `web_app/index.html`, plus executable Node unit tests covering:
     1. Positive/negative `redactSensitiveText()`, including ANSI-split tokens (`"sk-proj-12345\x1b[0m67890abcdef"`), 1-char/short `Authorization: Bearer super`, streaming partial token prefixes, and redact-before-truncate across `MAX_LINE_CHARS`.
     2. `wrapSpansMonospace()` span colors & orphan-tail prevention.
     3. `ingestLogChunk()` split chunks, Python `print` two-write sequence, split CRLF, `tqdm` `\r` overwrites, and split-chunk `Bearer` redaction.
     4. Interleaved `injectLocalLine()` during an open partial `stdout` line (`"hel"` + synthetic banner + `"lo\n"`, and open `\r` progress bar + interrupt warning + `\r` continuation).
     5. Adversarial 1,000,000-character partial chunk memory clamp (`partialRawText.length <= MAX_LINE_CHARS + 128`), boundary-timestamp `(t, m)` multiset deduplication (`dedupeBoundaryEntries`), and subgraph root-container `resolveNodeTitle("12:5")`.
2. **Live ComfyUI `:8000` + Chrome DevTools WebGL2 Runtime Proof:**
   - Reboot headless ComfyUI `:8000` with live `_install_observer_tap`, open `http://127.0.0.1:8000/logblaster`, confirm `"observers": 1` on `GET /logblaster/telemetry`, submit live `POST /prompt` with a foreign `client_id` and without `client_id` to verify directed observer forwarding + broadcast `status` completion, verify `GET /system_stats` byte schema, and capture visual proof across `COMFY B&Y`, `CASCADE`, `DUAL`, `DRUM`, and shaders `1-4` with 0 console errors.

## [S8] Registry Release Gate
The CI workflow runs the plugin regression suite with Python and Node for all pushes to main, pull requests against main and manual dispatch, including in fork-owned repositories. A Node preflight and per-harness success sentinels prevent silently skipping frontend coverage in CI. Automatic publishing requires an owner-repository main push with a changed project version compared with the push's previous commit. Pull requests, unchanged versions and missing/malformed comparison history skip publication; manual workflow dispatch remains an explicit release request. The Comfy registry publish action is pinned to a verified commit. Development changes do not automatically bump the version.

## [S9] Help, Responsive Controls, Motion Preferences & Honest Status
1. **Discoverable controls:** `?`/`H` or the help button opens a quick guide covering shortcuts, views and audio behavior. It appears once per browser profile through safe localStorage, closes with Escape and restores focus. SYS/MIC are explained as silent analyser taps; FILE/SYNTH play audibly. The generic TEST BLAST previews ComfyUI stages without submitting a workflow.
2. **Keyboard ownership:** Global shortcuts ignore Ctrl/Meta/Alt combinations, repeated keydown events, editable targets and focused interactive controls, preserving browser shortcuts and native button/selection behavior. Shift+A remains the microphone shortcut.
3. **Compact layout:** Controls compact below 900px with text size/COPY in MORE. The measured dock top reserves log space even when controls wrap or MORE expands. Node titles are shortened separately from the FILTER count. The 820x500 layout is part of live UI verification.
4. **Reduced motion:** The system preference is honored at startup and on change. FX is capped at 60% with matching slider limits; moving shader distortions, feedback trails, degauss/glitch bursts, scrolling impulses and birth flashes are disabled. Text, errors, themes and lower FX settings remain available.
5. **Telemetry freshness:** Only valid samples advance the success timestamp. Samples become STALE after five seconds and TELEMETRY OFFLINE after fifteen; no initial sample is presented as fresh. Socket outage duration and retry countdown replace a misleading running timer after ten seconds offline. One reconnect toast announces recovery.
6. **Behavioral verification:** The actual-script HUD harness checks help persistence, modifier/focus-safe keys, measured clearance, filter visibility, motion changes, telemetry failure/recovery, outage timing, hidden audio automation and streaming redaction. Live WebGL2 checks complement the simulated DOM/audio tests.
