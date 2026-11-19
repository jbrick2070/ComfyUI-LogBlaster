"""Zero-subprocess, zero-CUDA-context GPU telemetry via ctypes.

NVIDIA: reads live GPU utilization, VRAM usage, temperature, and power draw
directly from ``nvml.dll`` / ``libnvidia-ml.so.1`` in ~0.03 ms without
importing ``torch``, touching PyTorch's CUDA allocator, or spawning
``nvidia-smi``. Apple Silicon: reads GPU utilization and GPU memory in use
from IOKit's IOAccelerator statistics (``AppleGpuReader``), the same way.
"""

from __future__ import annotations

import atexit
import ctypes
from os import environ
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
    src = env if env is not None else environ
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
        self._retry_at = 0.0
        self._retry_delay_sec = 30.0
        self._lib: Any = None
        self._handle = ctypes.c_void_p()
        self._gpu_name = "GPU"

    def _release_nvml(self) -> None:
        """Balance our successful nvmlInit call before discarding its handle."""
        lib = self._lib
        self._lib = None
        self._initialized = False
        self._handle = ctypes.c_void_p()
        self._resolved_index = None
        self._gpu_name = "GPU"
        if lib is not None:
            try:
                lib.nvmlShutdown.restype = ctypes.c_int
                lib.nvmlShutdown()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            self._release_nvml()
            self._last_payload = None

    def _defer_retry(self) -> None:
        self._release_nvml()
        self._retry_at = time.monotonic() + self._retry_delay_sec

    def _unavailable_snapshot(self, now: float) -> Dict[str, Any]:
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

    def _ensure_init(self) -> bool:
        if self._initialized:
            return True
        if self._failed or time.monotonic() < self._retry_at:
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
            self._defer_retry()
            return False

        try:
            init_fn = getattr(lib, "nvmlInit_v2", None) or getattr(lib, "nvmlInit")
            init_fn.restype = ctypes.c_int
            if init_fn() != 0:
                self._defer_retry()
                return False
            # Store only after successful init: every later failure must release it.
            self._lib = lib

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
                    # UUIDs identify physical/MIG devices, not remapped CUDA indices.
                    # Reporting GPU 0 here would silently report a different device.
                    self._defer_retry()
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
                    self._defer_retry()
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
            self._defer_retry()
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
                return self._unavailable_snapshot(now)

            lib = self._lib
            handle = self._handle

            util = _NvmlUtilization()
            mem = _NvmlMemory()
            try:
                util_rc = lib.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(util))
                mem_rc = lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem))
            except Exception:
                util_rc = mem_rc = -1
            if util_rc != 0 or mem_rc != 0 or mem.total == 0:
                # A lost device / stale handle is not a valid idle GPU reading.
                self._defer_retry()
                return self._unavailable_snapshot(now)
            gpu_util = int(util.gpu)
            mem_util = int(util.memory)
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


class AppleGpuReader:
    """Apple Silicon GPU telemetry through IOKit, still zero-subprocess.

    macOS publishes the GPU's own statistics on its IOAccelerator service
    (the same dictionary `ioreg -c IOAccelerator` prints), readable without
    root: ``Device Utilization %`` and ``In use system memory`` (bytes the GPU
    holds in unified memory). Memory is unified, so the total is the machine's
    RAM (``hw.memsize``). Temperature and power need root (powermetrics / the
    SMC), so they are flagged unavailable rather than shown as a reading.
    Read through ctypes like the NVML path: no ``ioreg`` process, no torch.
    """

    _KCF_UTF8 = 0x08000100      # kCFStringEncodingUTF8
    _KCF_SINT64 = 4             # kCFNumberSInt64Type

    def __init__(self, cache_ttl_sec: float = 0.4) -> None:
        self._lock = threading.Lock()
        self._cache_ttl = cache_ttl_sec
        self._last_ts = 0.0
        self._last_payload: Optional[Dict[str, Any]] = None
        self._iokit: Any = None
        self._cf: Any = None
        self._libc: Any = None
        self._failed = False
        self._retry_at = 0.0
        self._gpu_name = "Apple GPU"
        self._total_mib = 0

    # -- library setup -------------------------------------------------
    def _ensure_libs(self) -> bool:
        if self._iokit is not None:
            return True
        if self._failed and time.monotonic() < self._retry_at:
            return False
        try:
            iokit = ctypes.CDLL(
                "/System/Library/Frameworks/IOKit.framework/IOKit")
            cf = ctypes.CDLL(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            vp, u32 = ctypes.c_void_p, ctypes.c_uint32
            iokit.IOServiceMatching.restype = vp
            iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
            iokit.IOServiceGetMatchingServices.restype = ctypes.c_int
            iokit.IOServiceGetMatchingServices.argtypes = [u32, vp, ctypes.POINTER(u32)]
            iokit.IOIteratorNext.restype = u32
            iokit.IOIteratorNext.argtypes = [u32]
            iokit.IOObjectRelease.restype = ctypes.c_int
            iokit.IOObjectRelease.argtypes = [u32]
            iokit.IORegistryEntryCreateCFProperty.restype = vp
            iokit.IORegistryEntryCreateCFProperty.argtypes = [u32, vp, vp, u32]
            cf.CFStringCreateWithCString.restype = vp
            cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, u32]
            cf.CFDictionaryGetValue.restype = vp
            cf.CFDictionaryGetValue.argtypes = [vp, vp]
            cf.CFNumberGetValue.restype = ctypes.c_bool
            # CFNumberType is a CFIndex (signed long) on 64-bit macOS.
            cf.CFNumberGetValue.argtypes = [vp, ctypes.c_long, vp]
            cf.CFGetTypeID.restype = ctypes.c_ulong
            cf.CFGetTypeID.argtypes = [vp]
            cf.CFNumberGetTypeID.restype = ctypes.c_ulong
            cf.CFNumberGetTypeID.argtypes = []
            cf.CFDictionaryGetTypeID.restype = ctypes.c_ulong
            cf.CFDictionaryGetTypeID.argtypes = []
            cf.CFRelease.restype = None
            cf.CFRelease.argtypes = [vp]
            libc.sysctlbyname.restype = ctypes.c_int
            libc.sysctlbyname.argtypes = [ctypes.c_char_p, vp,
                                          ctypes.POINTER(ctypes.c_size_t), vp,
                                          ctypes.c_size_t]
            self._iokit, self._cf, self._libc = iokit, cf, libc
            self._total_mib = self._sysctl_u64(b"hw.memsize") // (1024 * 1024)
            if self._total_mib <= 0:
                # An incomplete start is a failed start: retried after the
                # delay below, never cached as "ready" with no memory total
                # (Codex review 2026-11-19).
                raise RuntimeError("hw.memsize unreadable")
            brand = self._sysctl_str(b"machdep.cpu.brand_string")
            # Apple Silicon: the GPU is part of the named chip. An Intel Mac's
            # brand string names its CPU, which says nothing about its GPU.
            self._gpu_name = ("%s GPU" % brand) if brand.startswith("Apple") else "Mac GPU"
            return True
        except Exception:
            self._iokit = self._cf = self._libc = None
            self._failed = True
            self._retry_at = time.monotonic() + 30.0
            return False

    def _sysctl_u64(self, name: bytes) -> int:
        out = ctypes.c_uint64(0)
        size = ctypes.c_size_t(ctypes.sizeof(out))
        if self._libc.sysctlbyname(name, ctypes.byref(out), ctypes.byref(size), None, 0) != 0:
            return 0
        return int(out.value)

    def _sysctl_str(self, name: bytes) -> str:
        size = ctypes.c_size_t(0)
        if self._libc.sysctlbyname(name, None, ctypes.byref(size), None, 0) != 0 or not size.value:
            return ""
        buf = ctypes.create_string_buffer(size.value)
        if self._libc.sysctlbyname(name, buf, ctypes.byref(size), None, 0) != 0:
            return ""
        return buf.value.decode("utf-8", errors="replace").strip()

    def _cfstr(self, text: str) -> Any:
        return self._cf.CFStringCreateWithCString(None, text.encode("utf-8"), self._KCF_UTF8)

    def _int_of(self, ref: Any) -> Optional[int]:
        """The integer a CFNumber holds, or None for anything else."""
        if not ref or self._cf.CFGetTypeID(ref) != self._cf.CFNumberGetTypeID():
            return None
        out = ctypes.c_int64(0)
        if not self._cf.CFNumberGetValue(ref, self._KCF_SINT64, ctypes.byref(out)):
            return None
        return int(out.value)

    def _dict_int(self, mapping: Any, key: str) -> Optional[int]:
        cf_key = self._cfstr(key)
        try:
            return self._int_of(self._cf.CFDictionaryGetValue(mapping, cf_key))
        finally:
            self._cf.CFRelease(cf_key)

    def _property(self, service: int, key: str) -> Any:
        """A +1 retained registry property (the caller releases it), or None."""
        cf_key = self._cfstr(key)
        try:
            return self._iokit.IORegistryEntryCreateCFProperty(service, cf_key, None, 0)
        finally:
            self._cf.CFRelease(cf_key)

    def _read_stats(self) -> Optional[Dict[str, int]]:
        """Utilization %, in-use bytes and core count of the GPU doing the work,
        or None. Apple Silicon has one IOAccelerator; an Intel Mac can have two
        (integrated + discrete) in any enumeration order, so every adapter
        with statistics is read and the one holding the most memory -- the
        active renderer -- is reported, busier first on a tie (Codex review
        2026-11-19: taking the first one could report an idle adapter)."""
        found = self._read_all_adapters()
        if not found:
            return None
        best = max(found, key=lambda a: (a["in_use"], a["util"]))
        return dict(best, adapters=len(found))

    def _read_all_adapters(self) -> list:
        """Every IOAccelerator that publishes the two statistics, read once."""
        iokit, cf = self._iokit, self._cf
        matching = iokit.IOServiceMatching(b"IOAccelerator")  # consumed by the next call
        if not matching:
            return []
        iterator = ctypes.c_uint32(0)
        if iokit.IOServiceGetMatchingServices(0, matching, ctypes.byref(iterator)) != 0:
            return []
        found: list = []
        try:
            while True:
                service = iokit.IOIteratorNext(iterator.value)
                if not service:
                    return found
                try:
                    stats = self._property(service, "PerformanceStatistics")
                    if not stats:
                        continue
                    try:
                        if cf.CFGetTypeID(stats) != cf.CFDictionaryGetTypeID():
                            continue
                        util = self._dict_int(stats, "Device Utilization %")
                        in_use = self._dict_int(stats, "In use system memory")
                    finally:
                        cf.CFRelease(stats)
                    if util is None or in_use is None:
                        continue
                    cores = 0
                    cores_ref = self._property(service, "gpu-core-count")
                    if cores_ref:
                        try:
                            cores = self._int_of(cores_ref) or 0
                        finally:
                            cf.CFRelease(cores_ref)
                    found.append({"util": util, "in_use": in_use, "cores": cores})
                finally:
                    iokit.IOObjectRelease(service)
        finally:
            iokit.IOObjectRelease(iterator.value)

    def _unavailable(self, now: float) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "ok": False, "available": False, "metrics_mode": "unavailable",
            "device_index": None, "gpu_name": "Unavailable", "gpu_util": 0,
            "mem_util": 0, "vram_used_mib": 0, "vram_total_mib": 0,
            "temp_c": 0, "power_w": 0.0,
        }
        self._last_ts = now
        self._last_payload = payload
        return dict(payload)

    def snapshot(self) -> Dict[str, Any]:
        """Return a sanitized, path-free GPU telemetry snapshot."""
        now = time.monotonic()
        with self._lock:
            if self._last_payload is not None and (now - self._last_ts) < self._cache_ttl:
                return dict(self._last_payload)
            if not self._ensure_libs() or self._total_mib <= 0:
                return self._unavailable(now)
            try:
                stats = self._read_stats()
            except Exception:
                stats = None
            if stats is None:
                return self._unavailable(now)
            used_mib = max(0, int(stats["in_use"]) // (1024 * 1024))
            name = self._gpu_name
            if stats.get("adapters", 1) > 1:
                name = "%s (1 of %d)" % (name, stats["adapters"])
            elif stats["cores"] > 0:
                name = "%s (%d-core)" % (name, stats["cores"])
            payload = {
                "ok": True,
                "available": True,
                "metrics_mode": "apple_gpu",
                "device_index": 0,
                "gpu_name": name,
                "gpu_util": max(0, min(100, int(stats["util"]))),
                "mem_util": int(round(100.0 * used_mib / self._total_mib)),
                "vram_used_mib": used_mib,
                "vram_total_mib": int(self._total_mib),
                "temp_c": 0,          # root-only on macOS: not read
                "power_w": 0.0,       # root-only on macOS: not read
                "temp_available": False,
                "power_available": False,
            }
            self._last_ts = now
            self._last_payload = payload
            return dict(payload)

    def close(self) -> None:
        """Nothing is held open between reads; present for reader parity."""


def _default_reader() -> Any:
    """NVML everywhere it can exist; IOKit on macOS, where NVML never does."""
    if sys.platform == "darwin":
        return AppleGpuReader()
    return NvmlReader()


_DEFAULT_READER = _default_reader()
atexit.register(_DEFAULT_READER.close)


def get_gpu_telemetry() -> Dict[str, Any]:
    """Return cached live GPU telemetry without spawning any subprocess."""
    return _DEFAULT_READER.snapshot()
