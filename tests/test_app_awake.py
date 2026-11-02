import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from app_awake import AppWake
from scripts.build_app import build_awake


class AppWakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.build.cleanup)
        cls.binary = Path(cls.build.name) / 'app-awake'
        build_awake(cls.binary)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        self.owner = subprocess.Popen(['/bin/sleep','30'])
        self.addCleanup(self.close_owner)

    def close_owner(self):
        if self.owner.poll() is None:
            self.owner.terminate()
        self.owner.wait(timeout=3)

    def test_backend_can_exit_and_next_backend_reuses_the_same_holder(self):
        # This first worker really exits while its stand-in GUI stays alive.
        code = ('from app_awake import AppWake; from pathlib import Path; import sys; '
                'print(AppWake.acquire(Path(sys.argv[1]),int(sys.argv[2]),Path(sys.argv[3])).pid)')
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'runtime'))
        pid = int(subprocess.check_output([sys.executable,'-c',code,str(self.data),
                                           str(self.owner.pid),str(self.binary)], env=environment, timeout=6))
        handle = AppWake.acquire(self.data,self.owner.pid,self.binary)
        self.assertEqual(handle.pid,pid)
        self.assertIsNone(handle.process)
        self.assertTrue(handle.lease.is_file())
        if 'AC Power' in subprocess.check_output(['/usr/bin/pmset','-g','batt'], text=True):
            assertions = subprocess.check_output(['/usr/bin/pmset','-g','assertions'], text=True)
            self.assertTrue(any('pid '+str(pid)+'(' in line and 'PreventUserIdleDisplaySleep' in line
                                and 'Hearthstone AI' in line for line in assertions.splitlines()))
        self.close_owner()
        deadline = time.monotonic()+3
        while handle.lease.exists() and time.monotonic()<deadline:
            time.sleep(.03)
        self.assertFalse(handle.lease.exists())

    def test_explicit_stop_releases_adopted_assertion_while_gui_stays_open(self):
        first = AppWake.acquire(self.data,self.owner.pid,self.binary)
        self.addCleanup(first.release)
        adopted = AppWake.acquire(self.data,self.owner.pid,self.binary)
        adopted.release()
        self.assertEqual(first.process.wait(timeout=3),0)
        self.assertIsNone(self.owner.poll())
        self.assertFalse(first.lease.exists())

    def test_unknown_owner_leaves_no_assertion_or_lease(self):
        with self.assertRaises(RuntimeError):
            # Use the pid of a reaped process, not another user's process.
            self.close_owner()
            AppWake.acquire(self.data,self.owner.pid,self.binary)
        self.assertEqual(list(self.data.iterdir()),[])
