"""Standalone GUI smoke test — runs MainWindow offscreen, adds the ChatGPT
test image, inspects it, runs a safe-mode clean, screenshots the pre-clean
state, and verifies the output is fingerprint-free.

Run:
    QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/gui_smoke.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from imgclean.detect import inspect as run_inspect
from imgclean_gui.app import (
    MainWindow, STATUS_DIRTY, STATUS_DONE, STATUS_FAILED,
)


SAMPLE = ROOT / "samples" / "demo_image.png"
SCREENSHOT = ROOT / "docs" / "smoke_screenshot.png"


def spin(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_until(predicate, timeout_ms=15000, step_ms=80) -> bool:
    elapsed = 0
    while elapsed < timeout_ms:
        if predicate():
            return True
        spin(step_ms)
        elapsed += step_ms
    return False


def main() -> int:
    if not SAMPLE.exists():
        print(f"FAIL: sample missing at {SAMPLE} — run scripts/make_demo_image.py first")
        return 2

    app = QApplication.instance() or QApplication(sys.argv)
    tmpdir = Path(tempfile.mkdtemp(prefix="imgclean-gui-"))
    src = tmpdir / SAMPLE.name
    shutil.copy(SAMPLE, src)

    w = MainWindow()
    # Default "same folder" output — avoids the QFileDialog modal that
    # custom-folder mode would open and hang the offscreen run.
    w.out_same.setChecked(True)
    w.mode_radio["safe"].setChecked(True)
    w._add_paths([src])
    w.resize(1200, 760)
    w.show()
    spin(200)

    assert w.model.rowCount() == 1
    print(f"1: added {src.name}", flush=True)

    if not wait_until(lambda: w.model.entries()[0].status == STATUS_DIRTY):
        e = w.model.entries()[0]
        print(f"FAIL: inspect did not complete. status={e.status} error={e.error}")
        return 1
    e = w.model.entries()[0]
    print(f"2: inspect — status={e.status}, findings={len(e.report.findings)}, "
          f"badges={e.badges}", flush=True)

    w.list_view.setCurrentIndex(w.model.index(0))
    spin(150)
    pix = w.grab()
    if not pix.save(str(SCREENSHOT), "PNG"):
        print(f"FAIL: screenshot save failed")
        return 1
    print(f"3: screenshot → {SCREENSHOT.name} ({SCREENSHOT.stat().st_size:,}B)", flush=True)

    w._start_batch()
    if not wait_until(lambda: w.model.entries()[0].status in (STATUS_DONE, STATUS_FAILED)):
        e = w.model.entries()[0]
        print(f"FAIL: clean timed out. status={e.status} error={e.error}")
        return 1
    e = w.model.entries()[0]
    if e.status != STATUS_DONE:
        print(f"FAIL: clean did not succeed. status={e.status} error={e.error}")
        return 1
    print(f"4: clean done — output={e.output.name}", flush=True)

    post = run_inspect(str(e.output))
    if not post.is_clean:
        print(f"FAIL: output still has findings: {[f.name for f in post.findings]}")
        return 1
    print(f"5: re-inspect output → CLEAN ({len(post.findings)} findings)", flush=True)

    print("\nPASS")
    w.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
