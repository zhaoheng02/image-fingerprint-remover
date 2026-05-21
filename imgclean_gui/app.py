"""Main window for imgclean GUI."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QAbstractListModel, QModelIndex, QSize, QThreadPool, QSettings, QTimer
from PySide6.QtGui import (
    QAction, QColor, QDragEnterEvent, QDropEvent, QFont, QIcon, QImage, QPainter,
    QPalette, QPixmap, QStandardItem, QStandardItemModel,
)
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QListView, QMainWindow, QMessageBox, QProgressBar, QPushButton, QRadioButton,
    QSizePolicy, QSplitter, QStackedWidget, QStatusBar, QStyle, QStyledItemDelegate,
    QTextEdit, QToolButton, QVBoxLayout, QWidget,
)

from imgclean.findings import InspectReport, Severity
from .workers import CleanJob, CleanWorker, InspectTask


SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".PNG", ".JPG", ".JPEG"}

STATUS_PENDING = "pending"
STATUS_INSPECTING = "inspecting"
STATUS_DIRTY = "dirty"
STATUS_CLEAN = "clean"
STATUS_CLEANING = "cleaning"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

STATUS_LABELS = {
    STATUS_PENDING: "等待…",
    STATUS_INSPECTING: "检测中…",
    STATUS_DIRTY: "已脏",
    STATUS_CLEAN: "干净",
    STATUS_CLEANING: "清理中…",
    STATUS_DONE: "已清除 ✓",
    STATUS_FAILED: "失败 ✕",
}

STATUS_COLORS = {
    STATUS_PENDING: "#888888",
    STATUS_INSPECTING: "#888888",
    STATUS_DIRTY: "#e67e22",
    STATUS_CLEAN: "#27ae60",
    STATUS_CLEANING: "#3498db",
    STATUS_DONE: "#27ae60",
    STATUS_FAILED: "#c0392b",
}


# --------------------------------------------------------------------------- #
# Item model                                                                  #
# --------------------------------------------------------------------------- #


@dataclass
class FileEntry:
    path: Path
    status: str = STATUS_PENDING
    report: Optional[InspectReport] = None
    output: Optional[Path] = None
    error: str = ""
    thumb: Optional[QPixmap] = None
    badges: list[str] = field(default_factory=list)  # short labels


class FileListModel(QAbstractListModel):
    PathRole = Qt.UserRole + 1
    EntryRole = Qt.UserRole + 2

    def __init__(self):
        super().__init__()
        self._entries: list[FileEntry] = []
        self._index_by_path: dict[str, int] = {}

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._entries)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        e = self._entries[index.row()]
        if role == Qt.DisplayRole:
            return e.path.name
        if role == Qt.UserRole + 1:
            return str(e.path)
        if role == Qt.UserRole + 2:
            return e
        return None

    def entries(self) -> list[FileEntry]:
        return list(self._entries)

    def add_paths(self, paths: list[Path]) -> list[FileEntry]:
        new: list[FileEntry] = []
        for p in paths:
            key = str(p.resolve())
            if key in self._index_by_path:
                continue
            row = len(self._entries)
            self.beginInsertRows(QModelIndex(), row, row)
            entry = FileEntry(path=p)
            self._entries.append(entry)
            self._index_by_path[key] = row
            self.endInsertRows()
            new.append(entry)
        return new

    def find(self, path_str: str) -> Optional[FileEntry]:
        idx = self._index_by_path.get(str(Path(path_str).resolve()))
        if idx is None:
            return None
        return self._entries[idx]

    def index_for_path(self, path_str: str) -> Optional[QModelIndex]:
        key = str(Path(path_str).resolve())
        idx = self._index_by_path.get(key)
        if idx is None:
            return None
        return self.index(idx)

    def update_status(self, path_str: str, **fields) -> None:
        idx = self.index_for_path(path_str)
        if idx is None:
            return
        entry = self._entries[idx.row()]
        for k, v in fields.items():
            setattr(entry, k, v)
        self.dataChanged.emit(idx, idx, [Qt.DisplayRole])

    def clear_done(self) -> None:
        keep = [e for e in self._entries if e.status not in (STATUS_DONE,)]
        self.beginResetModel()
        self._entries = keep
        self._index_by_path = {str(e.path.resolve()): i for i, e in enumerate(keep)}
        self.endResetModel()

    def clear_all(self) -> None:
        self.beginResetModel()
        self._entries = []
        self._index_by_path = {}
        self.endResetModel()


# --------------------------------------------------------------------------- #
# Custom delegate — thumbnail + filename + status badge + finding badges      #
# --------------------------------------------------------------------------- #


class FileRowDelegate(QStyledItemDelegate):
    ROW_HEIGHT = 72
    THUMB_SIZE = 56
    PAD = 10

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), self.ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        entry: FileEntry = index.data(FileListModel.EntryRole)
        rect = option.rect

        # Selection background.
        if bool(option.state & QStyle.StateFlag.State_Selected):
            painter.fillRect(rect, QColor("#d0e3ff"))
        else:
            painter.fillRect(rect, QColor("#ffffff"))

        # Thumbnail box
        thumb_rect = rect.adjusted(self.PAD, self.PAD - 2, 0, -self.PAD + 2)
        thumb_rect.setWidth(self.THUMB_SIZE)
        thumb_rect.setHeight(self.THUMB_SIZE)
        painter.fillRect(thumb_rect, QColor("#eeeeee"))
        if entry.thumb is not None:
            scaled = entry.thumb.scaled(
                self.THUMB_SIZE, self.THUMB_SIZE,
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
            x = thumb_rect.x() + (self.THUMB_SIZE - scaled.width()) // 2
            y = thumb_rect.y() + (self.THUMB_SIZE - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        else:
            painter.setPen(QColor("#bbbbbb"))
            painter.drawText(thumb_rect, Qt.AlignCenter, "…")

        # Right column text area
        text_x = thumb_rect.right() + self.PAD
        text_w = rect.right() - text_x - self.PAD
        name_rect = rect.adjusted(0, 8, -self.PAD, 0)
        name_rect.setLeft(text_x)
        name_rect.setHeight(20)

        font = painter.font()
        bold = QFont(font)
        bold.setBold(True)
        painter.setFont(bold)
        painter.setPen(QColor("#222"))
        elided = painter.fontMetrics().elidedText(entry.path.name, Qt.ElideMiddle, text_w)
        painter.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter, elided)

        # Status + path
        sub_rect = name_rect.translated(0, 20)
        sub_rect.setHeight(18)
        painter.setFont(font)
        try:
            size_kb = entry.path.stat().st_size / 1024
            size_s = f"{size_kb:.0f} KB" if size_kb < 1024 else f"{size_kb/1024:.1f} MB"
        except OSError:
            size_s = "?"

        status_color = QColor(STATUS_COLORS.get(entry.status, "#888"))
        status_label = STATUS_LABELS.get(entry.status, entry.status)
        if entry.status == STATUS_DIRTY and entry.report:
            status_label += f" · {len(entry.report.findings)} 项"
        painter.setPen(status_color)
        painter.drawText(sub_rect, Qt.AlignLeft | Qt.AlignVCenter, f"{status_label}   ·   {size_s}")

        # Finding badges row
        if entry.badges:
            badge_rect = sub_rect.translated(0, 18)
            badge_rect.setHeight(18)
            x = badge_rect.x()
            painter.setPen(Qt.NoPen)
            small = QFont(font)
            small.setPointSize(max(8, font.pointSize() - 1))
            painter.setFont(small)
            for b in entry.badges[:6]:
                tw = painter.fontMetrics().horizontalAdvance(b) + 10
                br = badge_rect.adjusted(x - badge_rect.x(), 0, 0, 0)
                br.setWidth(tw)
                br.setHeight(16)
                painter.setBrush(QColor("#fbe5d2"))
                painter.drawRoundedRect(br, 4, 4)
                painter.setPen(QColor("#a04b00"))
                painter.drawText(br, Qt.AlignCenter, b)
                painter.setPen(Qt.NoPen)
                x += tw + 6

        # Bottom hairline
        painter.setPen(QColor("#eeeeee"))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
        painter.restore()


# --------------------------------------------------------------------------- #
# Detail pane                                                                 #
# --------------------------------------------------------------------------- #


class DetailPane(QWidget):
    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(8)
        self.title = QLabel("选择左侧文件查看详情")
        self.title.setStyleSheet("font-weight: 600; font-size: 14px;")
        v.addWidget(self.title)
        self.subtitle = QLabel("")
        self.subtitle.setStyleSheet("color: #666;")
        v.addWidget(self.subtitle)
        self.body = QTextEdit()
        self.body.setReadOnly(True)
        self.body.setStyleSheet("background: #fafafa; border: 1px solid #e8e8e8; border-radius: 6px;")
        v.addWidget(self.body, 1)

    def show_entry(self, entry: Optional[FileEntry]):
        if entry is None:
            self.title.setText("选择左侧文件查看详情")
            self.subtitle.setText("")
            self.body.clear()
            return
        self.title.setText(entry.path.name)
        try:
            sz = entry.path.stat().st_size
            self.subtitle.setText(f"{entry.path} · {sz:,} 字节 · {STATUS_LABELS.get(entry.status, '?')}")
        except OSError:
            self.subtitle.setText(str(entry.path))

        html_parts: list[str] = []
        if entry.error:
            html_parts.append(f"<p style='color:#c0392b'><b>错误：</b>{entry.error}</p>")
        if entry.report is None:
            html_parts.append("<p style='color:#888'>等待检测…</p>")
        else:
            r = entry.report
            if r.is_clean and not r.findings:
                html_parts.append("<p style='color:#27ae60; font-weight:600'>未发现任何识别元素。</p>")
            elif r.is_clean:
                html_parts.append("<p style='color:#27ae60; font-weight:600'>主要标识已清除，仅余信息级条目。</p>")
            else:
                crit = sum(1 for f in r.findings if f.severity == Severity.CRITICAL)
                high = sum(1 for f in r.findings if f.severity == Severity.HIGH)
                html_parts.append(
                    f"<p><b>未清理</b> — 共 {len(r.findings)} 项；"
                    f"严重 {crit}，高 {high}。</p>"
                )
            for f in r.findings:
                color = {
                    Severity.CRITICAL: "#c0392b", Severity.HIGH: "#e67e22",
                    Severity.MED: "#d4ac0d", Severity.LOW: "#3498db", Severity.INFO: "#888",
                }.get(f.severity, "#444")
                html_parts.append(
                    f"<div style='margin: 6px 0; padding: 6px 8px; "
                    f"border-left: 3px solid {color}; background: #fff;'>"
                    f"<b style='color:{color}'>[{f.severity.value.upper()}]</b> "
                    f"<b>{_html_esc(f.name)}</b><br>"
                    f"<span style='color:#666'>{_html_esc(f.location)} · "
                    f"{_html_esc(f.category.value)} · {f.size_bytes:,}B</span><br>"
                    f"<span>{_html_esc(f.detail)}</span>"
                )
                if f.value_preview:
                    pv = f.value_preview.strip().replace("\n", " ")
                    if len(pv) > 220:
                        pv = pv[:220] + "…"
                    html_parts.append(
                        f"<br><span style='color:#888; font-family: monospace; "
                        f"font-size: 11px;'>预览: {_html_esc(pv)}</span>"
                    )
                html_parts.append("</div>")

        if entry.output:
            html_parts.append(
                f"<hr><p>已输出 → <code>{_html_esc(str(entry.output))}</code></p>"
            )
        self.body.setHtml("".join(html_parts))


def _html_esc(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
         .replace('"', "&quot;")
    )


# --------------------------------------------------------------------------- #
# Main window                                                                 #
# --------------------------------------------------------------------------- #


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("imgclean — 图像指纹清理")
        self.resize(1200, 760)
        self.setMinimumSize(960, 600)
        self.setAcceptDrops(True)

        self.settings = QSettings("imgclean", "gui")
        self.thread_pool = QThreadPool.globalInstance()
        self.thread_pool.setMaxThreadCount(4)
        self.clean_worker: Optional[CleanWorker] = None

        self.model = FileListModel()
        self._build_ui()
        self._restore_settings()

    # ---- UI construction ----

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- LEFT: mode + output + primary action ----
        left = QFrame()
        left.setFixedWidth(260)
        left.setStyleSheet("background: #f5f6f8; border-right: 1px solid #e2e4e8;")
        lv = QVBoxLayout(left)
        lv.setContentsMargins(14, 16, 14, 14)
        lv.setSpacing(12)

        lv.addWidget(_section_label("清理模式"))
        self.mode_group = QButtonGroup(self)
        self.mode_radio = {}
        for key, title, desc, tradeoff in [
            ("safe",     "安全 (Safe)",      "仅删除元数据，画质不变",
                                            "鲁棒水印仍可能存在"),
            ("paranoid", "深度 (Paranoid)",  "删元数据 + 重编码像素 + 轻噪声",
                                            "文件略变化，画质几乎无损"),
            ("nuclear",  "核弹 (Nuclear)",   "深度 + 裁剪缩放 + 色彩微调",
                                            "画质可见损失，不保证清除 SynthID"),
        ]:
            rb = QRadioButton(title)
            rb.setStyleSheet("font-weight: 600;")
            self.mode_radio[key] = rb
            self.mode_group.addButton(rb)
            lv.addWidget(rb)
            d = QLabel(desc)
            d.setWordWrap(True)
            d.setStyleSheet("color:#444; font-size: 11px; margin-left: 22px;")
            lv.addWidget(d)
            t = QLabel(f"代价：{tradeoff}")
            t.setWordWrap(True)
            t.setStyleSheet("color:#888; font-size: 11px; margin-left: 22px; margin-bottom: 6px;")
            lv.addWidget(t)
        self.mode_radio["paranoid"].setChecked(True)

        lv.addSpacing(4)
        lv.addWidget(_hline())
        lv.addSpacing(4)
        lv.addWidget(_section_label("输出位置"))

        self.out_group = QButtonGroup(self)
        self.out_same = QRadioButton("同目录 (.cleaned 后缀)")
        self.out_same.setChecked(True)
        self.out_custom = QRadioButton("自定义文件夹…")
        self.out_inplace = QRadioButton("原地替换 ⚠")
        self.out_inplace.setStyleSheet("color:#c0392b;")
        for rb in (self.out_same, self.out_custom, self.out_inplace):
            self.out_group.addButton(rb)
            lv.addWidget(rb)

        self.custom_dir_label = QLabel("")
        self.custom_dir_label.setStyleSheet("color:#444; font-size:11px; margin-left:22px;")
        self.custom_dir_label.setWordWrap(True)
        lv.addWidget(self.custom_dir_label)
        self.out_custom.toggled.connect(self._pick_custom_dir)

        lv.addStretch(1)

        self.btn_clean = QPushButton("添加图片")
        self.btn_clean.setEnabled(False)
        self.btn_clean.setMinimumHeight(44)
        self.btn_clean.setStyleSheet("""
            QPushButton {
                background: #2d7af6; color: white; font-weight: 600;
                font-size: 14px; border-radius: 8px;
            }
            QPushButton:hover { background: #1f6bdc; }
            QPushButton:disabled { background: #b8c8e0; color: #fff; }
        """)
        self.btn_clean.clicked.connect(self._on_primary_clicked)
        lv.addWidget(self.btn_clean)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        lv.addWidget(self.progress)

        root.addWidget(left)

        # ---- CENTER: file list ----
        center = QFrame()
        cv = QVBoxLayout(center)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)

        toolbar = QFrame()
        toolbar.setStyleSheet("background: #ffffff; border-bottom: 1px solid #e2e4e8;")
        tb = QHBoxLayout(toolbar)
        tb.setContentsMargins(12, 10, 12, 10)
        tb.setSpacing(8)
        btn_add_files = QPushButton("+ 添加图片")
        btn_add_files.clicked.connect(self._add_files_dialog)
        btn_add_folder = QPushButton("+ 添加文件夹")
        btn_add_folder.clicked.connect(self._add_folder_dialog)
        btn_clear = QToolButton()
        btn_clear.setText("清空列表")
        btn_clear.clicked.connect(self._on_clear)
        tb.addWidget(btn_add_files)
        tb.addWidget(btn_add_folder)
        tb.addStretch(1)
        tb.addWidget(btn_clear)
        cv.addWidget(toolbar)

        # Stack: dropzone (empty state) ↔ list
        self.stack = QStackedWidget()
        cv.addWidget(self.stack, 1)

        self.dropzone = _DropZone()
        self.stack.addWidget(self.dropzone)

        self.list_view = QListView()
        self.list_view.setModel(self.model)
        self.list_view.setItemDelegate(FileRowDelegate())
        self.list_view.setUniformItemSizes(True)
        self.list_view.setSelectionMode(QListView.ExtendedSelection)
        self.list_view.setStyleSheet("QListView { background: #fff; border: none; }")
        self.list_view.selectionModel().currentChanged.connect(self._on_selection)
        self.stack.addWidget(self.list_view)
        self.stack.setCurrentIndex(0)

        root.addWidget(center, 1)

        # ---- RIGHT: detail pane ----
        right = QFrame()
        right.setFixedWidth(380)
        right.setStyleSheet("background: #fbfbfc; border-left: 1px solid #e2e4e8;")
        self.detail = DetailPane()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(self.detail)
        root.addWidget(right)

        sb = QStatusBar()
        self.setStatusBar(sb)

    # ---- settings persistence ----

    def _restore_settings(self) -> None:
        mode = self.settings.value("mode", "paranoid")
        if mode in self.mode_radio:
            self.mode_radio[mode].setChecked(True)
        out = self.settings.value("output", "same")
        {"same": self.out_same, "custom": self.out_custom,
         "inplace": self.out_inplace}.get(out, self.out_same).setChecked(True)
        custom = self.settings.value("custom_dir", "")
        if custom:
            self.custom_dir_label.setText(custom)

    def _save_settings(self) -> None:
        for key, rb in self.mode_radio.items():
            if rb.isChecked():
                self.settings.setValue("mode", key)
                break
        if self.out_same.isChecked(): self.settings.setValue("output", "same")
        elif self.out_custom.isChecked(): self.settings.setValue("output", "custom")
        else: self.settings.setValue("output", "inplace")
        self.settings.setValue("custom_dir", self.custom_dir_label.text())

    def closeEvent(self, event):
        self._save_settings()
        super().closeEvent(event)

    # ---- file adding ----

    def _add_files_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择图片", "",
            "图片 (*.png *.jpg *.jpeg *.PNG *.JPG *.JPEG);;所有文件 (*)",
        )
        self._add_paths([Path(p) for p in paths])

    def _add_folder_dialog(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if not d:
            return
        root = Path(d)
        files = [p for p in root.rglob("*") if p.is_file() and p.suffix in SUPPORTED_EXTS]
        self._add_paths(files)

    def _pick_custom_dir(self, checked: bool) -> None:
        if not checked:
            return
        d = QFileDialog.getExistingDirectory(self, "选择输出文件夹")
        if d:
            self.custom_dir_label.setText(d)
        else:
            self.out_same.setChecked(True)

    def _add_paths(self, paths: list[Path]) -> None:
        paths = [p for p in paths if p.suffix in SUPPORTED_EXTS]
        if not paths:
            return
        new_entries = self.model.add_paths(paths)
        if new_entries:
            self.stack.setCurrentIndex(1)
            for entry in new_entries:
                self.model.update_status(str(entry.path), status=STATUS_INSPECTING)
                self._queue_inspect(entry.path)
                self._queue_thumb(entry.path)
            self._refresh_primary_button()

    def _on_clear(self) -> None:
        self.model.clear_all()
        self.stack.setCurrentIndex(0)
        self.detail.show_entry(None)
        self._refresh_primary_button()

    # ---- inspect ----

    def _queue_inspect(self, path: Path) -> None:
        task = InspectTask(str(path))
        task.signals.done.connect(self._on_inspect_done)
        task.signals.failed.connect(self._on_inspect_failed)
        self.thread_pool.start(task)

    def _on_inspect_done(self, path: str, report: InspectReport) -> None:
        badges = _summarize_badges(report)
        status = STATUS_CLEAN if report.is_clean else STATUS_DIRTY
        self.model.update_status(path, status=status, report=report, badges=badges)
        # If currently selected, refresh detail
        idx = self.list_view.currentIndex()
        if idx.isValid():
            cur = idx.data(FileListModel.EntryRole)
            if cur and str(cur.path) == path:
                self.detail.show_entry(cur)
        self._refresh_primary_button()

    def _on_inspect_failed(self, path: str, err: str) -> None:
        self.model.update_status(path, status=STATUS_FAILED, error=err)

    # ---- thumb ----

    def _queue_thumb(self, path: Path) -> None:
        # Generate thumbnail synchronously here using QImage (Pillow can't do it
        # off-main-thread cheaply for our purposes; QImage is small enough).
        # For very large images we still want this off-thread, so use a runnable.
        QTimer.singleShot(0, lambda p=path: self._make_thumb(p))

    def _make_thumb(self, path: Path) -> None:
        img = QImage(str(path))
        if img.isNull():
            return
        scaled = img.scaled(128, 128, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        pix = QPixmap.fromImage(scaled)
        self.model.update_status(str(path), thumb=pix)

    # ---- selection ----

    def _on_selection(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if not current.isValid():
            self.detail.show_entry(None)
            return
        entry = current.data(FileListModel.EntryRole)
        self.detail.show_entry(entry)

    # ---- primary button ----

    def _refresh_primary_button(self) -> None:
        entries = self.model.entries()
        if not entries:
            self.btn_clean.setText("添加图片")
            self.btn_clean.setEnabled(False)
            return
        if self.clean_worker and self.clean_worker.isRunning():
            self.btn_clean.setText("清理中… 点击取消")
            self.btn_clean.setEnabled(True)
            return
        to_clean = [e for e in entries if e.status in (STATUS_DIRTY, STATUS_CLEAN, STATUS_INSPECTING)]
        n = len(to_clean)
        if n == 0:
            self.btn_clean.setText("无可清理项")
            self.btn_clean.setEnabled(False)
        else:
            self.btn_clean.setText(f"清除 {n} 张图片")
            self.btn_clean.setEnabled(True)

    def _on_primary_clicked(self) -> None:
        if self.clean_worker and self.clean_worker.isRunning():
            self.clean_worker.cancel()
            self.statusBar().showMessage("已请求取消，当前文件完成后停止…", 3000)
            return
        self._start_batch()

    # ---- batch clean ----

    def _selected_mode(self) -> str:
        for key, rb in self.mode_radio.items():
            if rb.isChecked():
                return key
        return "safe"

    def _confirm_inplace(self, n: int) -> bool:
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle("确认原地替换")
        msg.setText(
            f"将原地替换 {n} 个文件，原文件无法恢复。\n"
            f"建议先备份。仍要继续吗？"
        )
        msg.setStandardButtons(QMessageBox.Cancel | QMessageBox.Ok)
        msg.button(QMessageBox.Ok).setText("我已备份，继续")
        msg.button(QMessageBox.Cancel).setText("取消")
        msg.setDefaultButton(QMessageBox.Cancel)
        return msg.exec() == QMessageBox.Ok

    def _confirm_nuclear_first_time(self) -> bool:
        if self.settings.value("nuclear_warned", False, type=bool):
            return True
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle("核弹模式提醒")
        msg.setText(
            "核弹模式会改变图像外观（裁剪、缩放、色彩微调），\n"
            "且无法保证清除所有鲁棒水印（如 Google SynthID）。\n"
            "继续吗？"
        )
        msg.setStandardButtons(QMessageBox.Cancel | QMessageBox.Ok)
        if msg.exec() != QMessageBox.Ok:
            return False
        self.settings.setValue("nuclear_warned", True)
        return True

    def _start_batch(self) -> None:
        mode = self._selected_mode()
        if mode == "nuclear" and not self._confirm_nuclear_first_time():
            return

        entries = [e for e in self.model.entries()
                   if e.status in (STATUS_DIRTY, STATUS_CLEAN, STATUS_INSPECTING)]
        if not entries:
            return

        in_place = self.out_inplace.isChecked()
        if in_place and not self._confirm_inplace(len(entries)):
            return

        # Resolve output paths.
        jobs: list[CleanJob] = []
        out_dir: Optional[Path] = None
        if self.out_custom.isChecked():
            cd = self.custom_dir_label.text()
            if not cd:
                QMessageBox.warning(self, "缺少输出文件夹", "请先选择自定义输出文件夹。")
                return
            out_dir = Path(cd)

        for e in entries:
            if in_place:
                out = e.path
            elif out_dir is not None:
                out = out_dir / e.path.name
            else:
                out = e.path.with_name(f"{e.path.stem}.cleaned{e.path.suffix}")
            jobs.append(CleanJob(src=e.path, mode=mode, out_path=out, in_place=in_place))

        self.progress.setVisible(True)
        self.progress.setMaximum(len(jobs))
        self.progress.setValue(0)

        self.clean_worker = CleanWorker(jobs, self)
        self.clean_worker.file_started.connect(self._on_clean_file_started)
        self.clean_worker.file_done.connect(self._on_clean_file_done)
        self.clean_worker.file_failed.connect(self._on_clean_file_failed)
        self.clean_worker.batch_done.connect(self._on_batch_done)
        self.clean_worker.start()
        self._refresh_primary_button()

    def _on_clean_file_started(self, src: str) -> None:
        self.model.update_status(src, status=STATUS_CLEANING)

    def _on_clean_file_done(self, src: str, out: str, post: InspectReport) -> None:
        self.model.update_status(
            src, status=STATUS_DONE if post.is_clean else STATUS_DIRTY,
            report=post, output=Path(out),
            badges=_summarize_badges(post),
        )
        self.progress.setValue(self.progress.value() + 1)

    def _on_clean_file_failed(self, src: str, err: str) -> None:
        self.model.update_status(src, status=STATUS_FAILED, error=err)
        self.progress.setValue(self.progress.value() + 1)

    def _on_batch_done(self, cleaned: int, total: int) -> None:
        self.progress.setVisible(False)
        self.statusBar().showMessage(f"完成：清除 {cleaned} / {total}", 8000)
        self._refresh_primary_button()
        # Skip the modal reveal dialog in headless / offscreen runs (smoke tests).
        if QApplication.instance().platformName() == "offscreen":
            return
        outputs = [e.output for e in self.model.entries() if e.output and e.output.exists()]
        if outputs:
            reveal_box = QMessageBox(self)
            reveal_box.setWindowTitle("完成")
            reveal_box.setText(f"已清除 {cleaned} / {total} 张。")
            reveal_box.setInformativeText(f"输出位置：{outputs[-1].parent}")
            reveal_btn = reveal_box.addButton("在 Finder 中显示", QMessageBox.ActionRole)
            reveal_box.addButton(QMessageBox.Ok)
            reveal_box.exec()
            if reveal_box.clickedButton() is reveal_btn:
                import subprocess
                subprocess.run(["open", "-R", str(outputs[-1])], check=False)

    # ---- drag & drop ----

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        urls = event.mimeData().urls()
        paths: list[Path] = []
        for u in urls:
            p = Path(u.toLocalFile())
            if p.is_dir():
                paths.extend(q for q in p.rglob("*") if q.is_file() and q.suffix in SUPPORTED_EXTS)
            elif p.is_file() and p.suffix in SUPPORTED_EXTS:
                paths.append(p)
        self._add_paths(paths)


# --------------------------------------------------------------------------- #
# Drop zone (empty-state view)                                                #
# --------------------------------------------------------------------------- #


class _DropZone(QWidget):
    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setAlignment(Qt.AlignCenter)
        title = QLabel("把图片或文件夹拖到这里")
        title.setStyleSheet("font-size: 18px; color: #666; font-weight: 600;")
        title.setAlignment(Qt.AlignCenter)
        sub = QLabel("支持 JPG · PNG    ·    或点击上方「+ 添加图片」")
        sub.setStyleSheet("color: #999;")
        sub.setAlignment(Qt.AlignCenter)
        v.addWidget(title)
        v.addWidget(sub)


def _section_label(text: str) -> QLabel:
    l = QLabel(text)
    l.setStyleSheet("color: #888; font-size: 11px; font-weight: 600; letter-spacing: 1px;")
    return l


def _hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setStyleSheet("color: #e2e4e8;")
    return f


def _summarize_badges(report: InspectReport) -> list[str]:
    cats = report.by_category()
    label_map = {
        "c2pa_manifest": "C2PA",
        "ai_generation_prompt": "AI",
        "ai_generation_tag": "AI",
        "gps_location": "GPS",
        "exif": "EXIF",
        "device_serial": "序列号",
        "xmp": "XMP",
        "iptc": "IPTC",
        "trailing_bytes": "尾部数据",
        "thumbnail": "缩略图",
        "png_text": "文本块",
        "png_chunk_unknown": "未知块",
    }
    out: list[str] = []
    for cat, _n in cats.items():
        lbl = label_map.get(cat)
        if lbl and lbl not in out:
            out.append(lbl)
    return out


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("imgclean")
    app.setOrganizationName("imgclean")
    app.setStyle("Fusion")
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
