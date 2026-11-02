"""Start or rejoin the native app's wake holder across backend updates."""
import subprocess
from pathlib import Path

from navigation import Stop, read_json_line, stop_child


class AppWake:
    def __init__(self, lease, identity, pid, process):
        self.lease = lease
        self.identity = identity
        self.pid = pid
        self.process = process

    @classmethod
    def acquire(cls, data, app_pid, helper=None):
        lease = data / ('awake-%d.lease' % app_pid)
        binary = helper or Path(__file__).with_name('app-awake')
        child = subprocess.Popen([str(binary), str(app_pid), str(lease)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            try:
                reply = read_json_line(child, 4)
            except Stop as error:
                raise RuntimeError('保持唤醒未收到应用进程存活确认') from error
            status, pid = reply.get('status'), reply.get('pid')
            if status not in ('started', 'reused') or type(pid) is not int or pid <= 1:
                raise RuntimeError('应用保持唤醒未能启动')
            if status == 'reused' and child.wait(timeout=3) != 0:
                raise RuntimeError('应用保持唤醒未能接续')
            stat = lease.lstat()
            return cls(lease, (stat.st_dev, stat.st_ino), pid, child if status == 'started' else None)
        except BaseException:
            stop_child(child)
            raise
        finally:
            child.stdout.close()

    def release(self):
        try:
            stat = self.lease.lstat()
            if (stat.st_dev, stat.st_ino) == self.identity:
                self.lease.unlink()
        except FileNotFoundError:
            pass
        if self.process is not None:
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                stop_child(self.process)
