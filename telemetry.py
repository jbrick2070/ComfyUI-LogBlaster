"""Zero-subprocess, zero-CUDA-context GPU telemetry via NVML ctypes.

Reads live GPU utilization, VRAM usage, temperature, and power draw directly
from ``nvml.dll`` / ``libnvidia-ml.so.1`` in ~0.03 ms without importing
``torch``, touching PyTorch's CUDA allocator, or spawning ``nvidia-smi``.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from typing import Any, Dict, Optional


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


class NvmlReader:
    """Thread-safe lazy NVML reader with short TTL caching."""

    def __init__(self, cache_ttl_sec: float = 0.4) -> None:
        self._lock = threading.Lock()
        self._cache_ttl = cache_ttl_sec
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
            if init_fn() != 0:
                self._failed = True
                return False

            get_handle = (
                getattr(lib, "nvmlDeviceGetHandleByIndex_v2", None)
                or getattr(lib, "nvmlDeviceGetHandleByIndex")
            )
            handle = ctypes.c_void_p()
            if get_handle(ctypes.c_uint(0), ctypes.byref(handle)) != 0:
                self._failed = True
                return False

            name_buf = ctypes.create_string_buffer(96)
            if lib.nvmlDeviceGetName(handle, name_buf, ctypes.c_uint(96)) == 0:
                self._gpu_name = name_buf.value.decode("utf-8", errors="replace").strip()

            self._lib = lib
            self._handle = handle
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
