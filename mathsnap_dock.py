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
# 2. Get a Gemini API key: https://aistudio.google.com/apikey
#    Either paste it via the "🔑 Settings" button in the app, or export it:
#
#        export GEMINI_API_KEY="your-key"
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

import io
import os
import sys
import threading

from PyQt6.QtCore import (
    QBuffer, QEasingCurve, QIODevice, QObject, QPoint, QPropertyAnimation, QRect,
    QSettings, Qt, QTimer, pyqtSignal,
)
from PyQt6.QtGui import QColor, QGuiApplication, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QPushButton, QTextBrowser, QVBoxLayout, QWidget,
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


MODEL = "gemini-2.5-flash"
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
HOTKEY = "<alt>+<space>"
PANEL_HEIGHT = 190
PANEL_WIDTH_RATIO = 0.75
MIN_SELECTION = 8  # logical px; anything smaller counts as a cancelled snip

STYLE = """
#dock {
    background: rgba(28, 28, 32, 242);
    border: 1px solid rgba(255, 255, 255, 30);
    border-top-left-radius: 14px;
    border-top-right-radius: 14px;
}
QLabel { color: #d8d8de; font-size: 12px; }
#title { color: #ffffff; font-size: 13px; font-weight: 600; }
#thumb {
    background: rgba(255, 255, 255, 12);
    border: 1px dashed rgba(255, 255, 255, 50);
    border-radius: 8px;
    color: #8a8a94;
}
QPushButton {
    background: rgba(255, 255, 255, 18);
    color: #f2f2f5;
    border: 1px solid rgba(255, 255, 255, 28);
    border-radius: 8px;
    padding: 7px 12px;
    font-size: 12px;
}
QPushButton:hover { background: rgba(255, 255, 255, 34); }
QPushButton:disabled { color: #77777f; }
#capture { background: #e5484d; border: none; font-weight: 600; }
#capture:hover { background: #f2555a; }
#capture:disabled { background: #6b3a3c; }
#close { padding: 4px 9px; }
QTextBrowser {
    background: rgba(0, 0, 0, 70);
    color: #ececf1;
    border: 1px solid rgba(255, 255, 255, 22);
    border-radius: 10px;
    padding: 8px 10px;
    font-size: 13px;
}
"""


def pixmap_to_png(pixmap: QPixmap) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buf, "PNG")
    return bytes(buf.data())


def friendly_error(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    low = text.lower()
    if "api key" in low or "api_key" in low or "401" in low or "403" in low or "permission_denied" in low:
        return "The Gemini API key was rejected. Check it in 🔑 Settings.\n\n" + text
    if "429" in low or "resource_exhausted" in low or "quota" in low:
        return "Gemini rate limit or quota reached. Wait a moment and try again.\n\n" + text
    network_words = ("connect", "timed out", "timeout", "network", "getaddrinfo",
                     "name resolution", "nodename", "ssl", "503", "unavailable")
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)) or any(w in low for w in network_words):
        return "Network problem reaching Gemini. Check your connection and try again.\n\n" + text
    return text


class HotkeyBridge(QObject):
    """Marshals the pynput listener thread's callback onto the Qt main thread."""
    triggered = pyqtSignal()


class Solver(QObject):
    """Streams a Gemini response from a background thread."""
    chunk = pyqtSignal(int, str)
    finished = pyqtSignal(int)
    failed = pyqtSignal(int, str)

    def solve(self, job_id: int, api_key: str, png: bytes) -> None:
        threading.Thread(target=self._run, args=(job_id, api_key, png), daemon=True).start()

    def _run(self, job_id: int, api_key: str, png: bytes) -> None:
        try:
            client = genai.Client(api_key=api_key)
            stream = client.models.generate_content_stream(
                model=MODEL,
                contents=[
                    genai_types.Part.from_bytes(data=png, mime_type="image/png"),
                    PROMPT + "\n\n" + FORMAT_HINT,
                ],
            )
            got_text = False
            for part in stream:
                if part.text:
                    got_text = True
                    self.chunk.emit(job_id, part.text)
            if not got_text:
                self.failed.emit(job_id, "Gemini returned an empty response (it may have been blocked).")
                return
            self.finished.emit(job_id)
        except Exception as exc:
            self.failed.emit(job_id, friendly_error(exc))


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
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))
        sel = self._selection()
        if not sel.isEmpty():
            p.drawPixmap(sel, self._shot, self._source_rect(sel))
            p.setPen(QPen(QColor("#ff2d2d"), 2))
            p.drawRect(sel.adjusted(0, 0, -1, -1))
        else:
            p.setPen(QColor(255, 255, 255, 220))
            p.drawText(self.rect().adjusted(0, 40, 0, 0),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                       "Drag a box around the math problem  •  Esc to cancel")
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


class Dock(QWidget):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("MathSnap", "MathSnapDock")
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
        self._last_image = None  # PIL.Image of the latest snip (in memory only)

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

    # ---- UI ---------------------------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        frame = QFrame(objectName="dock")
        outer.addWidget(frame)
        row = QHBoxLayout(frame)
        row.setContentsMargins(14, 12, 14, 12)
        row.setSpacing(14)

        # Left: capture button + thumbnail
        left = QVBoxLayout()
        left.setSpacing(8)
        self.capture_btn = QPushButton("📸 Capture Math", objectName="capture")
        self.capture_btn.clicked.connect(self.start_capture)
        self.thumb = QLabel("No capture yet", objectName="thumb")
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setFixedSize(170, 104)
        left.addWidget(self.capture_btn)
        left.addWidget(self.thumb)
        left.addStretch(1)
        row.addLayout(left)

        # Middle: solution
        self.output = QTextBrowser()
        self.output.setOpenExternalLinks(True)
        self.output.setPlaceholderText(
            "Capture a math problem and the step-by-step solution will appear here.")
        row.addWidget(self.output, 1)

        # Right: close, status, settings
        right = QVBoxLayout()
        right.setSpacing(8)
        top = QHBoxLayout()
        top.addWidget(QLabel("MathSnap", objectName="title"))
        top.addStretch(1)
        close_btn = QPushButton("✕", objectName="close")
        close_btn.setToolTip("Hide (Option+Space to show again)")
        close_btn.clicked.connect(self.close_clicked)
        top.addWidget(close_btn)
        right.addLayout(top)
        self.status = QLabel()
        right.addWidget(self.status)
        right.addStretch(1)
        settings_btn = QPushButton("🔑 Settings")
        settings_btn.clicked.connect(self.open_settings)
        right.addWidget(settings_btn)
        holder = QWidget()
        holder.setLayout(right)
        holder.setFixedWidth(150)
        row.addWidget(holder)

    def set_status(self, text: str):
        colors = {"Ready": "#46c281", "Capturing...": "#f5a623", "Solving...": "#5aa9ff"}
        color = colors.get(text, "#ff6b6b")
        self.status.setText(f'<span style="color:{color}">●</span>&nbsp; {text}')

    # ---- Positioning / toggle ----------------------------------------------

    def _rects(self):
        """(shown_pos, hidden_pos, size) flush with the bottom of the primary screen."""
        screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()  # sits above the macOS Dock if it's at the bottom
        width = int(avail.width() * PANEL_WIDTH_RATIO)
        x = avail.x() + (avail.width() - width) // 2
        shown = QPoint(x, avail.y() + avail.height() - PANEL_HEIGHT)
        hidden = QPoint(x, screen.geometry().y() + screen.geometry().height() + 4)
        return shown, hidden, (width, PANEL_HEIGHT)

    def _animate(self, start: QPoint, end: QPoint, on_done=None):
        if self._anim is not None:
            self._anim.stop()
        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(200)
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

    def api_key(self) -> str:
        return (self.settings.value("gemini_api_key", "", type=str)
                or os.environ.get("GEMINI_API_KEY", "")
                or os.environ.get("GOOGLE_API_KEY", "")).strip()

    def open_settings(self) -> bool:
        key, ok = QInputDialog.getText(
            self, "MathSnap Settings", "Gemini API key:",
            QLineEdit.EchoMode.Password, self.settings.value("gemini_api_key", "", type=str))
        if not ok:
            return False
        self.settings.setValue("gemini_api_key", key.strip())
        self.settings.sync()
        self.set_status("Ready")
        return bool(key.strip())

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
        png = pixmap_to_png(crop)
        if Image is not None:
            try:
                self._last_image = Image.open(io.BytesIO(png))
                self._last_image.load()
            except Exception:
                self._last_image = None
        self.thumb.setPixmap(crop.scaled(
            self.thumb.contentsRect().size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        self._solve(png)

    # ---- Solving ------------------------------------------------------------

    def _solve(self, png: bytes):
        if genai is None:
            self._show_error("The google-genai package is not installed.\n\n"
                             "Run: pip install google-genai")
            return
        key = self.api_key()
        if not key:
            self._show_error("No Gemini API key set. Add one in 🔑 Settings, then capture again.",
                             status="No API key")
            if not self.open_settings():
                return
            key = self.api_key()
            if not key:
                return
        self._job_id += 1
        self._answer = ""
        self.output.clear()
        self.set_status("Solving...")
        self.solver.solve(self._job_id, key, png)

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
