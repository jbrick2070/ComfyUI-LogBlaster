"""Unit and contract tests for ComfyUI-LogBlaster."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

import pytest
from aiohttp import web
# Collected by the required single-file command via pytest's default prepend mode;
# keep tests/ unpackaged, or replace this sibling import if changing import mode.
from telemetry_failure_cases import (
    test_nvml_metric_failure_and_recovery,
    test_nvml_transient_init_failure_retries,
    test_nvml_explicit_target_never_falls_back,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_pkg_module(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / rel_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_no_utf8_bom_or_zero_byte_files() -> None:
    exts = {".py", ".js", ".cjs", ".html", ".toml", ".md", ".bat", ".ps1", ".yml"}
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
        "device_index",
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
    assert telemetry.resolve_nvml_device_index({"LOGBLASTER_NVML_INDEX": "invalid"}) == -1
    assert telemetry.resolve_nvml_device_index({"CUDA_VISIBLE_DEVICES": "3,1"}) == 3
    assert telemetry.resolve_nvml_device_index({"CUDA_VISIBLE_DEVICES": "GPU-uuid-stub"}) == 0
    assert telemetry.resolve_nvml_device_index({"CUDA_VISIBLE_DEVICES": "-1"}) == -1
    assert telemetry.resolve_nvml_device_index({}) == 0
    assert telemetry.resolve_nvml_target({"LOGBLASTER_NVML_INDEX": "GPU-1234"}) == (
        "explicit_uuid",
        "GPU-1234",
    )


def test_telemetry_unavailable_and_explicit_non_fallback() -> None:
    telemetry = _load_pkg_module("lb_telemetry_fallback", "telemetry.py")
    reader = telemetry.NvmlReader()
    reader._failed = True
    snap = reader.snapshot()
    assert snap["ok"] is False
    assert snap["available"] is False
    assert snap["metrics_mode"] == "unavailable"
    assert snap["device_index"] is None
    assert snap["vram_total_mib"] == 0

    invalid_env_reader = telemetry.NvmlReader(env={"LOGBLASTER_NVML_INDEX": "not_an_index"})
    invalid_snap = invalid_env_reader.snapshot()
    assert invalid_snap["available"] is False
    assert invalid_snap["metrics_mode"] == "unavailable"

    out_of_range_reader = telemetry.NvmlReader(device_index=999)
    oor_snap = out_of_range_reader.snapshot()
    assert oor_snap["available"] is False
    assert oor_snap["metrics_mode"] == "unavailable"


def test_stage_cue_node_contract_and_cache_semantics(capsys) -> None:
    nodes_mod = _load_pkg_module("lb_nodes_test", "nodes.py")
    cls = nodes_mod.NODE_CLASS_MAPPINGS["LogBlaster_StageCue"]
    spec = cls.INPUT_TYPES()
    assert "banner" in spec["required"]
    assert "fx_trigger" in spec["required"]
    assert "passthrough" in spec["optional"]
    assert "always_fire" in spec["optional"]

    assert cls.IS_CHANGED(banner="ACT II", fx_trigger="SHOCKWAVE", always_fire=False) == "SHOCKWAVE:ACT II"
    nan_token = cls.IS_CHANGED(banner="ACT II", fx_trigger="SHOCKWAVE", always_fire=True)
    assert isinstance(nan_token, float) and math.isnan(nan_token)

    node = cls()
    sentinel = {"frame_count": 121}
    result = node.emit_cue(
        banner="ACT II\nDROP",
        fx_trigger="SHOCKWAVE",
        passthrough=sentinel,
        always_fire=False,
    )
    assert result == (sentinel,)
    captured = capsys.readouterr().out
    assert "[LogBlaster:SHOCKWAVE]" in captured
    assert "ACT II DROP" in captured


def test_http_routes_observer_tap_and_hardened_contracts() -> None:
    sys.modules.pop("_comfyui_logblaster_singleton", None)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    init_mod = _load_pkg_module("lb_init_test", "__init__.py")
    init_src = (REPO_ROOT / "__init__.py").read_text(encoding="utf-8")
    assert "await asyncio.to_thread(_read_html_file)" in init_src
    assert "await asyncio.to_thread(get_gpu_telemetry)" in init_src

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
            self.sockets: Any = {
                "ui-tab-primary": object(),
                "logblaster-observer-1": object(),
            }
            self.sent: List[Tuple[Any, Any, Any]] = []

        def send_sync(self, event: Any, data: Any, sid: Any = None) -> None:
            self.sent.append((event, data, sid))

    stub_server = StubPromptServer()
    assert init_mod.register_routes(stub_server) is True
    assert init_mod.register_routes(stub_server) is False
    assert "/logblaster" in stub_server.routes.handlers
    assert "/logblaster/telemetry" in stub_server.routes.handlers

    # Directed execution events (sid != None) forward to logblaster-* observer sockets
    stub_server.send_sync("executing", {"node": "42", "prompt_id": "p1"}, "ui-tab-primary")
    assert stub_server.sent == [
        ("executing", {"node": "42", "prompt_id": "p1"}, "ui-tab-primary"),
        ("executing", {"node": "42", "prompt_id": "p1"}, "logblaster-observer-1"),
    ]
    stub_server.sent.clear()

    # Broadcast events (sid is None) do NOT duplicate
    stub_server.send_sync("status", {"status": {"exec_info": {"queue_remaining": 1}}}, None)
    assert stub_server.sent == [
        ("status", {"status": {"exec_info": {"queue_remaining": 1}}}, None),
    ]
    stub_server.sent.clear()

    # Events already addressed to a logblaster-* socket do NOT self-echo
    stub_server.send_sync("execution_start", {"prompt_id": "p2"}, "logblaster-observer-1")
    assert stub_server.sent == [
        ("execution_start", {"prompt_id": "p2"}, "logblaster-observer-1"),
    ]
    stub_server.sent.clear()

    # Broken/mutated sockets attribute must never raise into PromptExecutor
    class BrokenSocketsDict(dict):
        def keys(self):  # type: ignore[override]
            raise RuntimeError("simulated concurrent socket dict error")

    orig_sockets = stub_server.sockets
    stub_server.sockets = BrokenSocketsDict({"logblaster-x": object()})
    stub_server.send_sync("executing", {"node": "99"}, "ui-tab-primary")
    assert stub_server.sent == [("executing", {"node": "99"}, "ui-tab-primary")]
    stub_server.sockets = orig_sockets

    html_resp: web.Response = asyncio.run(stub_server.routes.handlers["/logblaster"](None))
    assert html_resp.status == 200
    assert "Access-Control-Allow-Origin" not in html_resp.headers
    assert html_resp.headers.get("Cache-Control") == "no-store"
    html_body = html_resp.text
    assert html_body is not None

    assert "#version 300 es" in html_body
    assert "/internal/logs/subscribe" in html_body
    assert "RETRO CRT PHOSPHOR" in html_body
    assert "CYBERPUNK GLITCH // MATRIX" in html_body
    assert "VINTAGE RADIO OSCILLOSCOPE" in html_body
    assert "ARCADE NEON // SYNTHWAVE" in html_body
    assert 'const VIEW_MODES = ["CASCADE", "DUAL", "DRUM", "FLAT"]' in html_body
    assert 'const TINT_NAMES = ["AUTO", "COMFY B&Y", "AMBER P3", "GREEN P31", "ICE CYAN"]' in html_body
    assert "wrapSpansMonospace" in html_body
    assert "injectLocalLine" in html_body
    assert "u_viewMode" in html_body
    assert "u_tintMode" in html_body
    assert "VIEW: CASCADE" in html_body

    assert 'powerPreference: preferHighGpu ? "high-performance" : "low-power"' in html_body
    assert "FPS: ECO" in html_body
    assert "MAX_RENDER_PIXELS" in html_body
    assert "MAX_LINE_CHARS" in html_body
    assert "MAX_LIVE_BUFFER_CHARS" in html_body
    assert "checkFramebufferStatus" in html_body
    assert "webglcontextlost" in html_body
    assert "redactSensitiveText" in html_body
    assert "/system_stats" in html_body
    assert "state.logSubscribed" in html_body
    assert "state.logSubAttempted" in html_body
    assert "syncLogsAfterOpen" in html_body
    assert "keepalive: true" in html_body

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
    assert "device_index" in telem_data
    assert telem_data["observers"] == 1


def _extract_js_function(source: str, signature_prefix: str) -> str:
    start = source.find(signature_prefix)
    assert start >= 0, f"Could not find JS function starting with: {signature_prefix}"
    brace_start = source.find("{", start)
    assert brace_start >= 0
    depth = 0
    for idx in range(brace_start, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"Unclosed braces in JS function: {signature_prefix}")


def test_frontend_js_syntax_and_unit_behaviors(tmp_path: Path) -> None:
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node binary required on PATH for frontend JS unit suite")
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

    fn_redact = _extract_js_function(inline_src, "function redactSensitiveText(")
    fn_wrap = _extract_js_function(inline_src, "function wrapSpansMonospace(")
    fn_recompute_err = _extract_js_function(inline_src, "function recomputeErrorCount(")
    fn_classify = _extract_js_function(inline_src, "function classifySemanticLine(")
    fn_tokenize = _extract_js_function(inline_src, "function tokenizeSpans(")
    fn_rebuild = _extract_js_function(inline_src, "function rebuildLineView(")
    fn_format_time = _extract_js_function(inline_src, "function formatTime(")
    fn_append = _extract_js_function(inline_src, "function appendOrUpdateLine(")
    fn_inject = _extract_js_function(inline_src, "function injectLocalLine(")
    fn_ingest = _extract_js_function(inline_src, "function ingestLogChunk(")
    fn_resolve_title = _extract_js_function(inline_src, "function resolveNodeTitle(")
    fn_record_seen = _extract_js_function(inline_src, "function recordSeenTimestamp(")
    fn_dedupe = _extract_js_function(inline_src, "function dedupeBoundaryEntries(")

    harness = f"""
const assert = require("assert");
const MAX_LINES = 3500;
const MAX_LINE_CHARS = 2048;
const ANSI_16 = {{ 31: "#ff4d6d", 32: "#38ffb3", 33: "#ffc845", 36: "#4df3ff" }};
const btnErrJump = {{ textContent: "", classList: {{ toggle() {{}} }} }};
const state = {{
  redactEnabled: true,
  viewMode: "CASCADE",
  fontSize: 14,
  lines: [],
  lineCounter: 0,
  linesVersion: 0,
  errorCount: 0,
  linesThisSec: 0,
  smoothScrollPx: 0,
  newestLineMs: 0,
  textDirty: false,
  flashPulse: 0,
  glitchBurst: 0,
  stepPulse: 0,
  partialLineOpen: false,
  partialCrPending: false,
  partialRawText: "",
  nodeTitleMap: new Map(),
  lastLogIsoT: "",
  seenAtLastIsoT: new Map(),
}};
function triggerShockwave() {{}}
function feedSignalAnalyzer() {{}}

{fn_redact}
{fn_wrap}
{fn_recompute_err}
{fn_classify}
{fn_tokenize}
{fn_rebuild}
{fn_format_time}
{fn_append}
{fn_inject}
{fn_ingest}
{fn_resolve_title}
{fn_record_seen}
{fn_dedupe}

function resetBuffer() {{
  state.lines = [];
  state.errorCount = 0;
  state.partialLineOpen = false;
  state.partialCrPending = false;
  state.partialRawText = "";
  state.lastLogIsoT = "";
  state.seenAtLastIsoT.clear();
}}

// 1. Positive redaction vectors (including short 5-char Bearer value & streaming partial prefixes)
assert.strictEqual(
  redactSensitiveText("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"),
  "Authorization: Bearer ***REDACTED***"
);
assert.strictEqual(
  redactSensitiveText("Authorization: Bearer super"),
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
  redactSensitiveText("Retrying https://admin:secretPass99@192.168.1.42:8188/api"),
  "Retrying https://admin:***REDACTED***@192.168.1.42:8188/api"
);
assert.strictEqual(
  redactSensitiveText("Retrying https://admin:secr", true),
  "Retrying https://admin:***REDACTED***"
);
assert.strictEqual(
  redactSensitiveText("Loading C:\\\\Users\\\\jeffr\\\\Documents\\\\model.safetensors"),
  "Loading ~\\\\Documents\\\\model.safetensors"
);

// 2. Negative redaction vectors (benign ComfyUI log text & hf_hub identifiers stay unmasked)
const cleanLine = "[OldTimeRadio] Stage 1/4: 42.8 tok/s | LOG TOKEN SPECTRUM | hf_hub ready";
assert.strictEqual(redactSensitiveText(cleanLine, false), cleanLine);

// 3. ANSI escape sequence embedded INSIDE a secret token cannot bypass redaction
resetBuffer();
ingestLogChunk("token=sk-proj-12345\\x1b[0m67890abcdef\\n");
assert.strictEqual(state.lines.length, 1);
assert.ok(!state.lines[0].viewRaw.includes("12345"), "Leaked viewRaw: " + state.lines[0].viewRaw);
const spanJoined = state.lines[0].spans.map((s) => s.text).join("");
assert.ok(!spanJoined.includes("12345"), "Leaked spans: " + spanJoined);

// 4. Redact-before-truncate across MAX_LINE_CHARS boundary
resetBuffer();
ingestLogChunk("a".repeat(2042) + "sk-proj-1234567890abcdef\\n");
assert.ok(!state.lines[0].viewRaw.includes("sk-proj-123"), "Leaked truncated token");

// 5. wrapSpansMonospace preserves colored spans & prevents 3-char orphan tails
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

// 6. Stateful partial-line framing across split stdout.write chunks + Python print
resetBuffer();
ingestLogChunk("hel");
ingestLogChunk("lo\\n");
ingestLogChunk("line 2");
ingestLogChunk("\\n");
assert.deepStrictEqual(state.lines.map((l) => l.viewRaw), ["hello", "line 2"]);

// 7. Split CRLF ("\\r" at end of chunk 1, "\\n" at start of chunk 2)
resetBuffer();
ingestLogChunk("alpha\\r");
ingestLogChunk("\\nbeta\\r\\n");
assert.deepStrictEqual(state.lines.map((l) => l.viewRaw), ["alpha", "beta"]);

// 8. Interleaved synthetic HUD event lines MUST NOT corrupt open partial stdout or tqdm lines!
resetBuffer();
ingestLogChunk("hel");
injectLocalLine("[ComfyUI] Executing KSampler (#5)");
ingestLogChunk("lo\\n");
assert.deepStrictEqual(state.lines.map((l) => l.viewRaw), [
  "[ComfyUI] Executing KSampler (#5)",
  "hello",
]);

resetBuffer();
ingestLogChunk("\\r 50%|#####     | 5/10");
injectLocalLine("[WARNING] Workflow execution interrupted");
ingestLogChunk("\\r 60%|######    | 6/10\\n");
assert.deepStrictEqual(state.lines.map((l) => l.viewRaw), [
  "[WARNING] Workflow execution interrupted",
  " 60%|######    | 6/10",
]);

// 9. Secret token split across chunks is masked BOTH mid-stream AND when joined!
resetBuffer();
ingestLogChunk("Authorization: Bearer ");
ingestLogChunk("sup");
assert.strictEqual(state.lines[0].viewRaw, "Authorization: Bearer ***REDACTED***");
ingestLogChunk("er_secret_jwt_999\\n");
assert.deepStrictEqual(state.lines.map((l) => l.viewRaw), [
  "Authorization: Bearer ***REDACTED***",
]);

resetBuffer();
ingestLogChunk("api_token=sk-pr");
assert.ok(!state.lines[0].viewRaw.endsWith("sk-pr"), "Expected partial prefix mask: " + state.lines[0].viewRaw);
ingestLogChunk("oj-1234567890abcdef\\n");
assert.ok(state.lines[0].viewRaw.includes("***REDACTED***"));

// 10. Adversarial 1,000,000-character partial chunk strictly bounded before concatenation
resetBuffer();
ingestLogChunk("x");
ingestLogChunk("a".repeat(1000000));
assert.ok(
  state.partialRawText.length <= MAX_LINE_CHARS + 128,
  "partialRawText unbounded: " + state.partialRawText.length
);
assert.ok(state.lines[0].viewRaw.length <= MAX_LINE_CHARS + 20);

// 11. Boundary-timestamp (t, m) multiset deduplication preserves same-timestamp subsequent "\\n"
resetBuffer();
const sameT = "2026-10-28T17:00:00.123456";
recordSeenTimestamp(sameT, "hello");
const drained = dedupeBoundaryEntries(
  [
    {{ t: sameT, m: "hello" }},
    {{ t: sameT, m: "\\n" }},
    {{ t: "2026-10-28T17:00:00.123999", m: "world\\n" }},
  ],
  state.lastLogIsoT,
  state.seenAtLastIsoT
);
assert.deepStrictEqual(drained.map((e) => e.m), ["\\n", "world\\n"]);

// 12. Subgraph execution ID ("12:5") resolves root subgraph container ("12"), never unrelated root #5
state.nodeTitleMap.clear();
state.nodeTitleMap.set("5", "Unrelated Root KSampler");
state.nodeTitleMap.set("12", "Audio Mastering Subgraph");
assert.strictEqual(resolveNodeTitle("5"), "Unrelated Root KSampler (#5)");
assert.strictEqual(resolveNodeTitle("12:5"), "Audio Mastering Subgraph › #12:5");
assert.strictEqual(resolveNodeTitle("99:4"), "Node #99:4");

console.log("JS_UNIT_OK");
"""
    harness_js = tmp_path / "unit_harness.js"
    harness_js.write_text(harness, encoding="utf-8")
    proc_unit = subprocess.run(
        [node_bin, str(harness_js)], capture_output=True, text=True
    )
    assert proc_unit.returncode == 0, proc_unit.stderr
    assert "JS_UNIT_OK" in proc_unit.stdout


@pytest.mark.parametrize("harness, sentinel", [
    ("extension_harness.cjs", "EXTENSION_UNIT_OK"),
    ("hud_harness.cjs", "HUD_INTEGRATION_OK"),
])
def test_browser_script_integration(harness: str, sentinel: str) -> None:
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node binary required for browser script integration tests")
    result = subprocess.run(
        [node_bin, str(REPO_ROOT / "tests" / harness)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert sentinel in result.stdout, f"Harness did not finish: {result.stdout}"
