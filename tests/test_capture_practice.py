import os
import tempfile
import unittest
from pathlib import Path

from capture_practice import Capture, CaptureError


class CaptureTests(unittest.TestCase):
    def test_incremental_private_capture_and_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "Power.log"
            destination = Path(directory) / "capture.log"
            source.write_bytes(b"a\n")
            capture = Capture(source, destination, max_bytes=16)
            try:
                self.assertEqual(capture.poll(), 2)
                with source.open("ab") as handle:
                    handle.write(b"b\n")
                self.assertEqual(capture.poll(), 2)
                self.assertEqual(destination.read_bytes(), b"a\nb\n")
                self.assertEqual(os.stat(destination).st_mode & 0o777, 0o600)
                source.write_bytes(b"x")
                with self.assertRaisesRegex(CaptureError, "truncated"):
                    capture.poll()
            finally:
                capture.close()

    def test_cap_and_replace_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "Power.log"
            source.write_bytes(b"abc")
            capture = Capture(source, Path(directory) / "capture.log", max_bytes=3)
            try:
                capture.poll()
                with source.open("ab") as handle:
                    handle.write(b"d")
                with self.assertRaisesRegex(CaptureError, "limit"):
                    capture.poll()
                source.unlink()
                source.write_bytes(b"abcd")
                with self.assertRaisesRegex(CaptureError, "rotated"):
                    capture.poll()
            finally:
                capture.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
