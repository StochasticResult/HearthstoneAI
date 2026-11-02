#!/usr/bin/env python3
"""Capture one future game's local Power.log in bounded, private chunks.

Run *before* starting Hearthstone.  This tool reads local files only; it sends
no game inputs. It refuses truncation/rotation, but reaching the evidence cap
only stops copying: the independent live parser keeps reading the source.
The output may contain account identifiers and must not be uploaded.
"""
import argparse
import os
import time
from pathlib import Path

LOG_ROOT = Path("/Applications/Hearthstone/Logs")
MAX_BYTES = 8 * 1024 * 1024


class CaptureError(Exception):
    pass


class Capture:
    def __init__(self, source, destination, max_bytes=MAX_BYTES, start_offset=0):
        self.source = Path(source)
        self.destination = Path(destination)
        self.max_bytes = max_bytes
        self.reader = self.source.open("rb")
        stat = os.fstat(self.reader.fileno())
        self.identity = (stat.st_dev, stat.st_ino)
        if type(start_offset) is not int or not 0 <= start_offset <= stat.st_size:
            self.reader.close()
            raise CaptureError('invalid game log start offset')
        self.offset = start_offset
        self.reader.seek(start_offset)
        self.written = 0
        self.capped = False
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        self.writer = os.fdopen(os.open(self.destination, flags, 0o600), "wb", buffering=0)

    def poll(self):
        stat = self.source.stat()
        if (stat.st_dev, stat.st_ino) != self.identity or stat.st_size < self.offset:
            raise CaptureError("source rotated or truncated")
        start = self.offset
        save_until = min(stat.st_size, start + self.max_bytes - self.written)
        # Only copy the bytes present at entry. A live writer may keep
        # appending while the independent gameplay reader catches up.
        while self.offset < save_until:
            data = self.reader.read(min(64 * 1024, save_until - self.offset))
            if not data:
                raise CaptureError("source shortened during evidence read")
            self.writer.write(data)
            self.written += len(data)
            self.offset += len(data)
        if self.offset < stat.st_size:
            self.capped = True
            self.offset = stat.st_size
            self.reader.seek(self.offset)
        return self.offset - start

    def close(self):
        self.reader.close()
        self.writer.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=420)
    args = parser.parse_args()
    if not (30 <= args.timeout <= 600):
        parser.error("timeout must be between 30 and 600 seconds")
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    old = {p.name for p in LOG_ROOT.glob("Hearthstone_*")}
    deadline = time.monotonic() + args.timeout
    cap = None
    print("practice log capture armed", flush=True)
    try:
        while time.monotonic() < deadline:
            if cap is None:
                candidates = sorted(p for p in LOG_ROOT.glob("Hearthstone_*") if p.name not in old and (p / "Power.log").is_file())
                if len(candidates) > 1:
                    raise CaptureError("multiple new game log directories")
                if candidates:
                    cap = Capture(candidates[0] / "Power.log", args.output)
                    print("capture attached to new game log", flush=True)
            if cap:
                cap.poll()
            time.sleep(0.25)
        print("capture deadline reached", flush=True)
    finally:
        if cap:
            try:
                cap.poll()
            except (CaptureError, OSError):
                # Retain the observed prefix but never pretend it is complete.
                print("source ended or changed; captured prefix only", flush=True)
            nbytes = cap.written
            cap.close()
            print("captured_bytes=%d" % nbytes, flush=True)
    return 0 if cap and cap.offset else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CaptureError, OSError) as error:
        print("capture stopped: " + str(error), flush=True)
        raise SystemExit(2)
