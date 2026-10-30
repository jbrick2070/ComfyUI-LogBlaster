"""ComfyUI-LogBlaster -- Real-time WebGL2 GPU Shader Log Visualizer for ComfyUI.

Serves an interactive WebGL2 GPU-processed terminal visualizer at GET /logblaster
that streams ComfyUI's live stdout/stderr (/internal/logs/subscribe + /ws) and
renders the live log text itself through switchable GLSL fragment shaders
(Retro CRT Phosphor, Cyberpunk Glitch/Matrix, Vintage Radio Oscilloscope,
Arcade Neon) while offloading shader work to the low-power integrated GPU.
"""

from __future__ import annotations

import asyncio
import html
import os
import re
import sys
import types
from typing import Any, Dict

if __package__ not in (None, ""):
    from .nodes import (
        NODE_CLASS_MAPPINGS as _RAW_NODE_CLASS_MAPPINGS,
        NODE_DISPLAY_NAME_MAPPINGS as _RAW_NODE_DISPLAY_NAME_MAPPINGS,
    )
    from .telemetry import get_gpu_telemetry
else:
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
#: Where web_app/index.html shows the version (its title and the control dock).
_VERSION_TOKEN = "__LOGBLASTER_VERSION__"

_SAFE_HEADERS = {
    "Cache-Control": "no-store",
}

_OBSERVED_EXECUTION_EVENTS = frozenset(
    {
        "execution_start",
        "executing",
        "progress",
        "execution_error",
        "execution_success",
        "execution_interrupted",
        "execution_cached",
    }
)


def _git_commit(pkg_dir: str) -> str:
    """The short commit this copy is checked out at, or "" (a registry install
    has no .git). Read from the files, so serving a page never runs git."""
    try:
        git_dir = os.path.join(pkg_dir, ".git")
        if os.path.isfile(git_dir):  # a worktree: ".git" names the real git dir
            with open(git_dir, "r", encoding="utf-8") as fh:
                pointer = fh.read().strip()
            if not pointer.startswith("gitdir:"):
                return ""
            git_dir = os.path.normpath(os.path.join(pkg_dir, pointer[len("gitdir:"):].strip()))
        with open(os.path.join(git_dir, "HEAD"), "r", encoding="utf-8") as fh:
            sha = fh.read().strip()
        # A symbolic ref may name another (HEAD -> alias -> main); git itself stops
        # following after five, and so does this.
        for _ in range(5):
            if not sha.startswith("ref:"):
                break
            sha = _read_ref(git_dir, sha[len("ref:"):].strip())
        return sha[:7] if re.fullmatch(r"[0-9a-f]{40,64}", sha) else ""
    except (OSError, UnicodeDecodeError):
        return ""


def _read_ref(git_dir: str, ref: str) -> str:
    """What a ref holds (a sha, or "ref: <another ref>"), from its loose file or
    packed-refs, in this git dir or a worktree's shared one; "" when absent."""
    for base in (git_dir, _common_git_dir(git_dir)):
        loose = os.path.join(base, *ref.split("/"))
        if os.path.isfile(loose):
            with open(loose, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        packed = os.path.join(base, "packed-refs")
        if os.path.isfile(packed):
            with open(packed, "r", encoding="utf-8") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == ref:
                        return parts[0]
    return ""


def _common_git_dir(git_dir: str) -> str:
    """A worktree's shared git dir (its `commondir` file), else the dir itself."""
    try:
        with open(os.path.join(git_dir, "commondir"), "r", encoding="utf-8") as fh:
            return os.path.normpath(os.path.join(git_dir, fh.read().strip()))
    except OSError:
        return git_dir


def _pack_version(pkg_dir: str = _PKG_DIR) -> str:
    """What the HUD shows as its version: pyproject's, with the git commit when
    this copy is a checkout ("1.1.0 · c417464"), so an old copy is told from a
    new one at a glance (operator, 2026-10-30, after an update left the 5080 on
    the old page with nothing on screen to say so)."""
    version = "?"
    try:
        with open(os.path.join(pkg_dir, "pyproject.toml"), "r", encoding="utf-8") as fh:
            found = re.search(r'(?m)^version\s*=\s*"([^"]+)"', fh.read())
        if found:
            version = found.group(1)
    except (OSError, UnicodeDecodeError):
        pass
    commit = _git_commit(pkg_dir)
    return f"{version} · {commit}" if commit else version


def _read_html_file() -> str:
    with open(_HTML_PATH, "r", encoding="utf-8") as fh:
        text = fh.read()
    return text.replace(_VERSION_TOKEN, html.escape(_pack_version()))


def _count_logblaster_observers(prompt_server: Any) -> int:
    try:
        sockets = getattr(prompt_server, "sockets", None)
        if not isinstance(sockets, dict):
            return 0
        return sum(
            1
            for k in list(sockets.keys())
            if isinstance(k, str) and k.startswith("logblaster-")
        )
    except Exception:
        return 0


def _install_observer_tap(prompt_server: Any) -> None:
    """Forward directed workflow execution events to connected logblaster-* observer sockets."""
    if getattr(prompt_server, "_logblaster_send_sync_wrapped", False):
        return
    orig_send_sync = getattr(prompt_server, "send_sync", None)
    if not callable(orig_send_sync):
        return

    def _wrapped_send_sync(event: Any, data: Any, sid: Any = None) -> Any:
        result = orig_send_sync(event, data, sid)
        try:
            if sid is not None and event in _OBSERVED_EXECUTION_EVENTS:
                sockets = getattr(prompt_server, "sockets", None)
                if isinstance(sockets, dict) and sockets:
                    for lb_sid in list(sockets.keys()):
                        if (
                            isinstance(lb_sid, str)
                            and lb_sid.startswith("logblaster-")
                            and lb_sid != sid
                        ):
                            try:
                                orig_send_sync(event, data, lb_sid)
                            except Exception:
                                pass
        except Exception:
            pass
        return result

    setattr(prompt_server, "send_sync", _wrapped_send_sync)
    setattr(prompt_server, "_logblaster_send_sync_wrapped", True)


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
            html_text = await asyncio.to_thread(_read_html_file)
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
        payload = await asyncio.to_thread(get_gpu_telemetry)
        payload["observers"] = _count_logblaster_observers(prompt_server)
        payload["version"] = await asyncio.to_thread(_pack_version)
        return web.json_response(payload, headers=_SAFE_HEADERS)

    _install_observer_tap(prompt_server)
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
                        "(auto-popup extension + execution observer loaded)"
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
