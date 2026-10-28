"""Unit and contract tests for ComfyUI-LogBlaster."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
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


def test_no_utf8_bom_anywhere() -> None:
    exts = {".py", ".js", ".html", ".toml", ".md", ".bat", ".ps1"}
    checked = 0
    for path in REPO_ROOT.rglob("*"):
        if path.is_file() and path.suffix.lower() in exts and ".git" not in path.parts:
            raw = path.read_bytes()
            assert not raw.startswith(b"\xef\xbb\xbf"), f"BOM found in {path}"
            raw.decode("utf-8")
            checked += 1
    assert checked >= 7


def test_telemetry_snapshot_schema_and_no_paths() -> None:
    telemetry = _load_pkg_module("lb_telemetry_test", "telemetry.py")
    snap = telemetry.get_gpu_telemetry()
    required_keys = {
        "ok",
        "available",
        "gpu_name",
        "gpu_util",
        "mem_util",
        "vram_used_mib",
        "vram_total_mib",
        "temp_c",
        "power_w",
    }
    assert required_keys.issubset(snap.keys())
    serialized = json.dumps(snap)
    assert not re.search(r"[A-Za-z]:\\", serialized)
    assert "/Users/" not in serialized


def test_telemetry_unavailable_fallback() -> None:
    telemetry = _load_pkg_module("lb_telemetry_fallback", "telemetry.py")
    reader = telemetry.NvmlReader()
    reader._failed = True
    snap = reader.snapshot()
    assert snap["ok"] is False
    assert snap["available"] is False
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


def test_http_routes_and_shader_html() -> None:
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
    assert "/logblaster" in stub_server.routes.handlers
    assert "/logblaster/telemetry" in stub_server.routes.handlers

    html_resp: web.Response = asyncio.run(stub_server.routes.handlers["/logblaster"](None))
    assert html_resp.status == 200
    assert "Access-Control-Allow-Origin" not in html_resp.headers
    html_body = html_resp.text
    assert html_body is not None
    assert "#version 300 es" in html_body
    assert "/internal/logs/subscribe" in html_body
    assert "RETRO CRT PHOSPHOR" in html_body
    assert "CYBERPUNK GLITCH // MATRIX" in html_body
    assert "VINTAGE RADIO OSCILLOSCOPE" in html_body
    assert "ARCADE NEON // SYNTHWAVE" in html_body
    assert 'powerPreference: preferHighGpu ? "high-performance" : "low-power"' in html_body

    telem_resp: web.Response = asyncio.run(
        stub_server.routes.handlers["/logblaster/telemetry"](None)
    )
    assert telem_resp.status == 200
    assert "Access-Control-Allow-Origin" not in telem_resp.headers
    telem_data = json.loads(telem_resp.text)
    assert "vram_used_mib" in telem_data
