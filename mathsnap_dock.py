#!/usr/bin/env python3
# =============================================================================
# MathSnap Dock — snip a math problem anywhere on screen, get a worked solution.
# =============================================================================
#
# SETUP
# -----
# 1. Python 3.10+ recommended. Install dependencies:
#
#        pip install PyQt6 pillow google-genai "pynput>=1.7.7"
#
# 2. Pick a free AI provider in "🔑 Settings" (all of these cost nothing):
#
#      Google Gemini   free tier, key from https://aistudio.google.com/apikey
#      Groq            free tier, key from https://console.groq.com/keys
#      OpenRouter      free models, key from https://openrouter.ai/keys
#      Ollama          runs on your own machine, no key, no account:
#                        install https://ollama.com, then e.g.
#                        ollama pull gemma3:4b
#
#    Keys can also come from the environment: GEMINI_API_KEY, GROQ_API_KEY,
#    OPENROUTER_API_KEY. The model name for every provider is editable in
#    Settings, so you can switch to any other vision-capable model.
#
# 3. Run:
#
#        python3 mathsnap_dock.py
#
# macOS PERMISSIONS (System Settings → Privacy & Security)
# --------------------------------------------------------
# Grant these to the app that launches Python (Terminal, iTerm, VS Code, ...),
# then restart that app:
#   * Screen Recording  — otherwise captures show only your wallpaper.
#   * Accessibility and Input Monitoring — needed for the global
#     Option+Space hotkey. Without them the hotkey silently does nothing.
#
# USAGE
# -----
#   Option+Space (Alt+Space)  toggle the dock from anywhere
#   📸 Capture Math           drag a red box around a problem; Esc/right-click cancels
#   ✕                         hide the dock (Option+Space brings it back)
#   Cmd+Q (Ctrl+Q)            quit, while the dock is focused
# =============================================================================

import base64
import io
import json
import os
import sys
import threading
import urllib.error
import urllib.request

from PyQt6.QtCore import (
    QBuffer, QEasingCurve, QIODevice, QObject, QPoint, QPropertyAnimation, QRect,
    QSettings, Qt, QTimer, pyqtSignal,
)
from PyQt6.QtGui import QColor, QGuiApplication, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    genai = None
    genai_types = None

try:
    from pynput import keyboard as pynput_keyboard
except Exception:  # ImportError, or no display / backend available
    pynput_keyboard = None


PROMPT = (
    "You are an expert Math Tutor. Identify the mathematical equation, expression, "
    "or word problem in this cropped image. Provide a clear, step-by-step explanation "
    "and conclude with the final boxed answer."
)
# The solution view renders Markdown but not LaTeX, so steer the output format.
FORMAT_HINT = (
    "Format the reply in Markdown. Write math in plain text / Unicode "
    "(e.g. x², √, ×, ÷, fractions as a/b) instead of LaTeX."
)

# Every provider here can be used for free. "openai" kind = OpenAI-compatible
# /chat/completions endpoint, called with the standard library only.
PROVIDERS = {
    "gemini": {
        "label": "Google Gemini  ·  free tier",
        "kind": "gemini",
        "model": "gemini-2.5-flash",
        "env": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "key_url": "https://aistudio.google.com/apikey",
        "note": "Free tier with daily limits. No credit card needed.",
    },
    "groq": {
        "label": "Groq  ·  free tier, very fast",
        "kind": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "meta-llama/llama-4-scout-17b-16e-instruct",
        "env": ("GROQ_API_KEY",),
        "key_url": "https://console.groq.com/keys",
        "note": "Free tier with rate limits. Use any vision model from console.groq.com/docs/models.",
    },
    "openrouter": {
        "label": "OpenRouter  ·  free models",
        "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openrouter/free",
        "env": ("OPENROUTER_API_KEY",),
        "key_url": "https://openrouter.ai/keys",
        "note": "\"openrouter/free\" auto-picks a free vision model. Any model id ending in :free also costs nothing.",
    },
    "ollama": {
        "label": "Ollama  ·  local, free, private",
        "kind": "openai",
        "base_url": "http://localhost:11434/v1",
        "model": "gemma3:4b",
        "env": (),
        "key_url": "https://ollama.com/download",
        "note": "Runs on your own machine: no key, no account, works offline. "
                "Install Ollama, then run: ollama pull gemma3:4b",
    },
}
DEFAULT_PROVIDER = "gemini"

HOTKEY = "<alt>+<space>"
PANEL_HEIGHT = 230
PANEL_WIDTH_RATIO = 0.75
SHADOW_MARGIN = 18
MIN_SELECTION = 8      # logical px; anything smaller counts as a cancelled snip
MAX_IMAGE_SIDE = 1600  # px; larger snips are downscaled before upload

STYLE = """
#dock {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(30, 27, 46, 246), stop:0.55 rgba(21, 21, 32, 246), stop:1 rgba(17, 24, 39, 246));
    border: 1px solid rgba(167, 139, 250, 70);
    border-bottom: none;
    border-top-left-radius: 20px;
    border-top-right-radius: 20px;
}
QLabel { color: #b9b8c9; font-size: 12px; background: transparent; }
#brand { color: #ffffff; font-size: 15px; font-weight: 700; letter-spacing: 0.3px; }
#sub { color: #8b8aa0; font-size: 11px; }
#section { color: #8b8aa0; font-size: 10px; font-weight: 700; letter-spacing: 1.4px; }
#thumb {
    background: rgba(255, 255, 255, 10);
    border: 1px dashed rgba(167, 139, 250, 90);
    border-radius: 12px;
    color: #77768c;
    font-size: 11px;
}
#pill {
    background: rgba(255, 255, 255, 14);
    border: 1px solid rgba(255, 255, 255, 22);
    border-radius: 12px;
    padding: 4px 10px;
    color: #e6e5f2;
    font-size: 11px;
    font-weight: 600;
}
#badge {
    background: rgba(139, 92, 246, 40);
    border: 1px solid rgba(167, 139, 250, 90);
    border-radius: 10px;
    padding: 3px 9px;
    color: #d6ccff;
    font-size: 10px;
    font-weight: 600;
}
QPushButton {
    background: rgba(255, 255, 255, 16);
    color: #ecebf5;
    border: 1px solid rgba(255, 255, 255, 26);
    border-radius: 10px;
    padding: 7px 12px;
    font-size: 12px;
    font-weight: 600;
}
QPushButton:hover { background: rgba(255, 255, 255, 30); border-color: rgba(167, 139, 250, 140); }
QPushButton:pressed { background: rgba(255, 255, 255, 12); }
QPushButton:disabled { color: #6f6e82; }
#capture, #primary {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #7c3aed, stop:1 #ec4899);
    border: none;
    color: white;
    font-size: 13px;
    font-weight: 700;
    padding: 10px 14px;
    border-radius: 12px;
}
#capture:hover, #primary:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #8b5cf6, stop:1 #f472b6);
}
#capture:disabled { background: #3b3550; color: #8b8aa0; }
#ghost { background: transparent; border: none; color: #9d9cb3; padding: 3px 8px; font-size: 13px; }
#ghost:hover { background: rgba(255, 255, 255, 22); color: #ffffff; }
#mini { padding: 3px 10px; font-size: 11px; border-radius: 9px; }
QTextBrowser {
    background: rgba(8, 8, 16, 120);
    color: #ecebf5;
    border: 1px solid rgba(255, 255, 255, 20);
    border-radius: 14px;
    padding: 10px 14px;
    font-size: 13px;
    selection-background-color: #7c3aed;
}
QScrollBar:vertical { background: transparent; width: 8px; margin: 6px 2px; }
QScrollBar::handle:vertical { background: rgba(255, 255, 255, 50); border-radius: 3px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: rgba(167, 139, 250, 160); }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }

QDialog { background: #17161f; }
QLineEdit, QComboBox {
    background: rgba(255, 255, 255, 12);
    color: #ecebf5;
    border: 1px solid rgba(255, 255, 255, 30);
    border-radius: 9px;
    padding: 7px 10px;
    font-size: 12px;
    selection-background-color: #7c3aed;
}
QLineEdit:focus, QComboBox:focus { border-color: #a78bfa; }
QLineEdit:disabled { color: #6f6e82; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: #211f2e; color: #ecebf5; border: 1px solid rgba(255, 255, 255, 30);
    selection-background-color: #7c3aed;
}
#note { color: #9d9cb3; font-size: 11px; }
#note a, QLabel a { color: #c4b5fd; }
"""

STATUS_COLORS = {"Ready": "#34d399", "Capturing...": "#fbbf24", "Solving...": "#a78bfa"}


def pixmap_to_png(pixmap: QPixmap) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buf, "PNG")
    return bytes(buf.data())


def shrink_png(png: bytes) -> bytes:
    """Downscale very large snips with Pillow so uploads stay small and fast."""
    if Image is None:
        return png
    try:
        img = Image.open(io.BytesIO(png))
        if max(img.size) <= MAX_IMAGE_SIDE:
            return png
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        out = io.BytesIO()
        img.convert("RGB").save(out, "PNG")
        return out.getvalue()
    except Exception:
        return png


def friendly_error(exc: Exception, provider_id: str) -> str:
    text = str(exc) or exc.__class__.__name__
    low = text.lower()
    if provider_id == "ollama" and ("refused" in low or "urlopen error" in low):
        return ("Could not reach Ollama at localhost:11434. Start the Ollama app (or run "
                "`ollama serve`) and make sure the model is pulled.\n\n" + text)
    if "404" in low or "not found" in low or "decommissioned" in low or "no endpoints" in low:
        return ("The model name was not accepted by this provider. Pick a current "
                "vision-capable model in 🔑 Settings.\n\n" + text)
    if "api key" in low or "api_key" in low or "401" in low or "403" in low or "permission_denied" in low:
        return "The API key was rejected. Check it in 🔑 Settings.\n\n" + text
    if "429" in low or "resource_exhausted" in low or "quota" in low or "rate limit" in low:
        return ("Free-tier rate limit or quota reached. Wait a moment, or switch to another "
                "free provider in 🔑 Settings.\n\n" + text)
    network_words = ("connect", "timed out", "timeout", "network", "getaddrinfo", "urlopen error",
                     "name resolution", "nodename", "ssl", "503", "unavailable")
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)) or any(w in low for w in network_words):
        return "Network problem reaching the AI provider. Check your connection and try again.\n\n" + text
    return text


class HotkeyBridge(QObject):
    """Marshals the pynput listener thread's callback onto the Qt main thread."""
    triggered = pyqtSignal()


class Solver(QObject):
    """Streams a model response from a background thread."""
    chunk = pyqtSignal(int, str)
    finished = pyqtSignal(int)
    failed = pyqtSignal(int, str)

    def solve(self, job_id: int, provider_id: str, model: str, api_key: str, png: bytes) -> None:
        threading.Thread(target=self._run, args=(job_id, provider_id, model, api_key, png),
                         daemon=True).start()

    def _run(self, job_id, provider_id, model, api_key, png):
        provider = PROVIDERS[provider_id]
        try:
            png = shrink_png(png)
            if provider["kind"] == "gemini":
                pieces = self._gemini(model, api_key, png)
            else:
                pieces = self._openai_compatible(provider["base_url"], model, api_key, png)
            got_text = False
            for text in pieces:
                if text:
                    got_text = True
                    self.chunk.emit(job_id, text)
            if not got_text:
                self.failed.emit(job_id, "The model returned an empty response (it may have been "
                                         "blocked, or the model cannot read images).")
                return
            self.finished.emit(job_id)
        except Exception as exc:
            self.failed.emit(job_id, friendly_error(exc, provider_id))

    @staticmethod
    def _gemini(model, api_key, png):
        if genai is None:
            raise RuntimeError("The google-genai package is not installed. Run: pip install google-genai")
        client = genai.Client(api_key=api_key)
        stream = client.models.generate_content_stream(
            model=model,
            contents=[
                genai_types.Part.from_bytes(data=png, mime_type="image/png"),
                PROMPT + "\n\n" + FORMAT_HINT,
            ],
        )
        for part in stream:
            yield part.text

    @staticmethod
    def _openai_compatible(base_url, model, api_key, png):
        body = json.dumps({
            "model": model,
            "stream": True,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT + "\n\n" + FORMAT_HINT},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}},
                ],
            }],
        }).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "MathSnapDock/1.0",
                   "X-Title": "MathSnap Dock"}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        request = urllib.request.Request(base_url + "/chat/completions", data=body, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=120)
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", "replace")[:600]
            raise RuntimeError(f"HTTP {err.code}: {detail}") from None
        with response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except ValueError:
                    continue
                if event.get("error"):
                    raise RuntimeError(json.dumps(event["error"])[:600])
                choices = event.get("choices") or []
                if choices:
                    yield (choices[0].get("delta") or {}).get("content") or ""


class SnipOverlay(QWidget):
    """Full-screen frozen screenshot, dimmed, with a red drag-to-select box."""
    selected = pyqtSignal(QPixmap)
    cancelled = pyqtSignal()

    def __init__(self, screen, screenshot: QPixmap):
        super().__init__()
        self._shot = screenshot
        self._origin = None
        self._current = None
        self._done = False
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setGeometry(screen.geometry())

    def _selection(self) -> QRect:
        if self._origin is None or self._current is None:
            return QRect()
        return QRect(self._origin, self._current).normalized().intersected(self.rect())

    def _source_rect(self, rect: QRect) -> QRect:
        # Map widget (logical) coordinates to screenshot (device) pixels.
        sx = self._shot.width() / max(1, self.width())
        sy = self._shot.height() / max(1, self.height())
        return QRect(round(rect.x() * sx), round(rect.y() * sy),
                     round(rect.width() * sx), round(rect.height() * sy))

    def paintEvent(self, _event):
        p = QPainter(self)
        p.drawPixmap(self.rect(), self._shot)
        p.fillRect(self.rect(), QColor(10, 8, 20, 120))
        sel = self._selection()
        if not sel.isEmpty():
            p.drawPixmap(sel, self._shot, self._source_rect(sel))
            p.setPen(QPen(QColor("#ff2d2d"), 2))
            p.drawRect(sel.adjusted(0, 0, -1, -1))
            p.setPen(QColor(255, 255, 255, 230))
            p.drawText(sel.x(), max(14, sel.y() - 6), f"{sel.width()} × {sel.height()}")
        else:
            hint = "Drag a box around the math problem   ·   Esc to cancel"
            font = p.font()
            font.setPointSize(15)
            font.setBold(True)
            p.setFont(font)
            width = p.fontMetrics().horizontalAdvance(hint) + 44
            box = QRect((self.width() - width) // 2, 56, width, 44)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(20, 18, 32, 215))
            p.drawRoundedRect(box, 22, 22)
            p.setPen(QColor(255, 255, 255, 235))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, hint)
        p.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = self._current = event.position().toPoint()
            self.update()
        else:
            self._finish(None)

    def mouseMoveEvent(self, event):
        if self._origin is not None:
            self._current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._origin is None:
            return
        self._current = event.position().toPoint()
        sel = self._selection()
        if sel.width() < MIN_SELECTION or sel.height() < MIN_SELECTION:
            self._finish(None)
        else:
            self._finish(self._shot.copy(self._source_rect(sel)))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._finish(None)

    def _finish(self, crop):
        if self._done:
            return
        self._done = True
        self.hide()
        if crop is None or crop.isNull():
            self.cancelled.emit()
        else:
            self.selected.emit(crop)
        self.close()


class SettingsDialog(QDialog):
    """Choose a (free) AI provider, its model, and its API key."""

    def __init__(self, dock: "Dock"):
        super().__init__(dock)
        self.dock = dock
        self._current = None
        self._pending = {}  # provider id -> (model, key) edited but not yet saved
        self.setWindowTitle("MathSnap Settings")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self.setMinimumWidth(460)
        self.setStyleSheet(STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(8)
        layout.addWidget(QLabel("AI provider", objectName="brand"))
        layout.addWidget(QLabel("Every option below is free to use.", objectName="sub"))
        layout.addSpacing(6)

        self.provider_box = QComboBox()
        for pid, info in PROVIDERS.items():
            self.provider_box.addItem(info["label"], pid)
        layout.addWidget(self.provider_box)

        self.note = QLabel(objectName="note")
        self.note.setWordWrap(True)
        self.note.setOpenExternalLinks(True)
        layout.addWidget(self.note)
        layout.addSpacing(6)

        layout.addWidget(QLabel("MODEL", objectName="section"))
        self.model_edit = QLineEdit()
        layout.addWidget(self.model_edit)
        layout.addWidget(QLabel("API KEY", objectName="section"))
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.key_edit)
        layout.addSpacing(10)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save", objectName="primary")
        save.setDefault(True)
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

        self.provider_box.currentIndexChanged.connect(self._provider_changed)
        self.provider_box.setCurrentIndex(max(0, self.provider_box.findData(dock.provider_id())))
        self._provider_changed()

    def _stash(self):
        if self._current:
            self._pending[self._current] = (self.model_edit.text().strip(), self.key_edit.text().strip())

    def _provider_changed(self):
        self._stash()
        pid = self.provider_box.currentData()
        self._current = pid
        info = PROVIDERS[pid]
        model, key = self._pending.get(pid, (self.dock.model_for(pid), self.dock.stored_key(pid)))
        self.model_edit.setText(model)
        self.model_edit.setPlaceholderText(info["model"])
        needs_key = bool(info["env"])
        self.key_edit.setEnabled(needs_key)
        self.key_edit.setText(key if needs_key else "")
        if needs_key:
            env_set = any(os.environ.get(name) for name in info["env"])
            self.key_edit.setPlaceholderText(
                f"Using ${info['env'][0]} from environment" if env_set else "Paste your key")
            link = f'<a style="color:#c4b5fd" href="{info["key_url"]}">Get a free key</a>'
        else:
            self.key_edit.setPlaceholderText("Not needed — runs locally")
            link = f'<a style="color:#c4b5fd" href="{info["key_url"]}">Download Ollama</a>'
        self.note.setText(f'{info["note"]} {link}')

    def _save(self):
        self._stash()
        settings = self.dock.settings
        for pid, (model, key) in self._pending.items():
            settings.setValue(f"{pid}/model", model)
            settings.setValue(f"{pid}/api_key", key)
        settings.setValue("provider", self._current)
        settings.sync()
        self.accept()


class Dock(QWidget):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("MathSnap", "MathSnapDock")
        legacy_key = self.settings.value("gemini_api_key", "", type=str)
        if legacy_key and not self.settings.value("gemini/api_key", "", type=str):
            self.settings.setValue("gemini/api_key", legacy_key)
        self.solver = Solver()
        self.solver.chunk.connect(self._on_chunk)
        self.solver.finished.connect(self._on_finished)
        self.solver.failed.connect(self._on_failed)
        self._job_id = 0
        self._answer = ""
        self._overlay = None
        self._anim = None
        self._visible = False
        self.hotkey_available = True
        self._last_png = None

        self.setWindowTitle("MathSnap Dock")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Qt.Tool windows vanish on macOS when the app loses focus unless this is set.
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.setStyleSheet(STYLE)
        self._build_ui()
        QShortcut(QKeySequence.StandardKey.Quit, self, activated=QApplication.quit)
        self.set_status("Ready")
        self._refresh_badge()

    # ---- UI ---------------------------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, 0)
        frame = QFrame(objectName="dock")
        shadow = QGraphicsDropShadowEffect(frame)
        shadow.setBlurRadius(36)
        shadow.setOffset(0, 2)
        shadow.setColor(QColor(124, 58, 237, 110))
        frame.setGraphicsEffect(shadow)
        outer.addWidget(frame)
        row = QHBoxLayout(frame)
        row.setContentsMargins(18, 14, 18, 14)
        row.setSpacing(16)

        # Left: brand, capture button, thumbnail
        left = QVBoxLayout()
        left.setSpacing(8)
        left.addWidget(QLabel("∑  MathSnap", objectName="brand"))
        self.capture_btn = QPushButton("📸  Capture Math", objectName="capture")
        self.capture_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.capture_btn.clicked.connect(self.start_capture)
        self.thumb = QLabel("Your snip appears here", objectName="thumb")
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setFixedWidth(190)
        self.thumb.setMinimumHeight(70)
        left.addWidget(self.capture_btn)
        left.addWidget(self.thumb, 1)
        row.addLayout(left)

        # Middle: solution
        middle = QVBoxLayout()
        middle.setSpacing(6)
        head = QHBoxLayout()
        head.addWidget(QLabel("SOLUTION", objectName="section"))
        self.badge = QLabel(objectName="badge")
        head.addWidget(self.badge)
        head.addStretch(1)
        self.retry_btn = QPushButton("↻ Retry", objectName="mini")
        self.retry_btn.setEnabled(False)
        self.retry_btn.clicked.connect(self.retry)
        self.copy_btn = QPushButton("Copy", objectName="mini")
        self.copy_btn.clicked.connect(self.copy_answer)
        head.addWidget(self.retry_btn)
        head.addWidget(self.copy_btn)
        middle.addLayout(head)
        self.output = QTextBrowser()
        self.output.setOpenExternalLinks(True)
        self.output.setPlaceholderText(
            "Capture a math problem and the step-by-step solution will appear here.\n"
            "Tip: press Option+Space anywhere to show or hide this dock.")
        middle.addWidget(self.output, 1)
        row.addLayout(middle, 1)

        # Right: close, status, settings
        right = QVBoxLayout()
        right.setSpacing(8)
        top = QHBoxLayout()
        self.status = QLabel(objectName="pill")
        top.addWidget(self.status)
        top.addStretch(1)
        close_btn = QPushButton("✕", objectName="ghost")
        close_btn.setToolTip("Hide (Option+Space to show again)")
        close_btn.clicked.connect(self.close_clicked)
        top.addWidget(close_btn)
        right.addLayout(top)
        right.addStretch(1)
        hint = QLabel("⌥ Space  to toggle", objectName="sub")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right.addWidget(hint)
        settings_btn = QPushButton("🔑  Settings")
        settings_btn.clicked.connect(self.open_settings)
        right.addWidget(settings_btn)
        holder = QWidget()
        holder.setLayout(right)
        holder.setFixedWidth(160)
        row.addWidget(holder)

    def set_status(self, text: str):
        color = STATUS_COLORS.get(text, "#fb7185")
        self.status.setText(f'<span style="color:{color}">●</span>&nbsp;&nbsp;{text}')

    def _refresh_badge(self):
        pid = self.provider_id()
        name = PROVIDERS[pid]["label"].split("·")[0].strip()
        self.badge.setText(f"{name} · {self.model_for(pid)}")

    # ---- Positioning / toggle ----------------------------------------------

    def _rects(self):
        """(shown_pos, hidden_pos, size) flush with the bottom of the primary screen."""
        screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()  # sits above the macOS Dock if it's at the bottom
        width = int(avail.width() * PANEL_WIDTH_RATIO) + 2 * SHADOW_MARGIN
        height = PANEL_HEIGHT + SHADOW_MARGIN
        x = avail.x() + (avail.width() - width) // 2
        shown = QPoint(x, avail.y() + avail.height() - height)
        hidden = QPoint(x, screen.geometry().y() + screen.geometry().height() + 4)
        return shown, hidden, (width, height)

    def _animate(self, start: QPoint, end: QPoint, on_done=None):
        if self._anim is not None:
            self._anim.stop()
        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(240)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.setStartValue(start)
        self._anim.setEndValue(end)
        if on_done:
            self._anim.finished.connect(on_done)
        self._anim.start()

    def slide_in(self):
        shown, hidden, (w, h) = self._rects()
        self.setFixedSize(w, h)
        start = self.pos() if self.isVisible() else hidden
        self.move(start)
        self.show()
        self.raise_()
        self.activateWindow()
        self._visible = True
        self._animate(start, shown)

    def slide_out(self):
        if not self.isVisible():
            self._visible = False
            return
        _, hidden, _ = self._rects()
        self._visible = False
        self._animate(self.pos(), hidden, on_done=self._hide_if_toggled_off)

    def _hide_if_toggled_off(self):
        if not self._visible:
            self.hide()

    def close_clicked(self):
        # With no global hotkey there is no way to summon a hidden dock, so quit instead.
        if self.hotkey_available:
            self.slide_out()
        else:
            QApplication.quit()

    def toggle(self):
        if self._overlay is not None:
            return  # mid-snip; ignore the hotkey
        if self._visible:
            self.slide_out()
        else:
            self.slide_in()

    # ---- Settings -----------------------------------------------------------

    def provider_id(self) -> str:
        pid = self.settings.value("provider", DEFAULT_PROVIDER, type=str)
        return pid if pid in PROVIDERS else DEFAULT_PROVIDER

    def model_for(self, pid: str) -> str:
        return self.settings.value(f"{pid}/model", "", type=str).strip() or PROVIDERS[pid]["model"]

    def stored_key(self, pid: str) -> str:
        return self.settings.value(f"{pid}/api_key", "", type=str).strip()

    def api_key(self, pid: str) -> str:
        key = self.stored_key(pid)
        for name in PROVIDERS[pid]["env"]:
            key = key or os.environ.get(name, "").strip()
        return key

    def open_settings(self) -> bool:
        saved = SettingsDialog(self).exec() == QDialog.DialogCode.Accepted
        if saved:
            self._refresh_badge()
            self.set_status("Ready")
        return saved

    # ---- Capture flow -------------------------------------------------------

    def start_capture(self):
        if self._overlay is not None:
            return
        self.set_status("Capturing...")
        self.capture_btn.setEnabled(False)
        if self._anim is not None:
            self._anim.stop()
        self.hide()
        # Give the window server time to actually remove the panel from the screen.
        QTimer.singleShot(250, self._grab_and_show_overlay)

    def _grab_and_show_overlay(self):
        screen = QGuiApplication.primaryScreen()
        shot = screen.grabWindow(0)
        if shot.isNull():
            self._restore_panel()
            self._show_error("Could not capture the screen. On macOS, grant Screen Recording "
                             "permission to the app running Python, then restart it.")
            return
        self._overlay = SnipOverlay(screen, shot)
        self._overlay.selected.connect(self._on_snip)
        self._overlay.cancelled.connect(self._on_snip_cancelled)
        self._overlay.show()
        self._overlay.raise_()
        self._overlay.activateWindow()
        self._overlay.setFocus()

    def _restore_panel(self):
        self._overlay = None
        self.capture_btn.setEnabled(True)
        self.slide_in()

    def _on_snip_cancelled(self):
        self._restore_panel()
        self.set_status("Ready")

    def _on_snip(self, crop: QPixmap):
        self._restore_panel()
        self._last_png = pixmap_to_png(crop)  # in-memory buffer; nothing is written to disk
        self.retry_btn.setEnabled(True)
        self.thumb.setPixmap(crop.scaled(
            self.thumb.contentsRect().size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        self._solve(self._last_png)

    # ---- Solving ------------------------------------------------------------

    def retry(self):
        if self._last_png:
            self._solve(self._last_png)

    def copy_answer(self):
        if self._answer:
            QGuiApplication.clipboard().setText(self._answer)
            self.copy_btn.setText("Copied ✓")
            QTimer.singleShot(1200, lambda: self.copy_btn.setText("Copy"))

    def _solve(self, png: bytes):
        pid = self.provider_id()
        needs_key = bool(PROVIDERS[pid]["env"])
        if needs_key and not self.api_key(pid):
            self._show_error("No API key set for this provider. Add a free one in 🔑 Settings "
                             "(or switch to Ollama, which needs no key), then press ↻ Retry.",
                             status="No API key")
            if not self.open_settings():
                return
            pid = self.provider_id()
            if PROVIDERS[pid]["env"] and not self.api_key(pid):
                return
        self._job_id += 1
        self._answer = ""
        self.output.clear()
        self.set_status("Solving...")
        self.solver.solve(self._job_id, pid, self.model_for(pid), self.api_key(pid), png)

    def _on_chunk(self, job_id: int, text: str):
        if job_id != self._job_id:
            return  # stale stream from an earlier capture
        self._answer += text
        bar = self.output.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        self.output.setMarkdown(self._answer)
        if at_bottom:
            bar.setValue(bar.maximum())

    def _on_finished(self, job_id: int):
        if job_id == self._job_id:
            self.set_status("Ready")

    def _on_failed(self, job_id: int, message: str):
        if job_id != self._job_id:
            return
        prefix = self._answer + "\n\n---\n\n" if self._answer else ""
        self._show_error(message, prefix=prefix)

    def _show_error(self, message: str, status: str = "Error", prefix: str = ""):
        self.output.setMarkdown(f"{prefix}**⚠️ {status}**\n\n{message}")
        self.set_status(status)


def start_hotkey(bridge: HotkeyBridge):
    """Register the global Option/Alt+Space hotkey. Returns the listener or None."""
    if pynput_keyboard is None:
        return None
    try:
        listener = pynput_keyboard.GlobalHotKeys({HOTKEY: bridge.triggered.emit})
        listener.daemon = True
        listener.start()
        return listener
    except Exception:
        return None


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("MathSnap Dock")
    app.setQuitOnLastWindowClosed(False)  # hiding the dock must not quit the app

    dock = Dock()
    bridge = HotkeyBridge()
    bridge.triggered.connect(dock.toggle)
    listener = start_hotkey(bridge)
    dock.slide_in()

    if listener is None:
        dock.hotkey_available = False
        dock._show_error(
            "The global Option+Space hotkey is unavailable.\n\n"
            "Install it with `pip install pynput` and, on macOS, grant Accessibility and "
            "Input Monitoring permission to the app running Python. Until then, the ✕ "
            "button quits the app instead of hiding it.",
            status="Hotkey off")

    code = app.exec()
    if listener is not None:
        listener.stop()
    sys.exit(code)


if __name__ == "__main__":
    main()
