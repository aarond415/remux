#!/usr/bin/env python3
"""
remux_ui.py — Drag-and-drop MKV / AVI → MP4 batch converter with optional TMDb tagging.

Requirements:
    pip install PyQt6
    ffmpeg and ffprobe in PATH (or FFMPEG_PATH / FFPROBE_PATH env vars)
    SublerCLI for tagging: https://bitbucket.org/galad87/sublercli
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from typing import Optional

from PyQt6.QtCore import Qt, QMimeData, QPoint, QSettings, QThread, pyqtSignal
from PyQt6.QtGui import (
    QColor, QDragEnterEvent, QDropEvent, QFont,
    QIcon, QPainter, QPixmap, QPolygon,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout,
    QHeaderView, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)

# ── Constants ─────────────────────────────────────────────────────────────────

FFMPEG         = os.environ.get("FFMPEG_PATH",  "ffmpeg")
FFPROBE        = os.environ.get("FFPROBE_PATH", "ffprobe")
SUBLER_DEFAULT = "/opt/homebrew/bin/SublerCLI"
APPLE_AUDIO    = {"aac", "alac", "mp3", "ac3"}
SUPPORTED      = {".mkv", ".avi"}
TMDB_BASE      = "https://api.themoviedb.org/3"
TMDB_IMG       = "https://image.tmdb.org/t/p"

S_PENDING    = "⏳  Pending"
S_CONVERTING = "🔄  Converting…"
S_FETCHING   = "🔍  Fetching metadata…"
S_ARTWORK    = "🎨  Pick artwork"
S_TAGGING    = "🏷   Tagging…"
S_DONE       = "✅  Done"
S_ERROR      = "❌  Error"

STATUS_COLOR = {
    S_PENDING:    "#888",
    S_CONVERTING: "#4fc3f7",
    S_FETCHING:   "#4fc3f7",
    S_ARTWORK:    "#ffb74d",
    S_TAGGING:    "#4fc3f7",
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
    if platform.system() == "Darwin":
        subprocess.Popen(["osascript", "-e", f'display notification "{body}" with title "{title}"'])


def default_dst(src: str, out_dir: Optional[str]) -> str:
    stem = os.path.splitext(src)[0]
    if out_dir:
        stem = os.path.join(out_dir, os.path.basename(stem))
    return stem + ".mp4"


def tmdb_get(path: str, params: dict, key: str) -> dict:
    p = dict(params)
    p["api_key"] = key
    url = f"{TMDB_BASE}{path}?" + urllib.parse.urlencode(p)
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def parse_movie_title(filename: str):
    """Return (title, year) from a movie filename."""
    stem = os.path.splitext(filename)[0]
    m = re.match(r"^(.+?)\s*[\(\[]?(\d{4})[\)\]]?", stem)
    if m:
        return re.sub(r"[._]", " ", m.group(1)).strip(), m.group(2)
    return re.sub(r"[._]", " ", stem).strip(), None


def parse_tv_filename(filename: str):
    """Return (show_title, year, season, episode) from a TV episode filename."""
    stem = os.path.splitext(filename)[0]
    m_ep = re.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", stem)
    if not m_ep:
        return None, None, None, None
    season  = int(m_ep.group(1))
    episode = int(m_ep.group(2))
    show_part = stem[:m_ep.start()].strip(" .-_")
    m_yr = re.match(r"^(.+?)\s*[\(\[]?(\d{4})[\)\]]?\s*$", show_part)
    if m_yr:
        title = re.sub(r"[._-]", " ", m_yr.group(1)).strip()
        year  = m_yr.group(2)
    else:
        title = re.sub(r"[._-]", " ", show_part).strip()
        year  = None
    return title, year, season, episode


# ── App icon ──────────────────────────────────────────────────────────────────

def make_icon(size: int = 256) -> QIcon:
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size

    p.setBrush(QColor("#1a1a2e"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(0, 0, s, s, int(s * 0.18), int(s * 0.18))

    sy, sh = int(s * 0.22), int(s * 0.56)
    p.setBrush(QColor("#2d2d2d"))
    p.drawRect(0, sy, s, sh)

    pw, ph, pgap, n = int(s * 0.09), int(s * 0.075), int(s * 0.045), 5
    ox = (s - (n * pw + (n - 1) * pgap)) // 2
    p.setBrush(QColor("#1a1a2e"))
    for i in range(n):
        x = ox + i * (pw + pgap)
        p.drawRoundedRect(x, sy + int(s * 0.035), pw, ph, 3, 3)
        p.drawRoundedRect(x, sy + sh - int(s * 0.035) - ph, pw, ph, 3, 3)

    fy, fh, fw, fp = sy + int(sh * 0.28), int(sh * 0.44), int(s * 0.225), int(s * 0.04)
    fx0 = (s - 3 * fw - 2 * fp) // 2
    for i, color in enumerate(["#1565c0", "#0d6efd", "#4fc3f7"]):
        p.setBrush(QColor(color))
        p.drawRoundedRect(fx0 + i * (fw + fp), fy, fw, fh, 4, 4)

    cx, cy = fx0 + fw + fp + fw // 2, fy + fh // 2
    tw, th = int(fw * 0.45), int(fh * 0.5)
    p.setBrush(QColor("white"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawPolygon(QPolygon([
        QPoint(cx - tw // 3, cy - th // 2),
        QPoint(cx - tw // 3, cy + th // 2),
        QPoint(cx + int(tw * 0.67), cy),
    ]))
    p.end()
    return QIcon(px)


# ── Data model ────────────────────────────────────────────────────────────────

class QueueItem:
    def __init__(self, src: str, out_dir: Optional[str] = None):
        self.src       = src
        self.dst       = default_dst(src, out_dir)
        self.status    = S_PENDING
        self.info      = ""
        self.row       = -1
        self.tmdb_tags: dict = {}


# ── Workers ───────────────────────────────────────────────────────────────────

class ProbeWorker(QThread):
    done = pyqtSignal(object, str)

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
            data  = json.loads(r.stdout)
            strs  = data.get("streams", [])
            fmt   = data.get("format", {})
            video = next((s for s in strs if s["codec_type"] == "video"), {})
            audio = next((s for s in strs if s["codec_type"] == "audio"), {})
            dur   = float(fmt.get("duration", 0))
            vc, ac = video.get("codec_name", "?").upper(), audio.get("codec_name", "?").upper()
            w, h   = video.get("width", 0), video.get("height", 0)
            info   = f"{vc} / {ac}  •  {w}×{h}  •  {int(dur//60)}m {int(dur%60):02d}s"
        except Exception as e:
            info = f"Probe failed: {e}"
        self.done.emit(self.item, info)


class ConvertWorker(QThread):
    progress = pyqtSignal(int)
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
        data  = json.loads(r.stdout)
        strs  = data.get("streams", [])
        fmt   = data.get("format", {})
        video = next((s for s in strs if s["codec_type"] == "video"), {})
        audio = next((s for s in strs if s["codec_type"] == "audio"), {})
        TEXT_SUBS = {"subrip", "ass", "ssa", "webvtt", "mov_text"}
        has_image_subs = any(
            s.get("codec_type") == "subtitle" and s.get("codec_name") not in TEXT_SUBS
            for s in strs
        )
        return float(fmt.get("duration", 0)), video.get("codec_name"), audio.get("codec_name"), has_image_subs

    def run(self) -> None:
        try:
            duration, vc, ac, has_image_subs = self._probe()
        except Exception as e:
            self.log.emit(f"ERROR probing: {e}")
            self.finished.emit(False)
            return

        ext       = os.path.splitext(self.item.src)[1].lower()
        is_avi    = ext == ".avi"
        drop_subs = is_avi or has_image_subs

        video_args = ["-c:v", "copy"] + (["-tag:v", "hvc1"] if vc == "hevc" else [])
        # AVI MP3 uses a broken codec tag (0x0055) that doesn't survive remux to MP4 cleanly;
        # always re-encode audio from AVI files.
        need_reencode = ac not in APPLE_AUDIO or is_avi
        audio_args = ["-c:a", "aac", "-b:a", "256k"] if need_reencode else ["-c:a", "copy"]
        sub_args   = ["-sn"] if drop_subs else ["-c:s", "mov_text"]

        self.log.emit(f"▶ {os.path.basename(self.item.src)}")
        self.log.emit(f"  Video : {vc or '?'}" + (" + hvc1 tag" if vc == "hevc" else ""))
        self.log.emit(f"  Audio : {ac or '?'}" + (" → AAC 256k" if need_reencode else " (copy)"))
        if drop_subs:
            self.log.emit("  Subs  : dropped (image-based subtitles can't go into MP4)")

        cmd  = [FFMPEG, "-i", self.item.src] + video_args + audio_args + sub_args + ["-progress", "pipe:1", "-nostats", "-y", self.item.dst]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
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

        self.log.emit(f"  → {os.path.basename(self.item.dst)}  ({os.path.getsize(self.item.dst)/1024**3:.2f} GB)")
        if self.delete_original:
            os.remove(self.item.src)
            self.log.emit("  Deleted original.")
        self.log.emit("")
        self.finished.emit(True)


class TagFetchWorker(QThread):
    """Searches TMDb, builds tag dict, downloads poster thumbnails."""
    done = pyqtSignal(object, dict, list, str, str)
    # item, tags, [(QPixmap, full_url)], match_title, error

    def __init__(self, item: QueueItem, key: str, media_type: str,
                 title_override: Optional[str] = None):
        super().__init__()
        self.item           = item
        self.key            = key
        self.media_type     = media_type      # "movie" or "tv"
        self.title_override = title_override

    def _thumb(self, path: str) -> QPixmap:
        url = f"{TMDB_IMG}/w92{path}"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = r.read()
        px = QPixmap()
        px.loadFromData(data)
        return px

    def _fetch_movie(self):
        fname = os.path.basename(self.item.src)
        if self.title_override:
            title, year = self.title_override, None
        else:
            title, year = parse_movie_title(fname)

        params = {"query": title, "language": "en-US"}
        if year:
            params["year"] = year
        results = tmdb_get("/search/movie", params, self.key).get("results", [])
        if not results:
            raise Exception(f"No TMDb match for '{title}'")

        mid    = results[0]["id"]
        detail = tmdb_get(f"/movie/{mid}", {"append_to_response": "credits"}, self.key)
        images = tmdb_get(f"/movie/{mid}/images", {"include_image_language": "en,null"}, self.key)

        name      = detail.get("title", title)
        release   = detail.get("release_date", "")
        genres    = ", ".join(g["name"] for g in detail.get("genres", [])[:2])
        credits   = detail.get("credits", {})
        cast      = ", ".join(a["name"] for a in credits.get("cast", [])[:5])
        directors = ", ".join(c["name"] for c in credits.get("crew", []) if c["job"] == "Director")

        tags = {
            "Name":         name,
            "Description":  detail.get("overview", ""),
            "Release Date": release,
            "Genre":        genres,
            "Cast":         cast,
            "Director":     directors,
            "Media Kind":   "Movie",
        }

        posters = [p["file_path"] for p in images.get("posters", [])]
        if detail.get("poster_path") and detail["poster_path"] not in posters:
            posters.insert(0, detail["poster_path"])

        return tags, posters, f"{name} ({release[:4]})" if release else name

    def _fetch_tv(self):
        fname = os.path.basename(self.item.src)
        show_title, year, season, episode = parse_tv_filename(fname)
        if season is None:
            raise Exception("Could not parse SxxExx from filename")

        search_title = self.title_override or show_title
        params = {"query": search_title, "language": "en-US"}
        if year and not self.title_override:
            params["first_air_date_year"] = year
        results = tmdb_get("/search/tv", params, self.key).get("results", [])
        if not results:
            raise Exception(f"No TMDb match for '{search_title}'")

        sid    = results[0]["id"]
        detail = tmdb_get(f"/tv/{sid}", {"append_to_response": "credits"}, self.key)

        try:
            ep = tmdb_get(f"/tv/{sid}/season/{season}/episode/{episode}", {}, self.key)
        except Exception:
            ep = {}

        show_name = detail.get("name", search_title)
        genres    = ", ".join(g["name"] for g in detail.get("genres", [])[:2])
        cast      = ", ".join(a["name"] for a in detail.get("credits", {}).get("cast", [])[:5])
        ep_id     = str(season * 100 + episode)

        tags = {
            "Name":          ep.get("name", f"Episode {episode}"),
            "TV Show":       show_name,
            "TV Season":     str(season),
            "TV Episode #":  str(episode),
            "TV Episode ID": ep_id,
            "Album":         f"{show_name}, Season {season}",
            "Album Artist":  show_name,
            "Artist":        show_name,
            "Track #":       f"{episode}/0",
            "Description":   ep.get("overview", ""),
            "Release Date":  ep.get("air_date", ""),
            "Genre":         genres,
            "Cast":          cast,
            "Media Kind":    "10",
        }

        # Season posters first, then show posters
        try:
            s_imgs = tmdb_get(f"/tv/{sid}/season/{season}/images", {}, self.key)
            posters = [p["file_path"] for p in s_imgs.get("posters", [])]
        except Exception:
            posters = []

        sh_imgs = tmdb_get(f"/tv/{sid}/images", {"include_image_language": "en,null"}, self.key)
        for p in sh_imgs.get("posters", []):
            if p["file_path"] not in posters:
                posters.append(p["file_path"])

        match_title = f"{show_name}  S{season:02d}E{episode:02d}"
        return tags, posters, match_title

    def run(self) -> None:
        try:
            if self.media_type == "movie":
                tags, posters, match_title = self._fetch_movie()
            else:
                tags, posters, match_title = self._fetch_tv()

            thumbs = []
            for path in posters[:16]:
                try:
                    thumbs.append((self._thumb(path), f"{TMDB_IMG}/w500{path}"))
                except Exception:
                    pass

            self.done.emit(self.item, tags, thumbs, match_title, "")
        except Exception as e:
            self.done.emit(self.item, {}, [], "", str(e))


class TagWriteWorker(QThread):
    """Downloads selected artwork and writes tags via SublerCLI."""
    log      = pyqtSignal(str)
    finished = pyqtSignal(bool)

    def __init__(self, item: QueueItem, tags: dict, artwork_url: Optional[str], subler: str):
        super().__init__()
        self.item        = item
        self.tags        = tags
        self.artwork_url = artwork_url
        self.subler      = subler

    def run(self) -> None:
        artwork_path = None
        try:
            if self.artwork_url:
                tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                with urllib.request.urlopen(self.artwork_url, timeout=15) as r:
                    tmp.write(r.read())
                tmp.close()
                artwork_path = tmp.name

            all_tags = dict(self.tags)
            if artwork_path:
                all_tags["Artwork"] = artwork_path

            meta_str = "".join(f"{{{k}:{v}}}" for k, v in all_tags.items() if v)
            result   = subprocess.run(
                [self.subler, "-dest", self.item.dst, "-metadata", meta_str],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                self.log.emit(f"  ERROR (SublerCLI): {result.stderr.strip()[:200]}")
                self.finished.emit(False)
                return

            self.log.emit("  Tags written successfully.\n")
            self.finished.emit(True)
        except Exception as e:
            self.log.emit(f"  ERROR: {e}")
            self.finished.emit(False)
        finally:
            if artwork_path and os.path.exists(artwork_path):
                os.unlink(artwork_path)


# ── Artwork picker ────────────────────────────────────────────────────────────

class ThumbLabel(QLabel):
    clicked = pyqtSignal(int)

    _NORMAL   = "border:2px solid transparent;border-radius:4px;"
    _SELECTED = "border:2px solid #0d6efd;border-radius:4px;"

    def __init__(self, idx: int, pixmap: QPixmap, url: str):
        super().__init__()
        self.idx = idx
        self.url = url
        self.setFixedSize(92, 138)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(self._NORMAL)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if not pixmap.isNull():
            self.setPixmap(pixmap.scaled(88, 134, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation))
        else:
            self.setText("?")

    def set_selected(self, sel: bool):
        self.setStyleSheet(self._SELECTED if sel else self._NORMAL)

    def mousePressEvent(self, e):
        self.clicked.emit(self.idx)


class ArtworkPickerDialog(QDialog):
    def __init__(self, parent, item: QueueItem, tags: dict,
                 thumbs: list, match_title: str, key: str, media_type: str):
        super().__init__(parent)
        self.setWindowTitle("Select Artwork")
        self.setMinimumSize(640, 520)
        self._item        = item
        self._tags        = tags
        self._key         = key
        self._media_type  = media_type
        self._thumbs      = thumbs
        self._sel_idx     = 0
        self._thumb_lbls: list[ThumbLabel] = []
        self.selected_url: Optional[str]   = thumbs[0][1] if thumbs else None
        self._fetch_worker: Optional[TagFetchWorker] = None
        self._build_ui(tags, thumbs, match_title)
        self._apply_dark()

    def _build_ui(self, tags: dict, thumbs: list, match_title: str):
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(16, 16, 16, 16)

        # Match info + re-search row
        search_row = QHBoxLayout()
        self._match_lbl = QLabel(f"<b>{match_title}</b>")
        self._match_lbl.setStyleSheet("color:#e0e0e0;font-size:13px;")
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Wrong match? Type a title and search again…")
        self._search_edit.returnPressed.connect(self._research)
        self._search_btn = QPushButton("Search")
        self._search_btn.setObjectName("small")
        self._search_btn.setFixedWidth(70)
        self._search_btn.clicked.connect(self._research)
        search_row.addWidget(self._match_lbl, 1)
        search_row.addWidget(self._search_edit, 2)
        search_row.addWidget(self._search_btn)
        lay.addLayout(search_row)

        # Subtitle (genre / year)
        year  = (tags.get("Release Date") or tags.get("air_date") or "")[:4]
        genre = tags.get("Genre", "")
        sub   = "  •  ".join(x for x in [year, genre] if x)
        if sub:
            sub_lbl = QLabel(sub)
            sub_lbl.setStyleSheet("color:#777;font-size:11px;")
            lay.addWidget(sub_lbl)

        # Thumbnail grid in scroll area
        self._scroll_widget = QWidget()
        self._grid = QGridLayout(self._scroll_widget)
        self._grid.setSpacing(8)
        self._grid.setContentsMargins(4, 4, 4, 4)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._scroll_widget)
        scroll.setStyleSheet("QScrollArea{border:1px solid #333;border-radius:6px;}")
        lay.addWidget(scroll, 1)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color:#888;font-size:11px;")
        lay.addWidget(self._status_lbl)

        # Buttons
        btn_row = QHBoxLayout()
        skip_btn = QPushButton("Skip tagging")
        skip_btn.setObjectName("small")
        skip_btn.clicked.connect(self.reject)
        self._confirm_btn = QPushButton("Use selected image")
        self._confirm_btn.clicked.connect(self._confirm)
        btn_row.addWidget(skip_btn)
        btn_row.addStretch()
        btn_row.addWidget(self._confirm_btn)
        lay.addLayout(btn_row)

        self._populate_grid(thumbs)

    def _populate_grid(self, thumbs: list):
        # Clear existing
        for lbl in self._thumb_lbls:
            lbl.deleteLater()
        self._thumb_lbls.clear()
        self._sel_idx = 0

        if not thumbs:
            self._status_lbl.setText("No posters found for this title.")
            self._confirm_btn.setEnabled(False)
            self.selected_url = None
            return

        self._confirm_btn.setEnabled(True)
        self.selected_url = thumbs[0][1]

        for i, (px, url) in enumerate(thumbs):
            lbl = ThumbLabel(i, px, url)
            lbl.clicked.connect(self._select)
            self._grid.addWidget(lbl, i // 4, i % 4)
            self._thumb_lbls.append(lbl)

        if self._thumb_lbls:
            self._thumb_lbls[0].set_selected(True)

    def _select(self, idx: int):
        for i, lbl in enumerate(self._thumb_lbls):
            lbl.set_selected(i == idx)
        self._sel_idx    = idx
        self.selected_url = self._thumb_lbls[idx].url

    def _research(self):
        override = self._search_edit.text().strip()
        if not override:
            return
        self._search_btn.setEnabled(False)
        self._status_lbl.setText("Searching…")
        self._fetch_worker = TagFetchWorker(self._item, self._key, self._media_type, override)
        self._fetch_worker.done.connect(self._on_research_done)
        self._fetch_worker.start()

    def _on_research_done(self, item, tags, thumbs, match_title, error):
        self._search_btn.setEnabled(True)
        if error:
            self._status_lbl.setText(f"No match: {error}")
            return
        self._tags = tags
        self._match_lbl.setText(f"<b>{match_title}</b>")
        self._status_lbl.setText("")
        self._populate_grid(thumbs)

    def _confirm(self):
        self.accept()

    def _apply_dark(self):
        self.setStyleSheet("""
            QDialog, QWidget { background:#141414; color:#e0e0e0; }
            QScrollArea { background:#1e1e1e; }
            QLineEdit {
                background:#2a2a2a; border:1px solid #444;
                border-radius:5px; padding:3px 7px; color:#e0e0e0;
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
        """)


# ── Drop zone ─────────────────────────────────────────────────────────────────

class DropZone(QLabel):
    files_dropped = pyqtSignal(list)

    _IDLE  = "border:2px dashed #555;border-radius:12px;color:#888;font-size:14px;background:#1e1e1e;"
    _HOVER = "border:2px dashed #4fc3f7;border-radius:12px;color:#4fc3f7;font-size:14px;background:#1a2a33;"

    def __init__(self):
        super().__init__("Drop MKV / AVI files here — or click to browse")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(90)
        self.setStyleSheet(self._IDLE)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _valid(self, mime: QMimeData) -> list:
        return [u.toLocalFile() for u in mime.urls()
                if os.path.splitext(u.toLocalFile())[1].lower() in SUPPORTED]

    def dragEnterEvent(self, e: QDragEnterEvent):
        if self._valid(e.mimeData()):
            e.acceptProposedAction(); self.setStyleSheet(self._HOVER)
        else:
            e.ignore()

    def dragLeaveEvent(self, e): self.setStyleSheet(self._IDLE)

    def dropEvent(self, e: QDropEvent):
        self.setStyleSheet(self._IDLE)
        paths = self._valid(e.mimeData())
        if paths: self.files_dropped.emit(paths)

    def mousePressEvent(self, e):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select video files", "",
            "Video files (*.mkv *.avi);;All files (*)",
        )
        if paths: self.files_dropped.emit(paths)


# ── Settings dialog ───────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)
        self._s = QSettings("Remux", "Remux")

        lay  = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)

        # Output folder
        dir_row = QHBoxLayout()
        self._dir = QLineEdit(self._s.value("output_dir", ""))
        self._dir.setPlaceholderText("Same folder as source file (default)")
        b1 = QPushButton("…"); b1.setFixedWidth(32); b1.setObjectName("small")
        b1.clicked.connect(self._browse_dir)
        dir_row.addWidget(self._dir); dir_row.addWidget(b1)
        form.addRow("Default output folder:", dir_row)

        # TMDb API key
        self._tmdb = QLineEdit(self._s.value("tmdb_key", ""))
        self._tmdb.setPlaceholderText("TMDb v3 API key")
        self._tmdb.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("TMDb API key:", self._tmdb)

        # SublerCLI path
        sub_row = QHBoxLayout()
        self._subler = QLineEdit(self._s.value("subler_path", SUBLER_DEFAULT))
        b2 = QPushButton("…"); b2.setFixedWidth(32); b2.setObjectName("small")
        b2.clicked.connect(self._browse_subler)
        sub_row.addWidget(self._subler); sub_row.addWidget(b2)
        form.addRow("SublerCLI path:", sub_row)

        lay.addLayout(form)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self.setStyleSheet("""
            QDialog, QWidget { background:#141414; color:#e0e0e0; }
            QLineEdit {
                background:#2a2a2a; border:1px solid #444;
                border-radius:6px; padding:4px 8px; color:#e0e0e0;
            }
            QPushButton {
                background:#0d6efd; border:none; border-radius:6px;
                color:white; font-size:13px; font-weight:bold; padding:4px 14px;
            }
            QPushButton:hover { background:#3385ff; }
            QPushButton#small {
                background:#2a2a2a; font-size:11px;
                font-weight:normal; padding:3px 10px; border-radius:5px;
            }
            QPushButton#small:hover { background:#383838; }
            QLabel { color:#ccc; }
        """)

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select output folder",
                                             self._dir.text() or os.path.expanduser("~"))
        if d: self._dir.setText(d)

    def _browse_subler(self):
        f, _ = QFileDialog.getOpenFileName(self, "Locate SublerCLI", "/opt/homebrew/bin")
        if f: self._subler.setText(f)

    def _save(self):
        self._s.setValue("output_dir",   self._dir.text().strip())
        self._s.setValue("tmdb_key",     self._tmdb.text().strip())
        self._s.setValue("subler_path",  self._subler.text().strip())
        self.accept()

    @staticmethod
    def output_dir() -> Optional[str]:
        return QSettings("Remux", "Remux").value("output_dir", "").strip() or None

    @staticmethod
    def tmdb_key() -> str:
        return QSettings("Remux", "Remux").value("tmdb_key", "").strip()

    @staticmethod
    def subler_path() -> str:
        v = QSettings("Remux", "Remux").value("subler_path", "").strip()
        return v or SUBLER_DEFAULT


# ── Main window ───────────────────────────────────────────────────────────────

class RemuxWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Remux")
        self.setMinimumWidth(720)
        self.setWindowIcon(make_icon())
        self.queue:        list[QueueItem] = []
        self._probers:     list            = []
        self._tag_workers: list            = []
        self.worker:       Optional[ConvertWorker] = None
        self._build_ui()
        self._apply_dark()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setSpacing(10)
        lay.setContentsMargins(18, 18, 18, 18)

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
        hdr.addWidget(self.queue_lbl); hdr.addStretch(); hdr.addWidget(settings_btn)
        lay.addLayout(hdr)

        # Table
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Input file", "Output file", "Codec info", "Status"])
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(COL_INPUT,  QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(COL_OUTPUT, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(COL_INFO,   QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked |
                                   QAbstractItemView.EditTrigger.SelectedClicked)
        self.table.setFixedHeight(190)
        self.table.verticalHeader().setVisible(False)
        self.table.setFont(QFont("Menlo", 11))
        self.table.itemChanged.connect(self._on_cell_edited)
        self.table.itemSelectionChanged.connect(self._on_selection)
        lay.addWidget(self.table)

        # Table buttons
        tbl_row = QHBoxLayout()
        self.remove_btn = QPushButton("Remove"); self.remove_btn.setObjectName("small")
        self.remove_btn.setEnabled(False); self.remove_btn.clicked.connect(self._remove_selected)
        self.clear_btn  = QPushButton("Clear done"); self.clear_btn.setObjectName("small")
        self.clear_btn.clicked.connect(self._clear_done)
        self.open_btn   = QPushButton("📂  Open output folder"); self.open_btn.setObjectName("small")
        self.open_btn.setVisible(False); self.open_btn.clicked.connect(self._open_output_folder)
        tbl_row.addWidget(self.remove_btn); tbl_row.addWidget(self.clear_btn)
        tbl_row.addStretch(); tbl_row.addWidget(self.open_btn)
        lay.addLayout(tbl_row)

        self.info_bar = QLabel("")
        self.info_bar.setStyleSheet("color:#555;font-size:11px;font-family:Menlo;")
        lay.addWidget(self.info_bar)

        # Options row
        opt_row = QHBoxLayout()
        self.delete_chk = QCheckBox("Delete originals")
        self.tag_chk    = QCheckBox("Tag with TMDb metadata")
        self.media_combo = QComboBox()
        self.media_combo.addItems(["Movie", "TV Show"])
        self.media_combo.setFixedWidth(100)
        self.media_lbl = QLabel("Type:")
        self.media_lbl.setStyleSheet("color:#aaa;font-size:12px;")
        self.convert_btn = QPushButton("Convert All")
        self.convert_btn.setEnabled(False)
        self.convert_btn.setFixedHeight(36)
        self.convert_btn.setMinimumWidth(130)
        self.convert_btn.clicked.connect(self._start_queue)
        opt_row.addWidget(self.delete_chk)
        opt_row.addSpacing(16)
        opt_row.addWidget(self.tag_chk)
        opt_row.addSpacing(8)
        opt_row.addWidget(self.media_lbl)
        opt_row.addWidget(self.media_combo)
        opt_row.addStretch()
        opt_row.addWidget(self.convert_btn)
        lay.addLayout(opt_row)

        # Progress
        prog_row = QHBoxLayout()
        self.cur_lbl = QLabel("")
        self.cur_lbl.setStyleSheet("color:#777;font-size:11px;")
        self.cur_lbl.setMinimumWidth(220)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100); self.progress.setValue(0)
        self.progress.setFixedHeight(16)
        prog_row.addWidget(self.cur_lbl, 1); prog_row.addWidget(self.progress, 2)
        lay.addLayout(prog_row)

        # Log
        self.log = QTextEdit()
        self.log.setReadOnly(True); self.log.setFixedHeight(130)
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
            QComboBox {
                background:#2a2a2a; border:1px solid #444;
                border-radius:5px; padding:3px 8px; color:#e0e0e0;
            }
            QComboBox::drop-down { border:none; }
            QComboBox QAbstractItemView { background:#2a2a2a; color:#e0e0e0; }
        """)

    # ── Queue management ──────────────────────────────────────────────────────

    def _add_files(self, paths: list):
        existing = {item.src for item in self.queue}
        out_dir  = SettingsDialog.output_dir()
        for path in paths:
            if path in existing:
                continue
            item = QueueItem(path, out_dir)
            item.row = len(self.queue)
            self.queue.append(item)
            self._insert_row(item)
            w = ProbeWorker(item)
            w.done.connect(self._on_probe_done)
            self._probers.append(w)
            w.start()
        self._refresh_header()
        self.convert_btn.setEnabled(True)

    def _insert_row(self, item: QueueItem):
        r = self.table.rowCount()
        self.table.insertRow(r)
        in_c = QTableWidgetItem(os.path.basename(item.src))
        in_c.setFlags(in_c.flags() & ~Qt.ItemFlag.ItemIsEditable)
        in_c.setToolTip(item.src)
        self.table.setItem(r, COL_INPUT, in_c)
        out_c = QTableWidgetItem(os.path.basename(item.dst))
        out_c.setToolTip(item.dst)
        self.table.setItem(r, COL_OUTPUT, out_c)
        inf_c = QTableWidgetItem("probing…")
        inf_c.setFlags(inf_c.flags() & ~Qt.ItemFlag.ItemIsEditable)
        inf_c.setForeground(QColor("#555"))
        self.table.setItem(r, COL_INFO, inf_c)
        self._set_status_cell(r, item.status)

    def _set_status_cell(self, row: int, status: str):
        cell = QTableWidgetItem(status)
        cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
        cell.setForeground(QColor(STATUS_COLOR.get(status, "#888")))
        self.table.setItem(row, COL_STATUS, cell)

    def _on_probe_done(self, item: QueueItem, info: str):
        item.info = info
        cell = self.table.item(item.row, COL_INFO)
        if cell:
            self.table.blockSignals(True)
            cell.setText(info); cell.setForeground(QColor("#777"))
            self.table.blockSignals(False)
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
        if os.sep not in text and "/" not in text:
            text = os.path.join(os.path.dirname(item.dst), text)
        if not text.endswith(".mp4"):
            text += ".mp4"
        item.dst = text
        self.table.blockSignals(True)
        cell.setText(os.path.basename(item.dst)); cell.setToolTip(item.dst)
        self.table.blockSignals(False)

    def _on_selection(self):
        sel = self.table.selectedItems()
        self.remove_btn.setEnabled(bool(sel))
        if not sel:
            self.info_bar.setText(""); return
        row = sel[0].row()
        if row < len(self.queue):
            self._show_info(self.queue[row])

    def _show_info(self, item: QueueItem):
        parts = [item.src] + ([item.info] if item.info else [])
        self.info_bar.setText("  " + "  •  ".join(parts))

    def _remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedItems()}, reverse=True)
        for row in rows:
            if self.queue[row].status not in (S_CONVERTING, S_FETCHING, S_TAGGING):
                self.queue.pop(row); self.table.removeRow(row)
        for i, item in enumerate(self.queue): item.row = i
        self._refresh_header()
        if not self.queue: self.convert_btn.setEnabled(False)

    def _clear_done(self):
        for row in range(len(self.queue) - 1, -1, -1):
            if self.queue[row].status in (S_DONE, S_ERROR):
                self.queue.pop(row); self.table.removeRow(row)
        for i, item in enumerate(self.queue): item.row = i
        self._refresh_header()
        if not self.queue: self.convert_btn.setEnabled(False)

    def _refresh_header(self):
        n = len(self.queue)
        self.queue_lbl.setText(f"Queue  ({n} file{'s' if n != 1 else ''})")

    # ── Pipeline ──────────────────────────────────────────────────────────────

    def _start_queue(self):
        self.convert_btn.setEnabled(False)
        self.open_btn.setVisible(False)
        self._process_next()

    def _process_next(self):
        for item in self.queue:
            if item.status == S_PENDING:
                self._convert(item)
                return
        # All done
        self.cur_lbl.setText(""); self.progress.setValue(0)
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
        self.worker.finished.connect(lambda ok, it=item: self._on_convert_done(ok, it))
        self.worker.start()

    def _on_convert_done(self, success: bool, item: QueueItem):
        if not success:
            item.status = S_ERROR
            self._set_status_cell(item.row, item.status)
            self._process_next()
            return

        # Start tagging if enabled and configured
        key    = SettingsDialog.tmdb_key()
        subler = SettingsDialog.subler_path()
        if self.tag_chk.isChecked() and key and os.path.exists(subler):
            item.status = S_FETCHING
            self._set_status_cell(item.row, item.status)
            self.cur_lbl.setText("Fetching metadata…")
            media = "movie" if self.media_combo.currentIndex() == 0 else "tv"
            w = TagFetchWorker(item, key, media)
            w.done.connect(self._on_fetch_done)
            self._tag_workers.append(w)
            w.start()
        else:
            if self.tag_chk.isChecked() and not key:
                self._append_log("  ⚠ TMDb key not set — skipping tags. Add it in ⚙ Settings.")
            item.status = S_DONE
            self._set_status_cell(item.row, item.status)
            self._process_next()

    def _on_fetch_done(self, item: QueueItem, tags: dict, thumbs: list,
                       match_title: str, error: str):
        if error or not tags:
            self._append_log(f"  TMDb: {error or 'no match'} — skipping tags.")
            item.status = S_DONE
            self._set_status_cell(item.row, item.status)
            self._process_next()
            return

        self._append_log(f"  Matched: {match_title}")
        item.tmdb_tags = tags
        item.status    = S_ARTWORK
        self._set_status_cell(item.row, item.status)

        media  = "movie" if self.media_combo.currentIndex() == 0 else "tv"
        dlg    = ArtworkPickerDialog(self, item, tags, thumbs, match_title,
                                     SettingsDialog.tmdb_key(), media)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            artwork_url = dlg.selected_url
            tags        = dlg._tags   # may have been updated by re-search
        else:
            artwork_url = None
            self._append_log("  Artwork skipped.")

        item.status = S_TAGGING
        self._set_status_cell(item.row, item.status)
        self.cur_lbl.setText("Writing tags…")

        w = TagWriteWorker(item, tags, artwork_url, SettingsDialog.subler_path())
        w.log.connect(self._append_log)
        w.finished.connect(lambda ok, it=item: self._on_tag_done(it, ok))
        self._tag_workers.append(w)
        w.start()

    def _on_tag_done(self, item: QueueItem, success: bool):
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
