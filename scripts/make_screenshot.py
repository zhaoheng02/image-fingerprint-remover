"""Render a README-quality screenshot of the GUI with the synthetic demo
image loaded and inspected.

Run:
    QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/make_screenshot.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from imgclean_gui.app import MainWindow, STATUS_DIRTY


def spin(ms):
    loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()


def wait_until(pred, timeout=8000, step=80):
    elapsed = 0
    while elapsed < timeout:
        if pred(): return True
        spin(step); elapsed += step
    return False


def main():
    demo = ROOT / "samples" / "demo_image.png"
    if not demo.exists():
        print(f"missing {demo}; run scripts/make_demo_image.py first")
        return 2
    app = QApplication.instance() or QApplication(sys.argv)
    w = MainWindow()
    w.out_same.setChecked(True)
    w.mode_radio["paranoid"].setChecked(True)
    w._add_paths([demo])
    w.resize(1280, 800)
    w.show()
    spin(250)
    wait_until(lambda: w.model.entries()[0].status == STATUS_DIRTY)
    spin(150)
    w.list_view.setCurrentIndex(w.model.index(0))
    spin(200)
    out = ROOT / "docs" / "screenshot.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    pix = w.grab()
    if not pix.save(str(out), "PNG"):
        print("save failed"); return 1
    print(f"wrote {out} ({out.stat().st_size:,}B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
