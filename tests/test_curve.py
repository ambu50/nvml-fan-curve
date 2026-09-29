import importlib.util
import ctypes
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / "nvml-fan-curve.py"
spec = importlib.util.spec_from_file_location("fan_curve", path)
fan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fan)


class FakeGpu:
    def __init__(self, index, uuid, fans, temp, minimum=33, maximum=100):
        self.index, self.uuid, self.fans = index, uuid, range(fans)
        self.minimum, self.maximum = minimum, maximum
        self.temperature = temp
        self.policies = [fan.AUTO] * fans
        self.targets = [0] * fans
        self.writes = []

    def temp(self):
        return self.temperature

    def policy(self, index):
        return self.policies[index]

    def target(self, index):
        return self.targets[index]

    def set(self, index, percent):
        self.policies[index] = fan.MANUAL
        self.targets[index] = percent
        self.writes.append((index, percent))


class CurveTests(unittest.TestCase):
    def setUp(self):
        self.points = fan.curve_points("55:40 60:50 65:60 70:72 75:85 80:100")

    def test_curve_boundary_and_validation(self):
        self.assertEqual([fan.curve_speed(self.points, t) for t in (30, 55, 59, 60, 79, 80)],
                         [40, 40, 40, 50, 85, 100])
        for invalid in ("", "55:40 55:50", "75:101", "x:50", "55:50:60"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                fan.curve_points(invalid)

    def test_single_gpu_stays_at_target_and_repairs_external_manual_change(self):
        gpu = FakeGpu(0, "GPU-A", 1, 54)
        state = {}
        fan.tick([gpu], self.points, 4, state)
        self.assertEqual(gpu.writes, [(0, 40)])
        fan.tick([gpu], self.points, 4, state)
        self.assertEqual(len(gpu.writes), 1)
        gpu.targets[0] = 80  # a second client changed the manual target
        fan.tick([gpu], self.points, 4, state)
        self.assertEqual(gpu.writes[-1], (0, 40))
        gpu.policies[0] = fan.AUTO
        fan.tick([gpu], self.points, 4, state)
        self.assertEqual(gpu.policies[0], fan.MANUAL)

    def test_multiple_gpus_and_fans_and_different_allowed_ranges(self):
        a = FakeGpu(0, "GPU-A", 1, 60)
        b = FakeGpu(1, "GPU-B", 2, 82, minimum=45, maximum=90)
        state = {}
        fan.tick([a, b], self.points, 4, state)
        self.assertEqual(a.writes, [(0, 50)])
        self.assertEqual(b.writes, [(0, 90), (1, 90)])
        b.targets[1] = 70
        fan.tick([a, b], self.points, 4, state)
        self.assertEqual(b.writes[-1], (1, 90))
        self.assertEqual(len(a.writes), 1)

    def test_deadband_keeps_held_target_when_external_edit_is_repaired(self):
        gpu = FakeGpu(0, "GPU-A", 1, 55)
        points = fan.curve_points("55:40 60:42 65:50")
        state = {}
        fan.tick([gpu], points, 4, state)
        gpu.temperature = 60
        fan.tick([gpu], points, 4, state)
        self.assertEqual(gpu.writes, [(0, 40)])
        gpu.targets[0] = 80
        fan.tick([gpu], points, 4, state)
        fan.tick([gpu], points, 4, state)
        self.assertEqual(gpu.writes, [(0, 40), (0, 40)])
        gpu.temperature = 65
        fan.tick([gpu], points, 4, state)
        self.assertEqual(gpu.writes[-1], (0, 50))


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.nvml = object.__new__(fan.NVML)

        def read(name, *args):
            if name == "nvmlDeviceGetCount_v2":
                return 3
            if name == "nvmlDeviceGetNumFans":
                return {1: 1, 2: 0, 3: 2}[args[0].value]
            raise AssertionError(name)

        def call(name, *args):
            if name == "nvmlDeviceGetHandleByIndex_v2":
                args[1]._obj.value = args[0] + 1
            elif name == "nvmlDeviceGetUUID":
                ctypes.memmove(args[1], f"GPU-{args[0].value}".encode() + b"\0", 6)
            elif name == "nvmlDeviceGetMinMaxFanSpeed":
                args[1]._obj.value, args[2]._obj.value = 33, 100
            else:
                raise AssertionError(name)

        self.nvml.read = read
        self.nvml.call = call

    def test_all_skips_fanless_and_keeps_one_and_multiple_fans(self):
        devices = self.nvml.devices()
        self.assertEqual([(gpu.index, len(gpu.fans)) for gpu in devices], [(0, 1), (2, 2)])

    def test_select_uuid_or_index_and_reject_missing(self):
        self.assertEqual([gpu.index for gpu in self.nvml.devices("GPU-3")], [2])
        self.assertEqual([gpu.index for gpu in self.nvml.devices("0,2")], [0, 2])
        with self.assertRaises(ValueError):
            self.nvml.devices("GPU-missing")
        with self.assertRaises(RuntimeError):
            self.nvml.devices("1")


if __name__ == "__main__":
    unittest.main()
