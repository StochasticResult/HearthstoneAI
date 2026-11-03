"""Process-level checks using disposable sleep processes for the game and GUI."""
from contextlib import closing
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest


class RankedFaultGuardTests(unittest.TestCase):
    def test_supervisor_crash_preserves_client_and_safety_or_explicit_stop_still_work(self):
        guard_code = """
import sys
from pathlib import Path
from types import SimpleNamespace
import game_watchdog as g
thermal_flag = Path(sys.argv.pop(1))
g.battery_status = lambda: (True,80)
g.swap_used = lambda: 0
g.memory_free_percentage = lambda: 70
g.memory_pressure_level = lambda: 1
g.thermal_state = lambda: 2 if thermal_flag.exists() else 0
g.shutil.disk_usage = lambda _: SimpleNamespace(free=20*g.GIB)
raise SystemExit(g.main())
"""
        controller_code = """
import json,os,subprocess,sys
guard = subprocess.Popen([sys.executable,'-u','-c',sys.argv[1],sys.argv[2],
    '--expected','/bin/sleep','--claim-pid',sys.argv[3],'--parent-pid',str(os.getpid()),
    '--native-owner-pid',sys.argv[4],'--run-seconds','0'], stdout=subprocess.PIPE,text=True)
armed = json.loads(guard.stdout.readline())
claimed = json.loads(guard.stdout.readline())
print(json.dumps([armed['pid'],claimed['claimed_pid']]),flush=True)
sys.stdin.readline()
guard.stdout.close()
os._exit(17)
"""
        for stopping in ('thermal','quit_gui','explicit_stop'):
            with self.subTest(stopping=stopping), tempfile.TemporaryDirectory() as tmp:
                target = subprocess.Popen(['/bin/sleep','40'])
                owner = subprocess.Popen(['/bin/sleep','40'])
                flag = Path(tmp)/'thermal'
                controller = subprocess.Popen([sys.executable,'-u','-c',controller_code,guard_code,
                    str(flag),str(target.pid),str(owner.pid)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
                guard_pid = None
                try:
                    self.assertTrue(select.select([controller.stdout],[],[],6)[0])
                    guard_pid, claimed = json.loads(controller.stdout.readline())
                    self.assertEqual(claimed,target.pid)
                    controller.stdin.write('crash\n');controller.stdin.flush()
                    self.assertEqual(controller.wait(timeout=3),17)
                    time.sleep(1.5)  # Longer than the watchdog's one-second sampling interval.
                    self.assertIsNone(target.poll())
                    os.kill(guard_pid,0)
                    if stopping == 'thermal':
                        flag.touch()
                    elif stopping == 'quit_gui':
                        with closing(select.kqueue()) as exit_event:
                            exit_event.control([select.kevent(guard_pid, filter=select.KQ_FILTER_PROC,
                                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT, fflags=select.KQ_NOTE_EXIT)], 0, 0)
                            owner.terminate();owner.wait(timeout=2)
                            self.assertTrue(exit_event.control(None, 1, 5))
                        self.assertIsNone(target.poll())
                    else:
                        os.kill(guard_pid,signal.SIGTERM)
                    if stopping != 'quit_gui':
                        self.assertNotEqual(target.wait(timeout=5),0)
                        self.assertIsNone(owner.poll())
                finally:
                    controller.stdin.close();controller.stdout.close()
                    for child in (controller,target,owner):
                        if child.poll() is None:
                            child.kill();child.wait(timeout=3)
                    if guard_pid:
                        try:os.kill(guard_pid,signal.SIGTERM)
                        except ProcessLookupError:pass
