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
                self.assertEqual(capture.poll(),1)
                self.assertTrue(capture.capped)
                self.assertEqual(capture.destination.read_bytes(),b'abc')
                self.assertEqual(capture.offset,4)
                source.unlink()
                source.write_bytes(b"abcd")
                with self.assertRaisesRegex(CaptureError, "rotated"):
                    capture.poll()
            finally:
                capture.close()

    def test_live_parser_keeps_following_after_evidence_cap(self):
        from incremental_board import IncrementalBoard, LogFollower
        from test_incremental_board import fixture, power
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'Power.log';source.write_bytes(fixture())
            capture=Capture(source,Path(directory)/'capture.log',max_bytes=32)
            board=IncrementalBoard();reader=LogFollower(source,board)
            try:
                capture.poll();reader.poll()
                with source.open('ab') as stream:
                    stream.write(power('TAG_CHANGE Entity=1 tag=TURN value=8').encode())
                capture.poll();reader.poll()
                self.assertEqual(board.snapshot(1)['game_turn'],'8')
                self.assertEqual(capture.destination.stat().st_size,32)
            finally:
                capture.close();reader.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
