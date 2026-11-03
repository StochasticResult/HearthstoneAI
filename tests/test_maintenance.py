import contextlib
import io
import itertools
import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from maintenance import cleanup, readiness, swapout_bytes
from resource_guard import DiagnosticOutput, GIB, MIB


@contextlib.contextmanager
def boundary_readings(*, pressure=1, available=55, disk=12*GIB, thermal=0, ac=True):
    with patch.multiple('maintenance', battery_status=lambda: (ac, 80),
                        memory_pressure_level=lambda: pressure,
                        memory_free_percentage=lambda: available,
                        swap_used=lambda: 2042.4*MIB, thermal_state=lambda: thermal), \
         patch('maintenance.shutil.disk_usage', return_value=SimpleNamespace(free=disk)):
        yield


class MaintenanceTests(unittest.TestCase):
    def test_cleanup_only_prunes_owned_old_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp);root=data/'runs';root.mkdir()
            for n in range(9):
                (root/('run-%02d'%n)).mkdir()
            outside=data/'unrelated';outside.mkdir();(outside/'keep').write_text('keep')
            (root/'run-linked').symlink_to(outside,target_is_directory=True)
            (root/'other-files').mkdir()
            self.assertEqual(cleanup(data)['old_runs_removed'],4)
            self.assertEqual(len([p for p in root.glob('run-*') if not p.is_symlink()]),5)
            self.assertEqual((outside/'keep').read_text(),'keep')
            self.assertTrue((root/'other-files').exists())

    def test_observed_healthy_mac_queues_with_original_baseline_and_resident_swap(self):
        with boundary_readings(), patch('maintenance.swapout_bytes') as activity:
            sample = readiness(1363.7*MIB)
        self.assertTrue(sample['ready'])
        self.assertEqual(sample['swap_growth_mib'], 678.7)
        activity.assert_not_called()

    def test_warning_waits_for_recent_heavy_page_writes_then_recovers_at_same_swap_total(self):
        with boundary_readings(pressure=2), \
             patch('maintenance.swapout_bytes', side_effect=[3*GIB, 3*GIB+128*MIB, 3*GIB+129*MIB]), \
             patch('maintenance.time.monotonic', side_effect=[100,115,130]):
            first = readiness(1363.7*MIB)
            busy = readiness(1363.7*MIB, first)
            recovered = readiness(1363.7*MIB, busy)
        self.assertEqual(first['reasons'], ['observing current paging'])
        self.assertEqual(busy['reasons'], ['active paging under memory pressure'])
        self.assertEqual(busy['swapout_mib_per_minute'], 512)
        self.assertTrue(recovered['ready'])
        self.assertEqual(recovered['swapout_mib_per_minute'], 4)
        self.assertEqual(recovered['swap_growth_mib'], first['swap_growth_mib'])

    def test_ranked_admission_uses_pressure_and_recent_paging_with_small_headroom(self):
        with boundary_readings(pressure=1,available=15), patch('maintenance.swapout_bytes') as activity:
            self.assertTrue(readiness(0,ranked=True)['ready'])
            self.assertFalse(readiness(0)['ready'])
            activity.assert_not_called()
        with boundary_readings(pressure=2,available=15), \
             patch('maintenance.swapout_bytes',side_effect=[3*GIB,3*GIB,3*GIB+128*MIB,3*GIB+256*MIB]), \
             patch('maintenance.time.monotonic',side_effect=[100,115,130,145]):
            start=readiness(0,ranked=True)
            quiet=readiness(0,start,ranked=True)
            busy=readiness(0,quiet,ranked=True)
            still_busy=readiness(0,busy,ranked=True)
        self.assertTrue(quiet['ready'])
        self.assertEqual(busy['reasons'],['active paging under memory pressure'])
        self.assertNotIn('sustained_paging_mib_per_minute',busy)
        self.assertEqual(still_busy['swapout_mib_per_minute'],512)
        self.assertEqual(still_busy['sustained_paging_mib_per_minute'],[512,512])
        for conditions in ({'available':9},{'pressure':4},{'pressure':0}):
            with self.subTest(conditions=conditions), boundary_readings(**conditions):
                self.assertFalse(readiness(0,ranked=True)['ready'])

    def test_missing_reset_or_stale_page_counters_wait_for_fresh_evidence(self):
        with boundary_readings(pressure=2), \
             patch('maintenance.swapout_bytes', side_effect=[ValueError(), 3*GIB, 2*GIB, 2*GIB, 2*GIB]), \
             patch('maintenance.time.monotonic', side_effect=[15,30,180,195]):
            sample = readiness(1363.7*MIB)
            self.assertEqual(sample['reasons'], ['swapout reading unavailable'])
            for _ in range(3):
                sample = readiness(1363.7*MIB, sample)
                self.assertEqual(sample['reasons'], ['observing current paging'])
            self.assertTrue(readiness(1363.7*MIB, sample)['ready'])

    def test_ranked_recycle_requires_two_measured_warning_intervals(self):
        previous = dict(swapout_bytes=3*GIB, paging_sampled_at=100,
                        memory_pressure_level=2, reasons=['active paging under memory pressure'])
        for sample in (previous, dict(previous, memory_pressure_level=1, swapout_mib_per_minute=512)):
            with self.subTest(sample=sample), boundary_readings(pressure=2), \
                 patch('maintenance.swapout_bytes', return_value=3*GIB+128*MIB), \
                 patch('maintenance.time.monotonic', return_value=115):
                current = readiness(0, sample, ranked=True)
            self.assertEqual(current['swapout_mib_per_minute'], 512)
            self.assertNotIn('sustained_paging_mib_per_minute', current)

    def test_healthy_paging_never_overrides_resource_protections(self):
        for conditions in ({'pressure':4}, {'pressure':0}, {'available':19}, {'disk':9*GIB},
                           {'thermal':1}, {'ac':False}):
            with self.subTest(conditions=conditions), boundary_readings(**conditions):
                self.assertFalse(readiness(1363.7*MIB)['ready'])

    def test_vm_stat_uses_reported_page_size_and_only_swapouts(self):
        report = 'Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPageouts: 100.\nSwapouts: 1234.\n'
        with patch('maintenance.subprocess.run', return_value=SimpleNamespace(stdout=report)):
            self.assertEqual(swapout_bytes(), 1234*16384)
        with patch('maintenance.subprocess.run', return_value=SimpleNamespace(stdout=report.split('Swapouts')[0])):
            with self.assertRaises(ValueError):
                swapout_bytes()

    def test_output_cap_retains_start_and_terminal_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'actions.log';log=DiagnosticOutput(p,4096)
            try:
                log.write(b'mulligan: kept starting hand\n')
                for _ in range(100):
                    log.write(('有界日志数据\n'*20).encode())
                log.write(b'{"game_result":"WON"}\n')
            finally:
                log.close()
            self.assertTrue(p.read_text(errors='replace').startswith('mulligan:'))
            tail=p.with_name('actions-tail.log')
            self.assertIn('"WON"',tail.read_text(errors='replace'))
            self.assertLessEqual(p.stat().st_size+tail.stat().st_size,4096)
            from match_review import summarize
            self.assertEqual(summarize(Path(tmp))[0]['outcome'],'WON')

    def test_guard_does_not_kill_child_for_deferred_swap_or_output_limit(self):
        import resource_guard as guard
        with tempfile.TemporaryDirectory() as tmp:
            args=['guard','--log',str(Path(tmp)/'actions.log'),'--timeout','0',
                  '--max-output-mib','1','--defer-maintenance','--swap-baseline-mib','600','--',
                  '/usr/bin/python3','-c',
                  'import sys; sys.stdout.write("diagnostic\\n"*300000); print("TERMINAL_RESULT")']
            handlers={s:signal.getsignal(s) for s in (signal.SIGTERM,signal.SIGINT)}
            swaps=iter([1031*MIB])
            try:
                with patch('sys.argv',args), patch.object(guard,'battery_status',return_value=(True,80)), \
                     patch.object(guard,'swap_used',side_effect=lambda:next(swaps,1400*MIB)), \
                     patch.object(guard,'memory_pressure_level',return_value=2), \
                     patch.object(guard,'memory_free_percentage',return_value=40), \
                     patch.object(guard,'thermal_state',return_value=0), \
                     patch.object(guard.shutil,'disk_usage',return_value=SimpleNamespace(free=12*GIB)), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(guard.main(),0)
                self.assertIn('TERMINAL_RESULT',(Path(tmp)/'actions-tail.log').read_text())
                result=json.loads(output.getvalue().splitlines()[-1])['result']
                self.assertIsNone(result['reason'])
                self.assertGreater(result['output_bytes'],MIB)
            finally:
                for sig,handler in handlers.items():signal.signal(sig,handler)

    def test_compact_decision_totals_survive_capped_diagnostics(self):
        from openai_advisor import OpenAIAdvisor
        from match_review import summarize
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            a=OpenAIAdvisor({'api_key':'fake-testing-key-123456789'},audit_path=root/'decisions.jsonl')
            a.audit_limit=600
            a.record({'event':'mulligan_confirmed'})
            for n in range(300):
                a.decision_seq=n+1
                a.record({'event':'proposed','observation':{'game_type':'GT_RANKED','game_turn':'1'},
                          'elapsed_ms':n+1,'tokens':5,'model':'gpt-6.1-sol'})
                a.record({'event':'confirmed'})
            a.record({'event':'game_complete','outcome':'WON'});a.close()
            result=summarize(root)[0]
            self.assertEqual(result['outcome'],'WON')
            self.assertEqual(result['api_responses'],300)
            self.assertEqual(result['api_tokens'],1500)
            self.assertEqual(result['confirmed_decisions'],300)
            self.assertEqual(result['unconfirmed_decisions'],[])
            self.assertTrue(result['mulligan_confirmed'])
            self.assertTrue(result['diagnostics_capped'])
            self.assertEqual(result['latency_sample_size'],256)
            self.assertLessEqual((root/'decisions.jsonl').stat().st_size,600)
            self.assertLess((root/'decision-summary.json').stat().st_size,8192)
            self.assertNotIn('fake-testing-key',(root/'decision-summary.json').read_text())

    def test_expired_proposal_is_separate_from_a_confirmed_or_ambiguous_move(self):
        from openai_advisor import OpenAIAdvisor
        from match_review import summarize
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            advisor=OpenAIAdvisor({'api_key':'fake-testing-key-123456789'},audit_path=root/'decisions.jsonl')
            for number in (1,2,3):
                advisor.decision_seq=number
                advisor.record({'event':'proposed','observation':{'game_type':'GT_RANKED','game_turn':'9'},
                                'action':{'kind':'attack'},'elapsed_ms':2800,'tokens':500})
                if number==1:advisor.record({'event':'confirmed'})
                if number==2:advisor.record({'event':'expired_without_submission','reason':'turn ended before submission'})
            advisor.close()
            for use_summary in (True,False):
                if not use_summary:(root/'decision-summary.json').unlink()
                summary,_=summarize(root)
                self.assertEqual(summary['confirmed_decisions'],1)
                self.assertEqual(summary['expired_decisions'],1)
                self.assertEqual(summary['unconfirmed_decisions'],[3])
                self.assertEqual(summary['api_responses'],3)

    def test_ranked_game_rss_spike_is_diagnostic_but_critical_pressure_still_stops(self):
        import game_watchdog as guard
        signals = (signal.SIGTERM,signal.SIGINT,signal.SIGHUP,signal.SIGUSR1,signal.SIGUSR2)
        old_handlers = {s:signal.getsignal(s) for s in signals}
        try:
            for critical in (False, True):
                pressure = iter([1, 4 if critical else 1, 1])
                # Model the recorded 1811 MiB spike. Only an explicit handback ends the healthy guard.
                argv = ['watchdog','--expected','/bin/sleep','--claim-pid','42424','--run-seconds','0',
                        '--parent-pid',str(os.getppid()),'--native-owner-pid',str(os.getppid())]
                def release(_):
                    signal.getsignal(signal.SIGUSR2)(signal.SIGUSR2,None)
                with self.subTest(critical=critical), patch('sys.argv',argv), \
                     patch.object(guard,'battery_status',return_value=(True,80)), \
                     patch.object(guard,'swap_used',return_value=900*MIB), \
                     patch.object(guard,'memory_pressure_level',side_effect=lambda:next(pressure,1)), \
                     patch.object(guard,'memory_free_percentage',return_value=47), \
                     patch.object(guard,'thermal_state',return_value=0), \
                     patch.object(guard.shutil,'disk_usage',return_value=SimpleNamespace(free=11*GIB)), \
                     patch.object(guard,'process_started',return_value=1), \
                     patch.object(guard,'processes',return_value=[(42424,1811*MIB,90)]), \
                     patch.object(guard.time,'sleep',side_effect=release), patch.object(guard,'stop_exact') as close, \
                     contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(guard.main(), 2 if critical else 0)
                records = [json.loads(l) for l in out.getvalue().splitlines()]
                self.assertFalse(any(r.get('recycle_after_game') for r in records))
                self.assertEqual(records[-1]['peak_sampled_rss_mib'],1811)
                self.assertEqual(records[-1]['watchdog_result'], 'critical system memory pressure' if critical else 'released to user')
                self.assertEqual(close.call_count, int(critical))
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig,handler)

    def test_multicore_startup_does_not_end_game_when_memory_and_heat_are_normal(self):
        import game_watchdog as guard
        handlers={s:signal.getsignal(s) for s in (signal.SIGTERM,signal.SIGINT)}
        clock=itertools.count()
        try:
            with patch('sys.argv',['watchdog','--expected','/bin/sleep','--claim-pid','42424','--run-seconds','25',
                                   '--swap-baseline-mib','600']), \
                 patch.object(guard,'battery_status',return_value=(True,80)), \
                 patch.object(guard,'swap_used',return_value=1031*MIB), \
                 patch.object(guard,'memory_pressure_level',return_value=2), \
                 patch.object(guard,'memory_free_percentage',return_value=40), \
                 patch.object(guard,'thermal_state',return_value=0), \
                 patch.object(guard.shutil,'disk_usage',return_value=SimpleNamespace(free=12*GIB)), \
                 patch.object(guard,'processes',return_value=[(42424,900*MIB,390.7)]), \
                 patch.object(guard.time,'monotonic',side_effect=lambda:next(clock)), \
                 patch.object(guard.time,'sleep'),patch.object(guard,'stop_exact') as stop, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(guard.main(),0)
                stop.assert_called_once_with(42424,'/bin/sleep')
            result=json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(result['watchdog_result'],'short test deadline')
            self.assertEqual(result['peak_sampled_cpu_pct'],390.7)
        finally:
            for sig,handler in handlers.items():signal.signal(sig,handler)

    def test_healthy_ranked_worker_can_exceed_the_offline_rss_budget(self):
        import resource_guard as guard
        handlers={s:signal.getsignal(s) for s in (signal.SIGTERM,signal.SIGINT)}
        try:
            for ranked in (True,False):
                with self.subTest(ranked=ranked), tempfile.TemporaryDirectory() as tmp:
                    args=['guard','--log',str(Path(tmp)/'actions.log'),'--max-rss-mib','200','--defer-maintenance']
                    if ranked:args+=['--ranked-system-memory','--parent-pid',str(os.getppid())]
                    args+=['--','/bin/sleep','0.4']
                    with patch('sys.argv',args),patch.object(guard,'group_usage',return_value=(350*MIB,10,1)), \
                         patch.object(guard,'battery_status',return_value=(True,80)),patch.object(guard,'swap_used',return_value=0), \
                         patch.object(guard,'memory_free_percentage',return_value=47),patch.object(guard,'memory_pressure_level',return_value=1), \
                         patch.object(guard,'thermal_state',return_value=0), \
                         patch.object(guard.shutil,'disk_usage',return_value=SimpleNamespace(free=12*GIB)), \
                         contextlib.redirect_stdout(io.StringIO()) as out:
                        self.assertEqual(guard.main(),0 if ranked else 124)
                    final=json.loads(out.getvalue().splitlines()[-1])['result']
                    self.assertEqual(final['peak_sampled_rss_mib'],350)
                    self.assertEqual(final['reason'],None if ranked else 'process group memory limit')
        finally:
            for sig,handler in handlers.items():signal.signal(sig,handler)

    def test_critical_pressure_still_terminates_only_the_isolated_child(self):
        import resource_guard as guard
        with tempfile.TemporaryDirectory() as tmp:
            args=['guard','--log',str(Path(tmp)/'actions.log'),'--timeout','0','--defer-maintenance',
                  '--ranked-system-memory','--parent-pid',str(os.getppid()),'--','/bin/sleep','15']
            handlers={s:signal.getsignal(s) for s in (signal.SIGTERM,signal.SIGINT)}
            pressure=iter([1])
            try:
                with patch('sys.argv',args), patch.object(guard,'battery_status',return_value=(True,80)), \
                     patch.object(guard,'swap_used',return_value=1031*MIB), \
                     patch.object(guard,'memory_pressure_level',side_effect=lambda:next(pressure,4)), \
                     patch.object(guard,'memory_free_percentage',return_value=40), \
                     patch.object(guard,'thermal_state',return_value=0), \
                     patch.object(guard.shutil,'disk_usage',return_value=SimpleNamespace(free=12*GIB)), \
                     contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(guard.main(),124)
                rows=list(map(json.loads,output.getvalue().splitlines()))
                self.assertEqual(rows[-1]['result']['reason'],'critical system memory pressure')
                with self.assertRaises(ProcessLookupError):
                    os.kill(rows[1]['started_pid'],0)
            finally:
                for sig,handler in handlers.items():signal.signal(sig,handler)
