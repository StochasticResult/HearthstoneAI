import unittest
from types import SimpleNamespace
from unittest.mock import patch

from resource_guard import MIB, memory_pressure_level, memory_stop_reason


class MemoryPolicyTests(unittest.TestCase):
    def test_stable_historical_swap_does_not_indicate_current_pressure(self):
        self.assertIsNone(memory_stop_reason(1031*MIB,1031*MIB,1,51))
        self.assertIsNone(memory_stop_reason(2048*MIB,2048*MIB,1,40))

    def test_growth_limit_survives_even_when_os_pressure_is_normal(self):
        self.assertIsNone(memory_stop_reason(1287*MIB,1031*MIB,1,51))
        self.assertEqual(memory_stop_reason(1288*MIB,1031*MIB,1,51),'swap growth threshold')
        self.assertIsNone(memory_stop_reason(900*MIB,1031*MIB,1,51))

    def test_warning_and_critical_pressure_stop_even_without_any_swap(self):
        self.assertEqual(memory_stop_reason(0,0,2,51),'system memory pressure warning')
        self.assertEqual(memory_stop_reason(0,0,4,51),'critical system memory pressure')

    def test_availability_and_explicit_development_cap_still_stop(self):
        self.assertEqual(memory_stop_reason(0,0,1,19),'system memory availability threshold')
        self.assertEqual(memory_stop_reason(1031*MIB,1031*MIB,1,51,maximum=1024*MIB),
                         'explicit swap total threshold')

    def test_unknown_status_is_never_assumed_healthy(self):
        for pressure, free in ((0,50),(3,50),(8,50),(True,50),(1,-1),(1,101),(1,None)):
            self.assertEqual(memory_stop_reason(0,0,pressure,free),'unknown system memory status')

    def test_reader_uses_exported_dispatch_flags_not_internal_kernel_enum(self):
        for level in (1,2,4):
            with patch('resource_guard.subprocess.run', return_value=SimpleNamespace(stdout=str(level))):
                self.assertEqual(memory_pressure_level(),level)
        for level in ('0','3','unknown'):
            with patch('resource_guard.subprocess.run', return_value=SimpleNamespace(stdout=level)):
                with self.assertRaises((RuntimeError,ValueError)):
                    memory_pressure_level()
