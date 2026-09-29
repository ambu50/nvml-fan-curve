#!/usr/bin/env python3
"""Temperature-driven NVIDIA fan control through NVML; no X server required.

Defaults to every detected GPU with a supported fan. Configure through the
systemd EnvironmentFile or environment variables (see README.md).
"""
import argparse
import ctypes as C
import os
import signal
import sys
import threading

AUTO = 0
MANUAL = 1
GPU_TEMPERATURE = 0


def curve_points(value):
    points = []
    for item in value.split():
        try:
            temp, speed = (int(part) for part in item.split(":"))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"invalid curve point {item!r}; expected temperature:percent") from exc
        if not 0 <= temp <= 150 or not 0 <= speed <= 100:
            raise ValueError(f"out-of-range curve point {item!r}")
        points.append((temp, speed))
    if not points or len({temp for temp, _ in points}) != len(points):
        raise ValueError("curve must contain points with unique temperatures")
    points.sort()
    return points


def curve_speed(points, temp):
    result = points[0][1]
    for threshold, percent in points:
        if temp < threshold:
            break
        result = percent
    return result


class NVML:
    def __init__(self):
        self.lib = C.CDLL("libnvidia-ml.so.1")
        self.lib.nvmlErrorString.argtypes = [C.c_int]
        self.lib.nvmlErrorString.restype = C.c_char_p
        self.bind("nvmlInit_v2")
        self.bind("nvmlShutdown")
        self.bind("nvmlDeviceGetCount_v2", C.POINTER(C.c_uint))
        self.bind("nvmlDeviceGetHandleByIndex_v2", C.c_uint, C.POINTER(C.c_void_p))
        self.bind("nvmlDeviceGetUUID", C.c_void_p, C.c_void_p, C.c_uint)
        self.bind("nvmlDeviceGetNumFans", C.c_void_p, C.POINTER(C.c_uint))
        self.bind("nvmlDeviceGetMinMaxFanSpeed", C.c_void_p, C.POINTER(C.c_uint), C.POINTER(C.c_uint))
        self.bind("nvmlDeviceGetTemperature", C.c_void_p, C.c_uint, C.POINTER(C.c_uint))
        self.bind("nvmlDeviceGetFanControlPolicy_v2", C.c_void_p, C.c_uint, C.POINTER(C.c_uint))
        self.bind("nvmlDeviceGetTargetFanSpeed", C.c_void_p, C.c_uint, C.POINTER(C.c_uint))
        self.bind("nvmlDeviceGetFanSpeed_v2", C.c_void_p, C.c_uint, C.POINTER(C.c_uint))
        self.bind("nvmlDeviceSetFanSpeed_v2", C.c_void_p, C.c_uint, C.c_uint)
        self.bind("nvmlDeviceSetDefaultFanSpeed_v2", C.c_void_p, C.c_uint)

    def bind(self, name, *args):
        fn = getattr(self.lib, name)
        fn.argtypes = args
        fn.restype = C.c_int

    def call(self, name, *args):
        code = getattr(self.lib, name)(*args)
        if code:
            raise RuntimeError(f"{name}: {self.lib.nvmlErrorString(code).decode()} (code {code})")

    def read(self, name, *args):
        value = C.c_uint()
        self.call(name, *args, C.byref(value))
        return value.value

    def devices(self, selection="all"):
        count = self.read("nvmlDeviceGetCount_v2")
        requested = None if selection == "all" else set(selection.replace(",", " ").split())
        found = set()
        devices = []
        for index in range(count):
            handle = C.c_void_p()
            self.call("nvmlDeviceGetHandleByIndex_v2", index, C.byref(handle))
            uuid = C.create_string_buffer(96)
            self.call("nvmlDeviceGetUUID", handle, uuid, len(uuid))
            identifier = uuid.value.decode()
            if requested is not None and str(index) not in requested and identifier not in requested:
                continue
            found.update({str(index), identifier})
            try:
                fans = self.read("nvmlDeviceGetNumFans", handle)
                if fans == 0:
                    raise RuntimeError("no controllable fans")
                minimum, maximum = C.c_uint(), C.c_uint()
                self.call("nvmlDeviceGetMinMaxFanSpeed", handle, C.byref(minimum), C.byref(maximum))
                devices.append(Device(self, index, identifier, handle, fans, minimum.value, maximum.value))
            except RuntimeError as exc:
                if requested is not None:
                    raise RuntimeError(f"GPU {index} ({identifier}): {exc}") from exc
                print(f"skipping GPU {index} ({identifier}): {exc}", file=sys.stderr, flush=True)
        if requested is not None and requested - found:
            raise ValueError(f"GPUS entries not found: {', '.join(sorted(requested - found))}")
        if not devices:
            raise RuntimeError("no GPUs with NVML-controllable fans found")
        return devices


class Device:
    def __init__(self, nvml, index, uuid, handle, fans, minimum, maximum):
        self.nvml, self.index, self.uuid, self.handle = nvml, index, uuid, handle
        self.fans = range(fans)
        self.minimum, self.maximum = minimum, maximum

    def temp(self):
        return self.nvml.read("nvmlDeviceGetTemperature", self.handle, GPU_TEMPERATURE)

    def policy(self, fan):
        return self.nvml.read("nvmlDeviceGetFanControlPolicy_v2", self.handle, fan)

    def target(self, fan):
        return self.nvml.read("nvmlDeviceGetTargetFanSpeed", self.handle, fan)

    def speed(self, fan):
        return self.nvml.read("nvmlDeviceGetFanSpeed_v2", self.handle, fan)

    def set(self, fan, percent):
        self.nvml.call("nvmlDeviceSetFanSpeed_v2", self.handle, fan, percent)

    def restore(self):
        ok = True
        for fan in self.fans:
            try:
                self.nvml.call("nvmlDeviceSetDefaultFanSpeed_v2", self.handle, fan)
            except RuntimeError as exc:
                ok = False
                print(f"GPU {self.index} fan {fan} restore: {exc}", file=sys.stderr, flush=True)
        return ok


def tick(devices, points, deadband, previous):
    """Apply one sample. Read back every fan target to catch outside changes."""
    for gpu in devices:
        temp = gpu.temp()
        desired = curve_speed(points, temp)
        target = max(gpu.minimum, min(gpu.maximum, desired))
        old = previous.get(gpu.uuid)
        # Hold the previous curve target across changes below the deadband.
        # An external edit still gets corrected to that held target.
        if old is not None and abs(target - old) < deadband:
            target = old
        for fan in gpu.fans:
            observed = gpu.target(fan) if gpu.policy(fan) == MANUAL else None
            if observed != target:
                gpu.set(fan, target)
                print(f"GPU {gpu.index} fan {fan}: {temp}C -> {target}%", flush=True)
        previous[gpu.uuid] = target


def daemon(devices, points, poll, deadband):
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    previous = {}
    failures = 0
    try:
        while not stopping.is_set():
            try:
                tick(devices, points, deadband, previous)
                failures = 0
            except RuntimeError as exc:
                failures += 1
                print(f"NVML error ({failures}/12): {exc}", file=sys.stderr, flush=True)
                if failures >= 12:
                    raise
            stopping.wait(poll)
    finally:
        for gpu in devices:
            gpu.restore()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--status", action="store_true", help="show every selected GPU's fan state")
    action.add_argument("--check", action="store_true", help="exit nonzero if fans are not in manual mode")
    action.add_argument("--restore", action="store_true", help="restore NVIDIA automatic fan policy")
    action.add_argument("--set", metavar="PERCENT", type=int, help="set all selected fans to a fixed speed")
    args = parser.parse_args()
    selection = os.getenv("GPUS", "all").strip()
    if not selection:
        parser.error("GPUS must be 'all', indexes, or GPU UUIDs")
    points = curve_points(os.getenv("CURVE", "55:40 60:50 65:60 70:72 75:85 80:100"))
    poll = float(os.getenv("POLL", "5"))
    deadband = int(os.getenv("DEADBAND", "4"))
    if poll <= 0 or deadband < 0:
        parser.error("POLL must be positive and DEADBAND nonnegative")
    if args.set is not None and not 0 <= args.set <= 100:
        parser.error("--set must be between 0 and 100")

    nvml = NVML()
    nvml.call("nvmlInit_v2")
    try:
        devices = nvml.devices(selection)
        for gpu in devices:
            print(f"GPU {gpu.index} {gpu.uuid}: {len(gpu.fans)} fan(s), range {gpu.minimum}-{gpu.maximum}%", flush=True)
        if args.status or args.check:
            ok = True
            for gpu in devices:
                for fan in gpu.fans:
                    policy = gpu.policy(fan)
                    if args.status:
                        print(f"GPU {gpu.index} fan {fan}: {gpu.temp()}C policy={'manual' if policy == MANUAL else 'auto'} "
                              f"target={gpu.target(fan)}% reported={gpu.speed(fan)}%")
                    ok &= policy == MANUAL
            return 0 if ok or args.status else 1
        if args.restore:
            results = [gpu.restore() for gpu in devices]
            return 0 if all(results) else 1
        if args.set is not None:
            for gpu in devices:
                for fan in gpu.fans:
                    gpu.set(fan, max(gpu.minimum, min(gpu.maximum, args.set)))
            return 0
        print(f"curve={points}, poll={poll}s, deadband={deadband}%", flush=True)
        daemon(devices, points, poll, deadband)
        return 0
    finally:
        nvml.call("nvmlShutdown")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"nvml-fan-curve: {exc}", file=sys.stderr)
        sys.exit(1)
