#!/usr/bin/env python3
"""
remux_ui.py — Drag-and-drop MKV / AVI → MP4 batch converter.

Requirements:
    pip install PyQt6
    ffmpeg and ffprobe must be in PATH
    (or set FFMPEG_PATH / FFPROBE_PATH environment variables)
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from typing import Optional

from PyQt6.QtCore import Qt, QMimeData, QPoint, QSettings, QThread, pyqtSignal
from PyQt6.QtGui import (
    QColor, QDragEnterEvent, QDropEvent, QFont,
    QIcon, QPainter, QPixmap, QPolygon,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QHeaderView,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)

# ── Config ────────────────────────────────────────────────────────────────────

FFMPEG      = os.environ.get("FFMPEG_PATH",  "ffmpeg")
FFPROBE     = os.environ.get("FFPROBE_PATH", "ffprobe")
APPLE_AUDIO = {"aac", "alac", "mp3", "ac3"}
SUPPORTED   = {".mkv", ".avi"}

S_PENDING    = "⏳  Pending"
S_CONVERTING = "🔄  Converting…"
S_DONE       = "✅  Done"
S_ERROR      = "❌  Error"

STATUS_COLOR = {
    S_PENDING:    "#888",
    S_CONVERTING: "#4fc3f7",
    S_DONE:       "#81c784",
    S_ERROR:      "#e57373",
}

COL_INPUT  = 0
COL_OUTPUT = 1
COL_INFO   = 2
COL_STATUS = 3


# ── Helpers ───────────────────────────────────────────────────────────────────

def ffmpeg_available() -> bool:
    return bool(shutil.which(FFMPEG) and shutil.which(FFPROBE))


def notify(title: str, body: str) -> None:
    """macOS system notification; silently ignored on other platforms."""
    if platform.system() == "Darwin":
        subprocess.Popen(
            ["osascript", "-e", f'display notification "{body}" with title "{title}"']
        )


def default_dst(src: str, out_dir: Optional[str]) -> str:
    stem = os.path.splitext(src)[0]
    if out_dir:
        stem = os.path.join(out_dir, os.path.basename(stem))
    return stem + ".mp4"


# ── App icon (drawn at runtime — no external assets needed) ───────────────────

def make_icon(size: int = 256) -> QIcon:
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size

    # Dark rounded background
    p.setBrush(QColor("#1a1a2e"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(0, 0, s, s, int(s * 0.18), int(s * 0.18))

    # Film strip body
    sy, sh = int(s * 0.22), int(s * 0.56)
    p.setBrush(QColor("#2d2d2d"))
    p.drawRect(0, sy, s, sh)

    # Perforation holes — top and bottom rows
    pw   = int(s * 0.09)
    ph   = int(s * 0.075)
    pgap = int(s * 0.045)
    n    = 5
    ox   = (s - (n * pw + (n - 1) * pgap)) // 2
    p.setBrush(QColor("#1a1a2e"))
    for i in range(n):
        x = ox + i * (pw + pgap)
        p.drawRoundedRect(x, sy + int(s * 0.035),        pw, ph, 3, 3)
        p.drawRoundedRect(x, sy + sh - int(s * 0.035) - ph, pw, ph, 3, 3)

    # Three frame windows
    fy = sy + int(sh * 0.28)
    fh = int(sh * 0.44)
    fw = int(s * 0.225)
    fp = int(s * 0.04)
    fx0 = (s - 3 * fw - 2 * fp) // 2
    for i, color in enumerate(["#1565c0", "#0d6efd", "#4fc3f7"]):
        p.setBrush(QColor(color))
        p.drawRoundedRect(fx0 + i * (fw + fp), fy, fw, fh, 4, 4)

    # Play triangle on middle frame
    cx = fx0 + fw + fp + fw // 2
    cy = fy + fh // 2
    tw, th = int(fw * 0.45), int(fh * 0.5)
    p.setBrush(QColor("white"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawPolygon(QPolygon([
        QPoint(cx - tw // 3,      cy - th // 2),
        QPoint(cx - tw // 3,      cy + th // 2),
        QPoint(cx + int(tw * 0.67), cy),
    ]))

    p.end()
    return QIcon(px)


# ── Data model ────────────────────────────────────────────────────────────────

class QueueItem:
    def __init__(self, src: str, out_dir: Optional[str] = None):
        self.src    = src
        self.dst    = default_dst(src, out_dir)
        self.status = S_PENDING
        self.info   = ""    # populated by ProbeWorker
        self.row    = -1    # table row index


# ── Background workers ────────────────────────────────────────────────────────

class ProbeWorker(QThread):
    """Runs ffprobe on a file and emits a human-readable info string."""
    done = pyqtSignal(object, str)   # QueueItem, info

    def __init__(self, item: QueueItem):
        super().__init__()
        self.item = item

    def run(self) -> None:
        try:
            r = subprocess.run(
                [FFPROBE, "-v", "quiet", "-print_format", "json",
                 "-show_streams", "-show_format", self.item.src],
                capture_output=True, text=True, timeout=15,
            )
            data    = json.loads(r.stdout)
            streams = data.get("streams", [])
            fmt     = data.get("format", {})
            video   = next((s for s in streams if s["codec_type"] == "video"), {})
            audio   = next((s for s in streams if s["codec_type"] == "audio"), {})
            dur     = float(fmt.get("duration", 0))
            vc      = video.get("codec_name", "?").upper()
            ac      = audio.get("codec_name", "?").upper()
            w, h    = video.get("width", 0), video.get("height", 0)
            mins    = int(dur // 60)
            secs    = int(dur % 60)
            info    = f"{vc} / {ac}  •  {w}×{h}  •  {mins}m {secs:02d}s"
        except Exception as e:
            info = f"Probe failed: {e}"
        self.done.emit(self.item, info)


class ConvertWorker(QThread):
    """Runs ffmpeg to remux a single file, streaming progress."""
    progress = pyqtSignal(int)     # 0–100
    log      = pyqtSignal(str)
    finished = pyqtSignal(bool)

    def __init__(self, item: QueueItem, delete_original: bool):
        super().__init__()
        self.item            = item
        self.delete_original = delete_original

    def _probe(self):
        r = subprocess.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", self.item.src],
            capture_output=True, text=True,
        )
        data    = json.loads(r.stdout)
        streams = data.get("streams", [])
        fmt     = data.get("format", {})
        video   = next((s for s in streams if s["codec_type"] == "video"), {})
        audio   = next((s for s in streams if s["codec_type"] == "audio"), {})
        return (
            float(fmt.get("duration", 0)),
            video.get("codec_name"),
            audio.get("codec_name"),
        )

    def run(self) -> None:
        try:
            duration, video_codec, audio_codec = self._probe()
        except Exception as e:
            self.log.emit(f"ERROR probing: {e}")
            self.finished.emit(False)
            return

        ext             = os.path.splitext(self.item.src)[1].lower()
        needs_transcode = audio_codec not in APPLE_AUDIO

        video_args = ["-c:v", "copy"] + (["-tag:v", "hvc1"] if video_codec == "hevc" else [])
        audio_args = ["-c:a", "aac", "-b:a", "256k"] if needs_transcode else ["-c:a", "copy"]
        sub_args   = ["-sn"] if ext == ".avi" else ["-c:s", "mov_text"]

        self.log.emit(f"▶ {os.path.basename(self.item.src)}")
        self.log.emit(
            f"  Video : {video_codec or '?'}"
            + (" + hvc1 tag" if video_codec == "hevc" else "")
        )
        self.log.emit(
            f"  Audio : {audio_codec or '?'}"
            + (" → AAC 256k" if needs_transcode else " (copy)")
        )

        cmd = (
            [FFMPEG, "-i", self.item.src]
            + video_args + audio_args + sub_args
            + ["-progress", "pipe:1", "-nostats", "-y", self.item.dst]
        )

        proc   = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        dur_us = duration * 1_000_000

        for line in proc.stdout:
            line = line.strip()
            m = re.match(r"out_time_us=(\d+)", line)
            if m and dur_us > 0:
                self.progress.emit(min(int(int(m.group(1)) / dur_us * 100), 99))
            elif line == "progress=end":
                self.progress.emit(100)

        proc.wait()

        if proc.returncode != 0:
            self.log.emit("  ERROR: ffmpeg failed.\n")
            if os.path.exists(self.item.dst):
                os.remove(self.item.dst)
            self.finished.emit(False)
            return

        size_gb = os.path.getsize(self.item.dst) / 1024 ** 3
        self.log.emit(f"  → {os.path.basename(self.item.dst)}  ({size_gb:.2f} GB)")
        if self.delete_original:
            os.remove(self.item.src)
            self.log.emit("  Deleted original.")
        self.log.emit("")
        self.finished.emit(True)


# ── Drop zone ─────────────────────────────────────────────────────────────────

class DropZone(QLabel):
    files_dropped = pyqtSignal(list)

    _IDLE  = ("border:2px dashed #555;border-radius:12px;"
               "color:#888;font-size:14px;background:#1e1e1e;")
    _HOVER = ("border:2px dashed #4fc3f7;border-radius:12px;"
               "color:#4fc3f7;font-size:14px;background:#1a2a33;")

    def __init__(self):
        super().__init__("Drop MKV / AVI files here — or click to browse")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(90)
        self.setStyleSheet(self._IDLE)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _valid(self, mime: QMimeData) -> list:
        return [
            u.toLocalFile() for u in mime.urls()
            if os.path.splitext(u.toLocalFile())[1].lower() in SUPPORTED
        ]

    def dragEnterEvent(self, e: QDragEnterEvent):
        if self._valid(e.mimeData()):
            e.acceptProposedAction()
            self.setStyleSheet(self._HOVER)
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        self.setStyleSheet(self._IDLE)

    def dropEvent(self, e: QDropEvent):
        self.setStyleSheet(self._IDLE)
        paths = self._valid(e.mimeData())
        if paths:
            self.files_dropped.emit(paths)

    def mousePressEvent(self, e):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select video files", "",
            "Video files (*.mkv *.avi);;All files (*)",
        )
        if paths:
            self.files_dropped.emit(paths)


# ── Settings dialog ───────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    _KEY = "output_dir"

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(440)
        self._s = QSettings("Remux", "Remux")

        lay  = QVBoxLayout(self)
        form = QFormLayout()

        row = QHBoxLayout()
        self._dir = QLineEdit(self._s.value(self._KEY, ""))
        self._dir.setPlaceholderText("Same folder as source file (default)")
        browse = QPushButton("…")
        browse.setFixedWidth(32)
        browse.setObjectName("small")
        browse.clicked.connect(self._browse)
        row.addWidget(self._dir)
        row.addWidget(browse)
        form.addRow("Default output folder:", row)
        lay.addLayout(form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(
            self, "Select output folder",
            self._dir.text() or os.path.expanduser("~"),
        )
        if d:
            self._dir.setText(d)

    def _save(self):
        self._s.setValue(self._KEY, self._dir.text().strip())
        self.accept()

    @staticmethod
    def output_dir() -> Optional[str]:
        v = QSettings("Remux", "Remux").value(SettingsDialog._KEY, "")
        return v.strip() or None


# ── Main window ───────────────────────────────────────────────────────────────

class RemuxWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Remux")
        self.setMinimumWidth(700)
        self.setWindowIcon(make_icon())
        self.queue:    list[QueueItem]   = []
        self._probers: list[ProbeWorker] = []
        self.worker:   Optional[ConvertWorker] = None
        self._build_ui()
        self._apply_dark()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setSpacing(10)
        lay.setContentsMargins(18, 18, 18, 18)

        # Drop zone
        self.drop_zone = DropZone()
        self.drop_zone.files_dropped.connect(self._add_files)
        lay.addWidget(self.drop_zone)

        # Queue header
        hdr = QHBoxLayout()
        self.queue_lbl = QLabel("Queue  (0 files)")
        self.queue_lbl.setStyleSheet("color:#aaa;font-size:12px;")
        settings_btn = QPushButton("⚙  Settings")
        settings_btn.setObjectName("small")
        settings_btn.setFixedWidth(95)
        settings_btn.clicked.connect(lambda: SettingsDialog(self).exec())
        hdr.addWidget(self.queue_lbl)
        hdr.addStretch()
        hdr.addWidget(settings_btn)
        lay.addLayout(hdr)

        # Queue table
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Input file", "Output file", "Codec info", "Status"])
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(COL_INPUT,  QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(COL_OUTPUT, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(COL_INFO,   QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked |
            QAbstractItemView.EditTrigger.SelectedClicked
        )
        self.table.setFixedHeight(190)
        self.table.verticalHeader().setVisible(False)
        self.table.setFont(QFont("Menlo", 11))
        self.table.itemChanged.connect(self._on_cell_edited)
        self.table.itemSelectionChanged.connect(self._on_selection)
        lay.addWidget(self.table)

        # Table action buttons
        tbl_row = QHBoxLayout()
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.setObjectName("small")
        self.remove_btn.setEnabled(False)
        self.remove_btn.clicked.connect(self._remove_selected)
        self.clear_btn = QPushButton("Clear done")
        self.clear_btn.setObjectName("small")
        self.clear_btn.clicked.connect(self._clear_done)
        self.open_btn = QPushButton("📂  Open output folder")
        self.open_btn.setObjectName("small")
        self.open_btn.setVisible(False)
        self.open_btn.clicked.connect(self._open_output_folder)
        tbl_row.addWidget(self.remove_btn)
        tbl_row.addWidget(self.clear_btn)
        tbl_row.addStretch()
        tbl_row.addWidget(self.open_btn)
        lay.addLayout(tbl_row)

        # Info bar (shows codec details for selected row)
        self.info_bar = QLabel("")
        self.info_bar.setStyleSheet("color:#666;font-size:11px;font-family:Menlo;")
        lay.addWidget(self.info_bar)

        # Options + convert
        bot = QHBoxLayout()
        self.delete_chk = QCheckBox("Delete originals after conversion")
        self.convert_btn = QPushButton("Convert All")
        self.convert_btn.setEnabled(False)
        self.convert_btn.setFixedHeight(36)
        self.convert_btn.setMinimumWidth(130)
        self.convert_btn.clicked.connect(self._start_queue)
        bot.addWidget(self.delete_chk)
        bot.addStretch()
        bot.addWidget(self.convert_btn)
        lay.addLayout(bot)

        # Progress row
        prog_row = QHBoxLayout()
        self.cur_lbl = QLabel("")
        self.cur_lbl.setStyleSheet("color:#777;font-size:11px;")
        self.cur_lbl.setMinimumWidth(200)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedHeight(16)
        prog_row.addWidget(self.cur_lbl, 1)
        prog_row.addWidget(self.progress, 2)
        lay.addLayout(prog_row)

        # Log
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(130)
        self.log.setFont(QFont("Menlo", 11))
        lay.addWidget(self.log)

    def _apply_dark(self):
        self.setStyleSheet("""
            QMainWindow, QWidget  { background:#141414; color:#e0e0e0; }
            QTableWidget {
                background:#1e1e1e; border:1px solid #333;
                border-radius:6px; color:#e0e0e0; gridline-color:#2a2a2a;
            }
            QTableWidget::item:selected { background:#2a3a4a; }
            QHeaderView::section {
                background:#1a1a1a; color:#777; border:none;
                border-bottom:1px solid #333; padding:4px; font-size:11px;
            }
            QPushButton {
                background:#0d6efd; border:none; border-radius:6px;
                color:white; font-size:13px; font-weight:bold; padding:4px 14px;
            }
            QPushButton:hover    { background:#3385ff; }
            QPushButton:disabled { background:#2a2a2a; color:#555; }
            QPushButton#small {
                background:#2a2a2a; font-size:11px;
                font-weight:normal; padding:3px 10px; border-radius:5px;
            }
            QPushButton#small:hover { background:#383838; }
            QProgressBar {
                background:#2a2a2a; border:1px solid #444; border-radius:4px;
                text-align:center; color:#ccc; font-size:10px;
            }
            QProgressBar::chunk { background:#0d6efd; border-radius:4px; }
            QTextEdit {
                background:#1a1a1a; border:1px solid #333;
                border-radius:6px; color:#a8d8a8;
            }
            QCheckBox { color:#aaa; }
            QLabel    { color:#ccc; }
            QLineEdit {
                background:#2a2a2a; border:1px solid #444;
                border-radius:6px; padding:4px 8px; color:#e0e0e0;
            }
            QDialog   { background:#141414; color:#e0e0e0; }
        """)

    # ── Queue management ──────────────────────────────────────────────────────

    def _add_files(self, paths: list):
        existing = {item.src for item in self.queue}
        out_dir  = SettingsDialog.output_dir()
        for path in paths:
            if path in existing:
                continue
            item     = QueueItem(path, out_dir)
            item.row = len(self.queue)
            self.queue.append(item)
            self._insert_row(item)
            self._probe_async(item)
        self._refresh_header()
        self.convert_btn.setEnabled(True)

    def _insert_row(self, item: QueueItem):
        r = self.table.rowCount()
        self.table.insertRow(r)

        in_cell = QTableWidgetItem(os.path.basename(item.src))
        in_cell.setFlags(in_cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
        in_cell.setToolTip(item.src)
        self.table.setItem(r, COL_INPUT, in_cell)

        out_cell = QTableWidgetItem(os.path.basename(item.dst))
        out_cell.setToolTip(item.dst)
        self.table.setItem(r, COL_OUTPUT, out_cell)

        info_cell = QTableWidgetItem("probing…")
        info_cell.setFlags(info_cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
        info_cell.setForeground(QColor("#555"))
        self.table.setItem(r, COL_INFO, info_cell)

        self._set_status_cell(r, item.status)

    def _set_status_cell(self, row: int, status: str):
        cell = QTableWidgetItem(status)
        cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
        cell.setForeground(QColor(STATUS_COLOR.get(status, "#888")))
        self.table.setItem(row, COL_STATUS, cell)

    def _probe_async(self, item: QueueItem):
        w = ProbeWorker(item)
        w.done.connect(self._on_probe_done)
        self._probers.append(w)
        w.start()

    def _on_probe_done(self, item: QueueItem, info: str):
        item.info = info
        cell = self.table.item(item.row, COL_INFO)
        if cell:
            self.table.blockSignals(True)
            cell.setText(info)
            cell.setForeground(QColor("#777"))
            self.table.blockSignals(False)
        # Refresh info bar if this row is selected
        sel = self.table.selectedItems()
        if sel and sel[0].row() == item.row:
            self._show_info(item)

    def _on_cell_edited(self, cell: QTableWidgetItem):
        if cell.column() != COL_OUTPUT:
            return
        row = cell.row()
        if row >= len(self.queue):
            return
        item = self.queue[row]
        text = cell.text().strip()
        if not text:
            return
        # If user typed a bare filename (no path separator), keep existing dir
        if os.sep not in text and "/" not in text:
            text = os.path.join(os.path.dirname(item.dst), text)
        if not text.endswith(".mp4"):
            text += ".mp4"
        item.dst = text
        self.table.blockSignals(True)
        cell.setText(os.path.basename(item.dst))
        cell.setToolTip(item.dst)
        self.table.blockSignals(False)

    def _on_selection(self):
        sel = self.table.selectedItems()
        self.remove_btn.setEnabled(bool(sel))
        if not sel:
            self.info_bar.setText("")
            return
        row = sel[0].row()
        if row < len(self.queue):
            self._show_info(self.queue[row])

    def _show_info(self, item: QueueItem):
        parts = [item.src]
        if item.info:
            parts.append(item.info)
        self.info_bar.setText("  " + "  •  ".join(parts))

    def _remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedItems()}, reverse=True)
        for row in rows:
            if self.queue[row].status != S_CONVERTING:
                self.queue.pop(row)
                self.table.removeRow(row)
        for i, item in enumerate(self.queue):
            item.row = i
        self._refresh_header()
        if not self.queue:
            self.convert_btn.setEnabled(False)

    def _clear_done(self):
        for row in range(len(self.queue) - 1, -1, -1):
            if self.queue[row].status in (S_DONE, S_ERROR):
                self.queue.pop(row)
                self.table.removeRow(row)
        for i, item in enumerate(self.queue):
            item.row = i
        self._refresh_header()
        if not self.queue:
            self.convert_btn.setEnabled(False)

    def _refresh_header(self):
        n = len(self.queue)
        self.queue_lbl.setText(f"Queue  ({n} file{'s' if n != 1 else ''})")

    # ── Conversion ────────────────────────────────────────────────────────────

    def _start_queue(self):
        self.convert_btn.setEnabled(False)
        self.open_btn.setVisible(False)
        self._process_next()

    def _process_next(self):
        for item in self.queue:
            if item.status == S_PENDING:
                self._convert(item)
                return
        # Batch finished
        self.cur_lbl.setText("")
        self.progress.setValue(0)
        done  = sum(1 for i in self.queue if i.status == S_DONE)
        total = len(self.queue)
        self._append_log(f"Batch complete — {done}/{total} succeeded.")
        if done:
            self.open_btn.setVisible(True)
            notify("Remux", f"Batch complete — {done}/{total} file{'s' if done != 1 else ''} converted.")
        self.convert_btn.setEnabled(any(i.status == S_PENDING for i in self.queue))

    def _convert(self, item: QueueItem):
        item.status = S_CONVERTING
        self._set_status_cell(item.row, item.status)
        self.table.scrollToItem(self.table.item(item.row, 0))

        fname = os.path.basename(item.src)
        self.cur_lbl.setText(fname if len(fname) <= 50 else fname[:48] + "…")
        self.progress.setValue(0)

        self.worker = ConvertWorker(item, self.delete_chk.isChecked())
        self.worker.progress.connect(self.progress.setValue)
        self.worker.log.connect(self._append_log)
        self.worker.finished.connect(lambda ok, it=item: self._on_done(ok, it))
        self.worker.start()

    def _on_done(self, success: bool, item: QueueItem):
        item.status = S_DONE if success else S_ERROR
        self._set_status_cell(item.row, item.status)
        self._process_next()

    # ── Output folder ─────────────────────────────────────────────────────────

    def _open_output_folder(self):
        done = [i for i in self.queue if i.status == S_DONE]
        if done:
            subprocess.Popen(["open", os.path.dirname(done[-1].dst)])

    # ── Log ───────────────────────────────────────────────────────────────────

    def _append_log(self, text: str):
        self.log.append(text)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Remux")
    app.setWindowIcon(make_icon())

    if not ffmpeg_available():
        msg = QMessageBox()
        msg.setWindowTitle("ffmpeg not found")
        msg.setIcon(QMessageBox.Icon.Critical)
        msg.setText(
            "<b>ffmpeg / ffprobe could not be found.</b><br><br>"
            "Remux requires ffmpeg to convert video files.<br><br>"
            "Install via Homebrew:<br>"
            "<code style='background:#222;padding:2px 6px;border-radius:3px;'>"
            "brew install ffmpeg</code><br><br>"
            "Or set the <code>FFMPEG_PATH</code> and <code>FFPROBE_PATH</code> "
            "environment variables if ffmpeg lives elsewhere."
        )
        msg.exec()
        sys.exit(1)

    win = RemuxWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
