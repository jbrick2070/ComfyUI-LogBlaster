"""Unit and contract tests for ComfyUI-LogBlaster."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict

from aiohttp import web

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_pkg_module(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / rel_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_no_utf8_bom_or_zero_byte_files() -> None:
    exts = {".py", ".js", ".html", ".toml", ".md", ".bat", ".ps1"}
    checked = 0
    for path in REPO_ROOT.rglob("*"):
        if (
            path.is_file()
            and path.suffix.lower() in exts
            and ".git" not in path.parts
            and "kibitz-runs" not in path.parts
            and ".kibitz" not in path.parts
        ):
            raw = path.read_bytes()
            assert len(raw) > 0, f"0-byte file: {path}"
            assert not raw.startswith(b"\xef\xbb\xbf"), f"BOM found in {path}"
            raw.decode("utf-8")
            if path.suffix.lower() == ".py":
                ast.parse(raw.decode("utf-8"), filename=str(path))
            checked += 1
    assert checked >= 8


def test_telemetry_snapshot_schema_and_no_paths() -> None:
    telemetry = _load_pkg_module("lb_telemetry_test", "telemetry.py")
    snap = telemetry.get_gpu_telemetry()
    required_keys = {
        "ok",
        "available",
        "metrics_mode",
        "gpu_name",
        "gpu_util",
        "mem_util",
        "vram_used_mib",
        "vram_total_mib",
        "temp_c",
        "power_w",
    }
    assert required_keys.issubset(snap.keys())
    assert snap["metrics_mode"] in {"nvml", "unavailable"}
    serialized = json.dumps(snap)
    assert not re.search(r"[A-Za-z]:\\", serialized)
    assert "/Users/" not in serialized

    assert telemetry.resolve_nvml_device_index({"LOGBLASTER_NVML_INDEX": "2"}) == 2
    assert telemetry.resolve_nvml_device_index({"CUDA_VISIBLE_DEVICES": "3,1"}) == 3
    assert telemetry.resolve_nvml_device_index({"CUDA_VISIBLE_DEVICES": "GPU-uuid-stub"}) == 0
    assert telemetry.resolve_nvml_device_index({}) == 0


def test_telemetry_unavailable_fallback() -> None:
    telemetry = _load_pkg_module("lb_telemetry_fallback", "telemetry.py")
    reader = telemetry.NvmlReader()
    reader._failed = True
    snap = reader.snapshot()
    assert snap["ok"] is False
    assert snap["available"] is False
    assert snap["metrics_mode"] == "unavailable"
    assert snap["vram_total_mib"] == 0


def test_stage_cue_node_contract(capsys) -> None:
    nodes_mod = _load_pkg_module("lb_nodes_test", "nodes.py")
    cls = nodes_mod.NODE_CLASS_MAPPINGS["LogBlaster_StageCue"]
    spec = cls.INPUT_TYPES()
    assert "banner" in spec["required"]
    assert "fx_trigger" in spec["required"]
    assert "passthrough" in spec["optional"]

    node = cls()
    sentinel = {"frame_count": 121}
    result = node.emit_cue(banner="ACT II\nDROP", fx_trigger="SHOCKWAVE", passthrough=sentinel)
    assert result == (sentinel,)
    captured = capsys.readouterr().out
    assert "[LogBlaster:SHOCKWAVE]" in captured
    assert "ACT II DROP" in captured


def test_http_routes_shaders_and_hardened_contracts() -> None:
    sys.modules.pop("_comfyui_logblaster_singleton", None)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    init_mod = _load_pkg_module("lb_init_test", "__init__.py")

    class StubRouteTable:
        def __init__(self) -> None:
            self.handlers: Dict[str, Callable[..., Any]] = {}

        def get(self, route_path: str):
            def decorator(fn: Callable[..., Any]):
                self.handlers[route_path] = fn
                return fn

            return decorator

    class StubPromptServer:
        def __init__(self) -> None:
            self.routes = StubRouteTable()

    stub_server = StubPromptServer()
    assert init_mod.register_routes(stub_server) is True
    # Duplicate registration guard must return False on second call
    assert init_mod.register_routes(stub_server) is False
    assert "/logblaster" in stub_server.routes.handlers
    assert "/logblaster/telemetry" in stub_server.routes.handlers

    html_resp: web.Response = asyncio.run(stub_server.routes.handlers["/logblaster"](None))
    assert html_resp.status == 200
    assert "Access-Control-Allow-Origin" not in html_resp.headers
    assert html_resp.headers.get("Cache-Control") == "no-store"
    html_body = html_resp.text
    assert html_body is not None

    # 4 WebGL2 shaders + multi-scale view modes (CASCADE | DUAL | DRUM | FLAT) + COMFY B&Y palette
    assert "#version 300 es" in html_body
    assert "/internal/logs/subscribe" in html_body
    assert "RETRO CRT PHOSPHOR" in html_body
    assert "CYBERPUNK GLITCH // MATRIX" in html_body
    assert "VINTAGE RADIO OSCILLOSCOPE" in html_body
    assert "ARCADE NEON // SYNTHWAVE" in html_body
    assert 'const VIEW_MODES = ["CASCADE", "DUAL", "DRUM", "FLAT"]' in html_body
    assert 'const TINT_NAMES = ["AUTO", "COMFY B&Y", "AMBER P3", "GREEN P31", "ICE CYAN"]' in html_body
    assert "wrapSpansMonospace" in html_body
    assert "u_viewMode" in html_body
    assert "u_tintMode" in html_body
    assert "VIEW: CASCADE" in html_body

    # Bounded render overhead, privacy redaction, context recovery, fallback telemetry & unsubscribe
    assert 'powerPreference: preferHighGpu ? "high-performance" : "low-power"' in html_body
    assert "FPS: ECO" in html_body
    assert "MAX_RENDER_PIXELS" in html_body
    assert "MAX_LINE_CHARS" in html_body
    assert "checkFramebufferStatus" in html_body
    assert "webglcontextlost" in html_body
    assert "redactSensitiveText" in html_body
    assert "/system_stats" in html_body
    assert "state.logSubscribed" in html_body
    assert "keepalive: true" in html_body

    # Zero-echo Web Audio topology: analyser must NEVER connect to audioCtx.destination
    assert "createAnalyser" in html_body
    assert "u_audioTex" in html_body
    assert "analyser.connect(audioCtx.destination)" not in html_body
    assert "externalSourceNode.connect(analyser)" in html_body
    assert "synthMasterGain.connect(audioCtx.destination)" in html_body

    telem_resp: web.Response = asyncio.run(
        stub_server.routes.handlers["/logblaster/telemetry"](None)
    )
    assert telem_resp.status == 200
    assert "Access-Control-Allow-Origin" not in telem_resp.headers
    telem_data = json.loads(telem_resp.text)
    assert "vram_used_mib" in telem_data
    assert "metrics_mode" in telem_data


def test_frontend_js_syntax_and_unit_behaviors(tmp_path: Path) -> None:
    node_bin = shutil.which("node")
    if not node_bin:
        return
    js_file = REPO_ROOT / "js" / "log_blaster.js"
    proc = subprocess.run([node_bin, "--check", str(js_file)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    html_text = (REPO_ROOT / "web_app" / "index.html").read_text(encoding="utf-8")
    m = re.search(r"<script>([\s\S]*)</script>", html_text)
    assert m is not None, "Missing <script> block in web_app/index.html"
    inline_src = m.group(1)
    inline_js = tmp_path / "inline_app.js"
    inline_js.write_text(inline_src, encoding="utf-8")
    proc_inline = subprocess.run(
        [node_bin, "--check", str(inline_js)], capture_output=True, text=True
    )
    assert proc_inline.returncode == 0, proc_inline.stderr

    # Extract redactSensitiveText and wrapSpansMonospace and run real behavioral assertions in Node
    redact_match = re.search(
        r"function redactSensitiveText\(input\)\s*\{[\s\S]*?\n  \}", inline_src
    )
    wrap_match = re.search(
        r"function wrapSpansMonospace\(spans, maxCols, maxRows\)\s*\{[\s\S]*?\n  \}",
        inline_src,
    )
    assert redact_match is not None, "Could not locate redactSensitiveText"
    assert wrap_match is not None, "Could not locate wrapSpansMonospace"

    harness = f"""
const assert = require("assert");
const state = {{ redactEnabled: true }};
{redact_match.group(0)}
{wrap_match.group(0)}

// 1. Positive redaction vectors
assert.strictEqual(
  redactSensitiveText("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"),
  "Authorization: Bearer ***REDACTED***"
);
assert.strictEqual(
  redactSensitiveText("Bearer super_secret_token_999"),
  "Bearer ***REDACTED***"
);
assert.strictEqual(
  redactSensitiveText("key=sk-proj-1234567890abcdef"),
  "key=sk-pro***REDACTED***"
);
assert.strictEqual(
  redactSensitiveText("aws=AKIAIOSFODNN7EXAMPLE"),
  "aws=AKIA***REDACTED***"
);
assert.strictEqual(
  redactSensitiveText("GET https://api.example.test/v1?api_key=mysecretvalue123&mode=fast"),
  "GET https://api.example.test/v1?api_key=***REDACTED***&mode=fast"
);
assert.strictEqual(
  redactSensitiveText("Loading C:\\\\Users\\\\jeffr\\\\Documents\\\\model.safetensors"),
  "Loading ~\\\\Documents\\\\model.safetensors"
);

// 2. Negative redaction vectors (normal ComfyUI logs must remain untouched)
const cleanLine = "[OldTimeRadio] Stage 1/4: 42.8 tok/s | LOG TOKEN SPECTRUM";
assert.strictEqual(redactSensitiveText(cleanLine), cleanLine);

// 3. wrapSpansMonospace preserves colored spans & prevents 3-char orphan tails
const spans = [
  {{ text: "Prompt executed in ", color: "#4dffb5", hot: false }},
  {{ text: "00:04.68", color: "#ffe680", hot: true }},
  {{ text: " === COMPLETE ===", color: "#4dffb5", hot: false }},
];
const rows = wrapSpansMonospace(spans, 41, 3);
assert.strictEqual(rows.length, 2);
const row2Text = rows[1].map((s) => s.text).join("");
assert.ok(row2Text.length >= 8, "Expected non-orphan row 2, got: " + row2Text);
assert.strictEqual(rows[0][1].color, "#ffe680");
console.log("JS_UNIT_OK");
"""
    harness_js = tmp_path / "unit_harness.js"
    harness_js.write_text(harness, encoding="utf-8")
    proc_unit = subprocess.run(
        [node_bin, str(harness_js)], capture_output=True, text=True
    )
    assert proc_unit.returncode == 0, proc_unit.stderr
    assert "JS_UNIT_OK" in proc_unit.stdout
