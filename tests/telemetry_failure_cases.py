"""NVML fault-injection cases, also collected by the main regression suite."""

from __future__ import annotations

import importlib.util
from pathlib import Path


class _Function:
    def __init__(self, fn):
        self.fn = fn

    def __call__(self, *args):
        return self.fn(*args)


class _NvmlStub:
    def __init__(self):
        self.init_count = 0
        self.shutdown_count = 0
        self.init_rc = 0
        self.handle_rc = 0
        self.util_rc = 0
        self.memory_rc = 0
        self.uuid_rc = 0
        self.indices = []
        self.uuids = []
        self.nvmlInit_v2 = _Function(self._init)
        self.nvmlShutdown = _Function(self._shutdown)
        self.nvmlDeviceGetHandleByIndex_v2 = _Function(self._handle)
        self.nvmlDeviceGetHandleByUUID = _Function(self._uuid_handle)
        self.nvmlDeviceGetIndex = _Function(self._index)
        self.nvmlDeviceGetName = _Function(self._name)
        self.nvmlDeviceGetUtilizationRates = _Function(self._util)
        self.nvmlDeviceGetMemoryInfo = _Function(self._memory)
        # Optional sensors may be unsupported without hiding useful core metrics.
        self.nvmlDeviceGetTemperature = _Function(lambda *args: 3)
        self.nvmlDeviceGetPowerUsage = _Function(lambda *args: 3)

    def _init(self):
        self.init_count += 1
        return self.init_rc

    def _shutdown(self):
        self.shutdown_count += 1
        return 0

    def _handle(self, index, pointer):
        self.indices.append(index.value)
        pointer._obj.value = 1
        return self.handle_rc

    def _name(self, handle, buffer, size):
        buffer.value = b"Test GPU"
        return 0

    def _uuid_handle(self, uuid, pointer):
        self.uuids.append(uuid)
        pointer._obj.value = 3
        return self.uuid_rc

    def _index(self, handle, pointer):
        pointer._obj.value = 3
        return 0

    def _util(self, handle, pointer):
        pointer._obj.gpu = 42
        pointer._obj.memory = 17
        return self.util_rc

    def _memory(self, handle, pointer):
        pointer._obj.total = 8 * 1024**3
        pointer._obj.used = 2 * 1024**3
        pointer._obj.free = 6 * 1024**3
        return self.memory_rc


def _reader(monkeypatch, env=None):
    path = Path(__file__).resolve().parents[1] / "telemetry.py"
    spec = importlib.util.spec_from_file_location("lb_fault_telemetry", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    lib = _NvmlStub()
    clock = [100.0]
    monkeypatch.setattr(module.ctypes, "CDLL", lambda name: lib)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    return module.NvmlReader(cache_ttl_sec=0, env=env or {}), lib, clock


def test_nvml_metric_failure_and_recovery(monkeypatch):
    for failed_metric in ("util_rc", "memory_rc"):
        reader, lib, clock = _reader(monkeypatch)
        assert reader.snapshot()["gpu_util"] == 42
        setattr(lib, failed_metric, 15)  # NVML_ERROR_GPU_IS_LOST
        failed = reader.snapshot()
        assert failed["available"] is False and failed["ok"] is False
        assert failed["metrics_mode"] == "unavailable"
        assert lib.shutdown_count == 1
        clock[0] += 29
        assert reader.snapshot()["available"] is False
        assert lib.init_count == 1
        setattr(lib, failed_metric, 0)
        clock[0] += 1
        recovered = reader.snapshot()
        assert recovered["available"] is True
        assert recovered["vram_total_mib"] == 8192
        assert recovered["vram_used_mib"] == 2048
        assert lib.init_count == 2
        reader.close()
        reader.close()
        assert lib.shutdown_count == 2


def test_nvml_transient_init_failure_retries(monkeypatch):
    reader, lib, clock = _reader(monkeypatch)
    lib.init_rc = 9
    assert reader.snapshot()["available"] is False
    assert lib.shutdown_count == 0  # Failed initialization acquired no reference.
    lib.init_rc = 0
    assert reader.snapshot()["available"] is False
    assert lib.init_count == 1
    clock[0] += 30
    assert reader.snapshot()["available"] is True
    reader.close()
    assert lib.init_count == 2 and lib.shutdown_count == 1


def test_nvml_explicit_target_never_falls_back(monkeypatch):
    reader, lib, clock = _reader(monkeypatch, {"LOGBLASTER_NVML_INDEX": "9"})
    lib.handle_rc = 6
    assert reader.snapshot()["available"] is False
    clock[0] += 30
    assert reader.snapshot()["available"] is False
    assert lib.indices == [9, 9]
    assert lib.shutdown_count == lib.init_count == 2

    invalid, invalid_lib, invalid_clock = _reader(
        monkeypatch, {"LOGBLASTER_NVML_INDEX": "invalid"}
    )
    assert invalid.snapshot()["available"] is False
    invalid_clock[0] += 60
    assert invalid.snapshot()["available"] is False
    assert invalid_lib.init_count == 0

    for key in ("LOGBLASTER_NVML_INDEX", "CUDA_VISIBLE_DEVICES"):
        uuid_reader, uuid_lib, uuid_clock = _reader(monkeypatch, {key: "GPU-test"})
        uuid_lib.uuid_rc = 6
        assert uuid_reader.snapshot()["available"] is False
        assert uuid_lib.indices == []
        assert uuid_lib.uuids == [b"GPU-test"]
        assert uuid_lib.shutdown_count == 1
        uuid_lib.uuid_rc = 0
        uuid_clock[0] += 30
        recovered = uuid_reader.snapshot()
        assert recovered["available"] is True and recovered["device_index"] == 3
        assert uuid_lib.indices == []
        uuid_reader.close()
        assert uuid_lib.shutdown_count == 2
