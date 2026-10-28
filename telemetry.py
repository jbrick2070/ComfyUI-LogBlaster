"""Zero-subprocess, zero-CUDA-context GPU telemetry via NVML ctypes.

Reads live GPU utilization, VRAM usage, temperature, and power draw directly
from ``nvml.dll`` / ``libnvidia-ml.so.1`` in ~0.03 ms without importing
``torch``, touching PyTorch's CUDA allocator, or spawning ``nvidia-smi``.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
import threading
import time
from typing import Any, Dict, Mapping, Optional, Tuple


class _NvmlUtilization(ctypes.Structure):
    _fields_ = [
        ("gpu", ctypes.c_uint),
        ("memory", ctypes.c_uint),
    ]


class _NvmlMemory(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


def resolve_nvml_target(env: Optional[Mapping[str, str]] = None) -> Tuple[str, Any]:
    """Classify target NVML GPU selection from LOGBLASTER_NVML_INDEX or CUDA_VISIBLE_DEVICES."""
    src = env if env is not None else os.environ
    explicit = src.get("LOGBLASTER_NVML_INDEX", "").strip()
    if explicit:
        if re.fullmatch(r"\d+", explicit):
            return ("explicit_index", int(explicit))
        if re.match(r"^(?:GPU|MIG)-", explicit, re.IGNORECASE):
            return ("explicit_uuid", explicit)
        return ("invalid", -1)

    cuda_vis = src.get("CUDA_VISIBLE_DEVICES", "").strip()
    if cuda_vis:
        first_token = cuda_vis.split(",")[0].strip()
        if re.fullmatch(r"\d+", first_token):
            return ("cuda_index", int(first_token))
        if re.match(r"^(?:GPU|MIG)-", first_token, re.IGNORECASE):
            return ("cuda_uuid", first_token)
        if first_token.lower() in {"-1", "none", "void"}:
            return ("disabled", -1)

    return ("default_index", 0)


def resolve_nvml_device_index(env: Optional[Mapping[str, str]] = None) -> int:
    """Resolve integer NVML GPU index (or -1 when explicitly invalid/disabled)."""
    mode, target = resolve_nvml_target(env)
    if mode in ("explicit_index", "cuda_index", "default_index"):
        return int(target)
    if mode in ("invalid", "disabled"):
        return -1
    return 0


class NvmlReader:
    """Thread-safe lazy NVML reader with short TTL caching."""

    def __init__(
        self,
        cache_ttl_sec: float = 0.4,
        device_index: Optional[int] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._lock = threading.Lock()
        self._cache_ttl = cache_ttl_sec
        self._device_index = device_index
        self._env = env
        self._resolved_index: Optional[int] = None
        self._last_ts = 0.0
        self._last_payload: Optional[Dict[str, Any]] = None
        self._initialized = False
        self._failed = False
        self._lib: Any = None
        self._handle = ctypes.c_void_p()
        self._gpu_name = "GPU"

    def _ensure_init(self) -> bool:
        if self._initialized:
            return True
        if self._failed:
            return False

        if self._device_index is not None:
            if self._device_index < 0:
                self._failed = True
                return False
            mode, target = ("explicit_index", int(self._device_index))
        else:
            mode, target = resolve_nvml_target(self._env)
            if mode in ("invalid", "disabled"):
                self._failed = True
                return False

        candidates = (
            ["nvml.dll", r"C:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll"]
            if sys.platform == "win32"
            else ["libnvidia-ml.so.1", "libnvidia-ml.so"]
        )

        lib = None
        for name in candidates:
            try:
                lib = ctypes.CDLL(name)
                break
            except OSError:
                continue

        if lib is None:
            self._failed = True
            return False

        try:
            init_fn = getattr(lib, "nvmlInit_v2", None) or getattr(lib, "nvmlInit")
            init_fn.restype = ctypes.c_int
            if init_fn() != 0:
                self._failed = True
                return False

            get_handle = (
                getattr(lib, "nvmlDeviceGetHandleByIndex_v2", None)
                or getattr(lib, "nvmlDeviceGetHandleByIndex")
            )
            get_handle.restype = ctypes.c_int

            handle = ctypes.c_void_p()
            resolved_idx: Optional[int] = None

            if mode in ("explicit_uuid", "cuda_uuid"):
                get_uuid_fn = getattr(lib, "nvmlDeviceGetHandleByUUID", None)
                rc = -1
                if get_uuid_fn is not None:
                    get_uuid_fn.restype = ctypes.c_int
                    rc = get_uuid_fn(str(target).encode("utf-8"), ctypes.byref(handle))
                if rc != 0:
                    if mode == "explicit_uuid":
                        self._failed = True
                        return False
                    handle = ctypes.c_void_p()
                    rc = get_handle(ctypes.c_uint(0), ctypes.byref(handle))
                    if rc == 0:
                        resolved_idx = 0
                if rc != 0:
                    self._failed = True
                    return False
                get_idx_fn = getattr(lib, "nvmlDeviceGetIndex", None)
                if resolved_idx is None and get_idx_fn is not None:
                    get_idx_fn.restype = ctypes.c_int
                    idx_out = ctypes.c_uint(0)
                    if get_idx_fn(handle, ctypes.byref(idx_out)) == 0:
                        resolved_idx = int(idx_out.value)
            else:
                target_idx = int(target)
                rc = get_handle(ctypes.c_uint(target_idx), ctypes.byref(handle))
                if rc == 0:
                    resolved_idx = target_idx
                elif mode != "explicit_index" and target_idx != 0:
                    # Containerized CUDA_VISIBLE_DEVICES may remap visible device to NVML index 0;
                    # explicit LOGBLASTER_NVML_INDEX never silently falls back to a different GPU.
                    handle = ctypes.c_void_p()
                    rc = get_handle(ctypes.c_uint(0), ctypes.byref(handle))
                    if rc == 0:
                        resolved_idx = 0
                if rc != 0:
                    self._failed = True
                    return False

            lib.nvmlDeviceGetName.restype = ctypes.c_int
            lib.nvmlDeviceGetUtilizationRates.restype = ctypes.c_int
            lib.nvmlDeviceGetMemoryInfo.restype = ctypes.c_int
            lib.nvmlDeviceGetTemperature.restype = ctypes.c_int
            lib.nvmlDeviceGetPowerUsage.restype = ctypes.c_int

            name_buf = ctypes.create_string_buffer(96)
            if lib.nvmlDeviceGetName(handle, name_buf, ctypes.c_uint(96)) == 0:
                self._gpu_name = name_buf.value.decode("utf-8", errors="replace").strip()

            self._lib = lib
            self._handle = handle
            self._resolved_index = resolved_idx if resolved_idx is not None else 0
            self._initialized = True
            return True
        except Exception:
            self._failed = True
            return False

    def snapshot(self) -> Dict[str, Any]:
        """Return a sanitized, path-free GPU telemetry snapshot."""
        now = time.monotonic()
        with self._lock:
            if (
                self._last_payload is not None
                and (now - self._last_ts) < self._cache_ttl
            ):
                return dict(self._last_payload)

            if not self._ensure_init():
                payload: Dict[str, Any] = {
                    "ok": False,
                    "available": False,
                    "metrics_mode": "unavailable",
                    "device_index": None,
                    "gpu_name": "Unavailable",
                    "gpu_util": 0,
                    "mem_util": 0,
                    "vram_used_mib": 0,
                    "vram_total_mib": 0,
                    "temp_c": 0,
                    "power_w": 0.0,
                }
                self._last_ts = now
                self._last_payload = payload
                return dict(payload)

            lib = self._lib
            handle = self._handle

            util = _NvmlUtilization()
            gpu_util = 0
            mem_util = 0
            if lib.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(util)) == 0:
                gpu_util = int(util.gpu)
                mem_util = int(util.memory)

            mem = _NvmlMemory()
            vram_used_mib = 0
            vram_total_mib = 0
            if lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem)) == 0:
                vram_used_mib = int(mem.used // (1024 * 1024))
                vram_total_mib = int(mem.total // (1024 * 1024))

            temp_c = ctypes.c_uint(0)
            temp_val = 0
            if (
                lib.nvmlDeviceGetTemperature(
                    handle, ctypes.c_uint(0), ctypes.byref(temp_c)
                )
                == 0
            ):
                temp_val = int(temp_c.value)

            power_mw = ctypes.c_uint(0)
            power_w = 0.0
            if lib.nvmlDeviceGetPowerUsage(handle, ctypes.byref(power_mw)) == 0:
                power_w = round(float(power_mw.value) / 1000.0, 1)

            payload = {
                "ok": True,
                "available": True,
                "metrics_mode": "nvml",
                "device_index": self._resolved_index,
                "gpu_name": self._gpu_name,
                "gpu_util": gpu_util,
                "mem_util": mem_util,
                "vram_used_mib": vram_used_mib,
                "vram_total_mib": vram_total_mib,
                "temp_c": temp_val,
                "power_w": power_w,
            }
            self._last_ts = now
            self._last_payload = payload
            return dict(payload)


_DEFAULT_READER = NvmlReader()


def get_gpu_telemetry() -> Dict[str, Any]:
    """Return cached live GPU telemetry without spawning any subprocess."""
    return _DEFAULT_READER.snapshot()
