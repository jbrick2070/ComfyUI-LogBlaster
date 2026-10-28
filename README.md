# ComfyUI-LogBlaster

Real-time **WebGL2 GPU Shader Log-Text Visualizer** for ComfyUI.

Instead of plain HTML text over a static background, `ComfyUI-LogBlaster` renders ComfyUI's live blasting `stdout`/`stderr` stream (`/internal/logs/subscribe` + `/ws`) onto an offscreen glyph-alpha texture and processes **the log characters themselves** through a 2-pass WebGL2 ping-pong fragment shader pipeline.

## Zero Impact on ComfyUI GPU Renders

- Requests `powerPreference: "low-power"` on its WebGL2 context so dual-GPU machines (e.g. **Intel integrated GPU + NVIDIA RTX 5080**) run all shader passes on the idle integrated GPU (`0 MiB` discrete VRAM stolen, `0%` CUDA contention).
- Reads live NVIDIA GPU utilization, VRAM, temperature, and power draw directly via `ctypes` (`nvml.dll` / `libnvidia-ml.so.1`) in ~0.03 ms — zero `torch.cuda` context initialization and zero `nvidia-smi` subprocesses.
- Automatically pauses `requestAnimationFrame` when minimized or hidden.

## 4 Switchable GPU Shader Presets

| Hotkey | Preset | Log-Text GPU Effects |
| --- | --- | --- |
| `1` | **Retro CRT Phosphor** | Curved CRT barrel glass, 9-tap HDR phosphor bloom, temporal cathode afterglow, scanline beam-width widening on bright glyphs, RGB aperture grille, degauss shake on workflow start |
| `2` | **Cyberpunk Glitch // Matrix** | Velocity-reactive horizontal scan-band slicing on log lines, dynamic RGB chromatic aberration prisms, Matrix digital rain dripping directly downward off live log letters |
| `3` | **Vintage Radio Oscilloscope** | Illuminated copper/emerald Tektronix etched graticule, dual-stage P7 radar phosphor decay, live carrier waveform, magnetic electron-beam deflection across log lines |
| `4` | **Arcade Neon // Synthwave** | 3D receding synthwave horizon grid, plasma gradient highlights, physical radial shockwave rings that refract and bend log characters on node transitions |

## Controls & Hotkeys

- **`1` – `4`**: Switch GPU shader preset
- **`T`**: Cycle phosphor palette (`AUTO` -> `AMBER P3` -> `GREEN P31` -> `ICE CYAN`)
- **`FX` Slider**: Dial shader intensity from `0%` (razor-sharp raw terminal) to `160%` (overdrive)
- **`B`**: Trigger **`TEST BLAST`** (synthetic multi-stage ComfyUI + TTS + diffusion log storm anytime, even while idle)
- **`Space` / Mouse Wheel**: Pause live follow & scroll back through 3,500 lines *inside* the GPU shader
- **`-` / `+`**: Adjust terminal font size (`11px`–`22px`)
- **`F`**: Toggle fullscreen

## Launching

1. **Inside ComfyUI UI**: Click **`LOG BLASTER`** in the top bar, or leave **`AUTO:ON`** enabled so the HUD pops open automatically whenever you run a workflow.
2. **Direct URL**: Open `http://127.0.0.1:8000/logblaster` in any browser.
3. **Chromeless Native App Window**: Double-click `launch_log_blaster.bat` (launches `msedge --app=http://127.0.0.1:8000/logblaster`).
4. **Headless Auto-Popup Watcher**: Run `powershell -ExecutionPolicy Bypass -File scripts\watch_and_pop.ps1` to auto-pop the borderless HUD window whenever a headless API job starts on `:8000`.
