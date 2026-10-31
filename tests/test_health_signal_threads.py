"""The journal thread and the monitor thread share one DegradationDetector."""
from __future__ import annotations

import io
import sys
import threading

from totalray.health_signal import DegradationDetector, JournalTailer


def test_concurrent_observers_do_not_corrupt_the_detector():
    detector = DegradationDetector(error_threshold=3, window_seconds=10.0)
    detector.set_active_tag("cfg-1")
    failures: list[BaseException] = []
    stop = threading.Event()

    def journal():
        t = 0.0
        try:
            while not stop.is_set():
                t += 1.0
                detector.observe_log_line("cfg-1: connection reset", now=t)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    def monitor():
        t = 0.0
        try:
            for _ in range(5000):
                t += 1.0
                detector.observe_retransmits(0, now=t)
                detector.snapshot(now=t)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
        finally:
            stop.set()

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=journal), threading.Thread(target=monitor)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(30)
    finally:
        sys.setswitchinterval(old)
    assert failures == []


def test_journal_reader_survives_a_failing_callback():
    seen = []

    def callback(line):
        seen.append(line)
        if line == "bad":
            raise RuntimeError("boom")

    tailer = JournalTailer(callback)
    tailer.process = type("P", (), {"stdout": io.StringIO("one\nbad\ntwo\n")})()
    tailer._read()
    assert seen == ["one", "bad", "two"]
