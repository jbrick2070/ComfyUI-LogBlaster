"""ComfyUI-LogBlaster -- Real-time WebGL2 GPU Shader Log Visualizer for ComfyUI.

Serves an interactive WebGL2 GPU-processed terminal visualizer at GET /logblaster
that streams ComfyUI's live stdout/stderr (/internal/logs/subscribe + /ws) and
renders the live log text itself through switchable GLSL fragment shaders
(Retro CRT Phosphor, Cyberpunk Glitch/Matrix, Vintage Radio Oscilloscope,
Arcade Neon) while offloading shader work to the low-power integrated GPU.
"""

from __future__ import annotations

import os
import sys
import types
from typing import Any, Dict

try:
    from .nodes import (
        NODE_CLASS_MAPPINGS as _RAW_NODE_CLASS_MAPPINGS,
        NODE_DISPLAY_NAME_MAPPINGS as _RAW_NODE_DISPLAY_NAME_MAPPINGS,
    )
    from .telemetry import get_gpu_telemetry
except ImportError:
    from nodes import (  # type: ignore
        NODE_CLASS_MAPPINGS as _RAW_NODE_CLASS_MAPPINGS,
        NODE_DISPLAY_NAME_MAPPINGS as _RAW_NODE_DISPLAY_NAME_MAPPINGS,
    )
    from telemetry import get_gpu_telemetry  # type: ignore

_SINGLETON_KEY = "_comfyui_logblaster_singleton"
_IS_DUPLICATE_LOAD = _SINGLETON_KEY in sys.modules
if not _IS_DUPLICATE_LOAD:
    _guard_mod = types.ModuleType(_SINGLETON_KEY)
    _guard_mod.origin = os.path.dirname(os.path.abspath(__file__))  # type: ignore[attr-defined]
    sys.modules[_SINGLETON_KEY] = _guard_mod

NODE_CLASS_MAPPINGS: Dict[str, Any] = {} if _IS_DUPLICATE_LOAD else dict(_RAW_NODE_CLASS_MAPPINGS)
NODE_DISPLAY_NAME_MAPPINGS: Dict[str, str] = (
    {} if _IS_DUPLICATE_LOAD else dict(_RAW_NODE_DISPLAY_NAME_MAPPINGS)
)

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_HTML_PATH = os.path.join(_PKG_DIR, "web_app", "index.html")

_SAFE_HEADERS = {
    "Cache-Control": "no-store",
}


def register_routes(prompt_server: Any) -> bool:
    """Attach read-only GET /logblaster and GET /logblaster/telemetry routes idempotently."""
    if prompt_server is None or getattr(prompt_server, "routes", None) is None:
        return False
    if getattr(prompt_server, "_logblaster_routes_registered", False):
        return False

    from aiohttp import web  # type: ignore

    @prompt_server.routes.get("/logblaster")
    async def _serve_logblaster_app(request: Any) -> Any:
        try:
            with open(_HTML_PATH, "r", encoding="utf-8") as fh:
                html_text = fh.read()
            return web.Response(
                text=html_text,
                content_type="text/html",
                charset="utf-8",
                headers=_SAFE_HEADERS,
            )
        except Exception as exc:
            print(f"[LogBlaster] could not read web_app/index.html: {exc}")
            return web.Response(
                text="LogBlaster UI unavailable; check ComfyUI console.",
                status=500,
                content_type="text/plain",
                charset="utf-8",
                headers=_SAFE_HEADERS,
            )

    @prompt_server.routes.get("/logblaster/telemetry")
    async def _serve_logblaster_telemetry(request: Any) -> Any:
        payload = get_gpu_telemetry()
        return web.json_response(payload, headers=_SAFE_HEADERS)

    setattr(prompt_server, "_logblaster_routes_registered", True)
    return True


if not _IS_DUPLICATE_LOAD:
    try:
        from server import PromptServer  # type: ignore
    except (ImportError, ModuleNotFoundError):
        # Imported outside a live ComfyUI runtime (e.g. pytest or AST audit).
        PromptServer = None  # type: ignore

    if PromptServer is not None:
        try:
            if getattr(PromptServer, "instance", None) is not None:
                if register_routes(PromptServer.instance):
                    print(
                        "[LogBlaster] GPU Shader Log HUD ready at /logblaster "
                        "(auto-popup extension loaded)"
                    )
        except Exception as _route_exc:
            print(f"[LogBlaster] Failed to register HTTP routes: {_route_exc}")

WEB_DIRECTORY = None if _IS_DUPLICATE_LOAD else "./js"

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
    "register_routes",
]
