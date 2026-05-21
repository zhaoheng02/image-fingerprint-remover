"""Background workers — inspect (parallel) and clean (sequential)."""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal, QThread

from imgclean.clean import clean_bytes
from imgclean.detect import inspect as run_inspect
from imgclean.findings import InspectReport


# --------------------------------------------------------------------------- #
# inspect (one-shot, runs in QThreadPool)                                     #
# --------------------------------------------------------------------------- #


class InspectSignals(QObject):
    done = Signal(str, object)   # (path, InspectReport)
    failed = Signal(str, str)    # (path, error_message)


class InspectTask(QRunnable):
    def __init__(self, path: str):
        super().__init__()
        self.path = path
        self.signals = InspectSignals()

    def run(self):
        try:
            report = run_inspect(self.path)
            self.signals.done.emit(self.path, report)
        except Exception as exc:
            self.signals.failed.emit(self.path, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# clean (sequential, runs in a single QThread, supports cancel)               #
# --------------------------------------------------------------------------- #


@dataclass
class CleanJob:
    src: Path
    mode: str
    out_path: Path
    in_place: bool  # write atomically via temp+rename


class CleanWorker(QThread):
    file_started = Signal(str)                  # src
    file_done = Signal(str, str, object)        # src, out_path, post-clean InspectReport
    file_failed = Signal(str, str)              # src, error_message
    batch_done = Signal(int, int)               # cleaned_count, total

    def __init__(self, jobs: list[CleanJob], parent=None):
        super().__init__(parent)
        self._jobs = jobs
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        cleaned = 0
        total = len(self._jobs)
        for job in self._jobs:
            if self._cancel:
                break
            try:
                self.file_started.emit(str(job.src))
                data = job.src.read_bytes()
                out_bytes, _ = clean_bytes(data, mode=job.mode)
                job.out_path.parent.mkdir(parents=True, exist_ok=True)

                if job.in_place:
                    # Atomic replace: write to sibling temp, fsync, then rename.
                    tmp_fd, tmp_name = tempfile.mkstemp(
                        prefix=".imgclean-", suffix=job.src.suffix,
                        dir=str(job.src.parent),
                    )
                    try:
                        with os.fdopen(tmp_fd, "wb") as fh:
                            fh.write(out_bytes)
                            fh.flush()
                            os.fsync(fh.fileno())
                        os.replace(tmp_name, job.out_path)
                    except Exception:
                        try:
                            os.unlink(tmp_name)
                        except OSError:
                            pass
                        raise
                else:
                    job.out_path.write_bytes(out_bytes)

                if job.mode in ("paranoid", "nuclear"):
                    try:
                        ts = 946684800.0  # 2000-01-01 UTC
                        os.utime(job.out_path, (ts, ts))
                    except OSError:
                        pass

                post = run_inspect(str(job.out_path))
                cleaned += 1
                self.file_done.emit(str(job.src), str(job.out_path), post)
            except Exception as exc:
                self.file_failed.emit(str(job.src), f"{type(exc).__name__}: {exc}")
        self.batch_done.emit(cleaned, total)
