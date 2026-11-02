"""Foreground and emergency hotkey lease, private to one ranked coordinator."""
import ctypes
from functools import cache
import os
import struct
import subprocess
import tempfile
from pathlib import Path

from navigation import read_json_line, stop_child


class FocusLost(RuntimeError):
    """The native helper rejected this operation before posting any input."""


class PointCovered(FocusLost):
    """A foreground game can still have another window above the click point."""


@cache
def _point_checker():
    lib = ctypes.CDLL(str(Path(__file__).with_name('pointer-target.dylib')))
    check = lib.hs_point_owned
    check.argtypes = (ctypes.c_int,ctypes.c_double,ctypes.c_double)
    check.restype = ctypes.c_int
    return check


def check_point(pid, x, y):
    if not _point_checker()(pid, float(x), float(y)):
        raise PointCovered('目标位置被遮挡，未发送鼠标输入')


def rejected_before_input(detail):
    return detail in ('Hearthstone is not the frontmost app', 'Could not uniquely locate a visible Hearthstone window')


class RankedFocus:
    def __init__(self, data):
        self.file = tempfile.NamedTemporaryFile(prefix='.ranked-focus-', dir=data)
        self.process = None
        self.target = 0
        try:
            self.file.write(struct.pack('=i', 0) + b'I' + bytes(4))
            self.file.flush()
            fd = self.file.fileno()
            self.process = subprocess.Popen([str(Path(__file__).with_name('ranked-focus')), str(os.getpid()), str(fd)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, pass_fds=(fd,))
            if read_json_line(self.process, 4).get('status') != 'armed':
                raise RuntimeError('自动天梯退出快捷键注册失败')
            self.process.stdout.close()
        except BaseException:
            self.close()
            raise

    def bind(self, pid):
        if self.target != pid:
            os.pwrite(self.file.fileno(), struct.pack('=i', pid), 0)
            self.target = pid

    def state(self):
        if self.process.poll() is not None:
            raise RuntimeError('前台和退出快捷键组件已退出')
        return os.pread(self.file.fileno(), 1, 4)

    @property
    def stopped(self):
        return os.pread(self.file.fileno(), 1, 4) == b'S'

    def close(self):
        try:
            stop_child(self.process)
            if self.process and self.process.stdout:
                self.process.stdout.close()
        finally:
            self.file.close()
