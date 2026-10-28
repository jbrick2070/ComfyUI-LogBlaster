# ComfyUI-LogBlaster -- Architecture, Feature & Hardening Specification

## [S1] Goal & Core Vision
`ComfyUI-LogBlaster` is a standalone ComfyUI custom-node pack (`C:\Users\jeffr\Documents\ComfyUI\custom_nodes\ComfyUI-LogBlaster`) that makes running long ComfyUI workflows (diffusion, video, TTS, LLM pipelines) visually exciting without hurting render speed or stealing discrete GPU VRAM.

Core requirements from operator:
1. Automatically bring up a terminal/HUD window when ComfyUI runs a workflow.
2. Render and visualize **the live blasting log text itself** through real GPU shaders (not plain DOM text floating over a wallpaper).
3. Support 4 switchable shader presets:
   - Preset 1 (`1`): **Retro CRT Phosphor** (curved barrel glass, phosphor bloom/halation, temporal persistence, scanlines, RGB triads, degauss wobble).
   - Preset 2 (`2`): **Cyberpunk Glitch // Matrix** (velocity-driven horizontal scan-band slicing, dynamic RGB chromatic aberration, Matrix digital trails dripping downward off actual log letters).
   - Preset 3 (`3`): **Vintage Radio Oscilloscope** (illuminated Tektronix copper/emerald graticule, dual-stage P7 radar phosphor persistence, live oscilloscope carrier/audio beam, magnetic electron-beam deflection on log lines).
   - Preset 4 (`4`): **Arcade Neon // Synthwave** (3D receding synthwave horizon grid, plasma gradient highlights, radial shockwave rings that physically refract log characters on node transitions and beat drops).
4. **Audio-Reactive Mode** without hurting CPU/GPU performance:
   - Built-in procedural Web Audio telemetry synthesizer (`SYNTH`, hotkey `M`) routed into native `AnalyserNode` (`fftSize=512`).
   - External live audio capture (`AUDIO IN`, hotkey `A`: System/Tab loopback via `getDisplayMedia` with video track immediately stopped, `Shift+Click` for Mic via `getUserMedia`, plus drag-and-drop audio file playback).
   - Real-time `256x2` `R8` GPU audio texture (`u_audioTex`: row 0 = time-domain waveform, row 1 = frequency FFT) + `u_audioBass`, `u_audioMid`, `u_audioHigh` uniforms + beat-drop shockwave detector.
5. **Zero ComfyUI Render Penalty**:
   - WebGL2 context requests `powerPreference: "low-power"` to target the integrated GPU (e.g. Intel Graphics alongside an NVIDIA RTX 5080) with `?gpu=high` opt-in override.
   - Internal DPR capped at `1.5`; `requestAnimationFrame` pauses automatically when `document.hidden` is true.
   - Server-side GPU telemetry (`telemetry.py`) reads NVML directly via `ctypes` (`nvml.dll` / `libnvidia-ml.so.1`) with 0.4s TTL cache — zero `torch.cuda` imports and zero `nvidia-smi` subprocesses.

## [S2] Repository Layout & Grounded Files
Reviewers MUST read the real files in this repository before making claims:
- `__init__.py` -- ComfyUI entrypoint, `sys.modules` duplicate-folder singleton guard, `GET /logblaster` and `GET /logblaster/telemetry` route registration, `WEB_DIRECTORY = "./js"`.
- `telemetry.py` -- `NvmlReader` ctypes wrapper for `nvml.dll` / `libnvidia-ml.so.1` returning path-free GPU stats (`gpu_util`, `vram_used_mib`, `vram_total_mib`, `temp_c`, `power_w`).
- `nodes.py` -- Optional `LogBlaster_StageCue` wildcard passthrough node emitting `[LogBlaster:SHOCKWAVE|GLITCH_BURST|PHOSPHOR_FLASH|BANNER_ONLY]` markers to `stdout`.
- `web_app/index.html` -- Single-file offline WebGL2 GPU Log-Text Visualizer + ANSI/CR stream parser + Web Audio FFT/Synth engine.
- `js/log_blaster.js` -- ComfyUI frontend extension: topbar `LOG BLASTER` + `AUTO:ON/OFF` pill, synchronous `api.queuePrompt` popup hook, `execution_start` fallback embedded dock, and `BroadcastChannel("comfy-logblaster-bus")` node-title bridge.
- `launch_log_blaster.bat` & `scripts/watch_and_pop.ps1` -- Chromeless `msedge --app=...` launcher and optional headless `/queue` watcher.
- `tests/test_logblaster.py` -- Pytest contract suite (UTF-8/no-BOM, NVML schema & path scrub, StageCue contract, HTTP routes & shader HTML checks).

## [S3] Backend & Security Contract (`__init__.py`, `telemetry.py`, `nodes.py`)
- Duplicate-folder load safety: ComfyUI setups that junction `custom_nodes` across Desktop and standalone roots scan the pack twice; `_comfyui_logblaster_singleton` ensures routes and `WEB_DIRECTORY` register once.
- Security & Comfy Registry compliance:
  - Only read-only `GET /logblaster` and `GET /logblaster/telemetry` routes exist.
  - Zero `POST` routes, zero `subprocess` calls, zero caller-supplied filesystem reads, zero wildcard `Access-Control-Allow-Origin: *`, and zero absolute local paths in JSON/error responses.
- `NvmlReader` calls `nvmlInit_v2` lazily under a `threading.Lock`, caches snapshots for `0.4s`, and falls back cleanly on non-NVIDIA hosts (macOS MPS, AMD ROCm, CPU-only).

## [S4] Log Stream & ComfyUI Event Wiring (`web_app/index.html`)
- Connects to `ws(s)://<host>/ws?clientId=logblaster-<uuid>`.
- On open:
  1. `GET /internal/logs/raw` backfills recent server stdout/stderr history (`silentBackfill=true` so historical lines do not trigger white birth-flash or synth ticks).
  2. `PATCH /internal/logs/subscribe` (`{ clientId, enabled: true }`) subscribes to ComfyUI `TerminalService` live `logs` packets.
- Carriage-return (`\r`) handling: `ingestLogChunk` normalizes `\r\n` -> `\n` and overwrites the active line in-place on bare `\r` updates (`tqdm` / diffusion / TTS progress bars) while updating `stepPulse` and `blastRate`.
- ANSI SGR parser supports 16-color, bold, 256-color (`38;5;n`), and 24-bit RGB (`38;2;r;g;b`), plus semantic token highlighting for uncolored ComfyUI/OTR tags, percentages, and units (`it/s`, `tok/s`, `MiB`, `GiB`).
- Ring buffer capped at `MAX_LINES = 3500` with mouse-wheel scrollback rendered inside the GPU shader texture.

## [S5] 2-Pass WebGL2 Glyph-Alpha & Temporal Feedback Pipeline (`web_app/index.html`)
- Offscreen 2D `textCanvas` is cleared to transparent (`ctx.clearRect`) every frame so `texel.a > 0` exclusively on rendered glyphs, progress bars, and spectrum/waveform traces.
- `u_textTex` uploaded via `gl.texSubImage2D` (`UNPACK_FLIP_Y_WEBGL = true`, `UNPACK_PREMULTIPLY_ALPHA_WEBGL = false`).
- `u_audioTex` (`256x2` `gl.R8`) uploaded via `gl.texSubImage2D` (`512 bytes`/frame).
- Pass 1 (`FEEDBACK_FRAG` -> ping-pong `fboA`/`fboB`): computes temporal cathode persistence (CRT / P7 Radio Scope), downward per-column character-wake rain (`Cyberpunk Matrix`), or radial zoom light-echo (`Arcade Neon`).
- Pass 2 (`COMPOSITE_FRAG` -> screen): applies bass-thump zoom, barrel curvature, degauss wobble, radial shockwave UV refraction, preset UV glitch/beam deflection, RGB chromatic aberration, 9-tap HDR bloom, procedural background fields (grid / Tektronix graticule + live audio wave / 3D synthwave horizon + FFT skyline), scanline beam-width modulation, and aperture grille.

## [S6] Frontend Auto-Popup & Headless Integration (`js/log_blaster.js`, `scripts/watch_and_pop.ps1`)
- Synchronous `api.queuePrompt` wrapper opens `/logblaster` inside the user's Run click gesture (preventing popup-blocker suppression).
- Asynchronous `execution_start` listener checks `BroadcastChannel` heartbeat and falls back to a resizable embedded `<iframe>` dock with a 1-click `Pop Out Window` button if `window.open` returns `null`.
- Broadcasts `{ id: title }` map from `app.graph._nodes` so the HUD displays human-readable node titles.

## [S7] Open Review Questions for Panel (Defects & High-Value Feature Polish)
1. Are there any subtle WebGL2, Web Audio, WebSocket lifecycle, CR/ANSI parser, HiDPI/resize, or ComfyUI frontend compatibility bugs in the actual source files?
2. Notice on Windows Chromium dual-GPU laptops: ANGLE D3D11 sometimes ignores `powerPreference: "low-power"` unless specific flags or OS graphics settings apply (we observed `FX GPU: NVIDIA GeForce RTX 5080` in headless Chrome DevTools). How should we handle/document/optimize GPU budget (e.g. optional 30/60 FPS cap toggle when ComfyUI is actively rendering vs 120Hz monitor refresh) so even when Chromium picks the dGPU, overhead stays near-zero?
3. What high-impact, lean user features (e.g. instant log search/filter `/` inside the GPU terminal, error-jump bookmark, node-timing breakdown or mini waterfall, URL/saved preferences, or ComfyUI `/system_stats` fallback on non-NVIDIA Macs/AMD) would make daily workflow runs markedly more fun and useful without adding bloat?
