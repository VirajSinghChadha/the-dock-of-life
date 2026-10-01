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
#   Ctrl+Option+S             snip straight away, even while the dock is hidden
#   📸 Capture Math           drag a red box around a problem on any display;
#                             Esc/right-click cancels
#   📋 Paste / drag & drop    solve an image from the clipboard or a file
#   Mode                      full steps, answer only, a hint, or a beginner explanation
#   Follow-up box             ask a question about the current problem
#   History ▾                 reopen an earlier problem from this session
#   ↻ Retry / ■ Stop          re-solve (e.g. after switching provider) or stop
#   ✕                         hide the dock (Option+Space brings it back)
#   ⏻  or Cmd+Q (Ctrl+Q)      quit (the app has no Dock icon, so it can float over
#                             full-screen apps)
# =============================================================================

import base64
import contextlib
import ctypes
import ctypes.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from PyQt6.QtCore import (
    QBuffer, QEasingCurve, QIODevice, QObject, QPoint, QPropertyAnimation, QRect,
    QSettings, Qt, QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QColor, QCursor, QGuiApplication, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout,
    QLabel, QLineEdit, QMenu, QPushButton, QSizePolicy, QTextBrowser, QVBoxLayout, QWidget,
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
# Solve modes: id -> (menu label, instruction sent with the image)
MODES = {
    "steps": ("Step by step", PROMPT),
    "answer": ("Answer only", (
        "You are an expert Math Tutor. Identify the mathematical problem in this cropped image "
        "and give only the final answer, with at most one short line of justification.")),
    "hint": ("Hint only", (
        "You are a patient Math Tutor. Identify the mathematical problem in this cropped image. "
        "Do NOT solve it and do NOT reveal the final answer. Give one helpful hint for the next "
        "step, and name the concept or formula that applies.")),
    "explain": ("Explain simply", (
        "You are a friendly Math Tutor talking to a beginner. Identify the mathematical problem "
        "in this cropped image, explain the idea behind it in plain words, then solve it in small "
        "steps, explaining why each step works. End with the final answer.")),
}
DEFAULT_MODE = "steps"
# The solution view renders Markdown but not LaTeX, so steer the output format.
FORMAT_HINT = (
    "Format the reply in Markdown. Write math in plain text / Unicode "
    "(e.g. x², √, ×, ÷, fractions as a/b) instead of LaTeX."
)


def mode_prompt(mode: str) -> str:
    return MODES.get(mode, MODES[DEFAULT_MODE])[1] + "\n\n" + FORMAT_HINT

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

APP_VERSION = "1.1.1"
# Shown once in the dock to existing users after they update.
WHATS_NEW = (
    f"**🎉 MathSnap Dock was updated to {APP_VERSION}**\n\n"
    "- Snip on any display, with sharper Retina snips\n"
    "- {capture} snips straight away, even with the dock hidden\n"
    "- 📋 Paste image, or drop an image file onto the dock\n"
    "- Modes: Step by step, Answer only, Hint only, Explain simply\n"
    "- Ask follow-up questions, ■ Stop, History ▾, readable math\n"
    "- 🔑 Settings → Test connection\n\n"
    "If snips show only your wallpaper, re-add MathSnap Dock under System Settings → "
    "Privacy & Security → Screen & System Audio Recording, then reopen the app."
)

HOTKEY = "<alt>+<space>"
CAPTURE_HOTKEY = "<ctrl>+<alt>+s"
TOGGLE_KEYS = "⌥Space" if sys.platform == "darwin" else "Alt+Space"
CAPTURE_KEYS = "⌃⌥S" if sys.platform == "darwin" else "Ctrl+Alt+S"
PANEL_HEIGHT = 262
PANEL_WIDTH_RATIO = 0.75
SHADOW_MARGIN = 18
MIN_SELECTION = 8      # logical px; anything smaller counts as a cancelled snip
MAX_IMAGE_SIDE = 1600  # px; larger snips are downscaled before upload
MAX_HISTORY = 20       # problems kept (in memory only) for the History menu
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic")

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
#mini::menu-indicator { image: none; width: 0; }
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
QMenu {
    background: #211f2e; color: #ecebf5; border: 1px solid rgba(255, 255, 255, 30);
    border-radius: 8px; padding: 4px;
}
QMenu::item { padding: 6px 14px; border-radius: 6px; }
QMenu::item:selected { background: #7c3aed; }
QMenu::item:disabled { color: #6f6e82; }
"""

STATUS_COLORS = {"Ready": "#34d399", "Capturing...": "#fbbf24", "Solving...": "#a78bfa",
                 "Stopped": "#9d9cb3", "No image": "#fbbf24", "Updated": "#34d399"}


# ---- macOS native helpers (ctypes, no extra dependency) ----------------------
# Qt alone cannot put a window on top of another app's full-screen Space or check
# the Screen Recording permission, so these talk to AppKit / CoreGraphics directly.

IS_MAC = sys.platform == "darwin"
MAC_LEVEL_PANEL = 25     # NSStatusWindowLevel: above normal and full-screen windows
MAC_LEVEL_OVERLAY = 101  # NSPopUpMenuWindowLevel: above the panel, Dock and menu bar
_objc = None
if IS_MAC:
    try:
        _objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        _objc.objc_getClass.restype = ctypes.c_void_p
        _objc.objc_getClass.argtypes = [ctypes.c_char_p]
        _objc.sel_registerName.restype = ctypes.c_void_p
        _objc.sel_registerName.argtypes = [ctypes.c_char_p]
    except Exception:
        _objc = None


def _mac_send(obj, selector: str, *args, restype=ctypes.c_void_p, argtypes=()):
    send = ctypes.cast(_objc.objc_msgSend,
                       ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes))
    return send(obj, _objc.sel_registerName(selector.encode()), *args)


def mac_run_as_accessory():
    """No Dock icon / menu bar, which lets our windows sit over other apps' full-screen Spaces."""
    if not _objc:
        return
    try:
        app = _mac_send(_objc.objc_getClass(b"NSApplication"), "sharedApplication")
        _mac_send(app, "setActivationPolicy:", 1, restype=ctypes.c_bool, argtypes=(ctypes.c_long,))
    except Exception:
        pass


def mac_float_everywhere(widget, level: int):
    """Show this window on every Space, including over full-screen apps. Call after show()."""
    # winId() is only an NSView on the Cocoa platform plugin; messaging anything else crashes.
    if not _objc or QGuiApplication.platformName() != "cocoa":
        return
    try:
        window = _mac_send(int(widget.winId()), "window")
        if not window:
            return
        # canJoinAllSpaces | fullScreenAuxiliary — the documented combination for a
        # utility window that must float over *every* Space, including one currently
        # occupied by a native full-screen app. (Adding `stationary` here, as a
        # previous version did, keeps the window from following the user onto a
        # full-screen Space at all on current macOS.)
        _mac_send(window, "setCollectionBehavior:", 1 | 256,
                  restype=None, argtypes=(ctypes.c_ulong,))
        _mac_send(window, "setLevel:", level, restype=None, argtypes=(ctypes.c_long,))
        _mac_send(window, "setHidesOnDeactivate:", False, restype=None, argtypes=(ctypes.c_bool,))
    except Exception:
        pass


def mac_activate_app():
    """Bring our accessory app to the front so its windows receive key presses (e.g. Esc)."""
    if not _objc:
        return
    try:
        app = _mac_send(_objc.objc_getClass(b"NSApplication"), "sharedApplication")
        _mac_send(app, "activateIgnoringOtherApps:", True, restype=None, argtypes=(ctypes.c_bool,))
    except Exception:
        pass


def mac_input_monitoring_allowed() -> bool:
    """False if macOS will silently swallow our global hotkeys (no Accessibility permission)."""
    if not IS_MAC:
        return True
    try:
        ax = ctypes.cdll.LoadLibrary(ctypes.util.find_library("ApplicationServices"))
        ax.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(ax.AXIsProcessTrusted())
    except Exception:
        return True


def mac_screen_capture_allowed() -> bool:
    """True if macOS lets us record other apps' windows. Prompts the user the first time."""
    if not IS_MAC:
        return True
    try:
        cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        if cg.CGPreflightScreenCaptureAccess():
            return True
        cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
        return bool(cg.CGRequestScreenCaptureAccess())
    except Exception:
        return True  # very old macOS: no such API, and no permission needed


def screen_under_cursor():
    return QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()


def _grab_screen_cli(screen) -> QPixmap:
    """Shell out to macOS's own `screencapture` tool, which (unlike Qt's grabWindow)
    keeps working across rebuilds of an ad-hoc-signed app whose code signature
    changes every time, and reliably includes full-screen apps' windows."""
    geo = screen.geometry()
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-R",
             f"{geo.x()},{geo.y()},{geo.width()},{geo.height()}", path],
            capture_output=True, timeout=15, check=False)
        shot = QPixmap(path)
        if not shot.isNull():
            shot.setDevicePixelRatio(shot.width() / max(1, geo.width()))
        return shot
    except Exception:
        return QPixmap()
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)  # the frozen frame lives in memory only


def grab_screen(screen) -> QPixmap:
    """Screenshot one display.

    On macOS, Qt's grabWindow() uses a deprecated API that — once the Screen
    Recording permission is missing or stale (very common for an ad-hoc-signed
    app whose signature changes on every rebuild) — silently returns a
    *non-null* image containing only the desktop picture and Dock, with every
    other app's window invisible. That looks like success, so Qt is only used
    as a fallback here; `screencapture` is the primary path and either raises
    the real permission prompt or produces a complete capture.
    """
    if IS_MAC:
        shot = _grab_screen_cli(screen)
        if not shot.isNull():
            return shot
        return screen.grabWindow(0)
    return screen.grabWindow(0)


def pixmap_from_mime(mime):
    """An image from a clipboard / drag-and-drop payload: image files first, then raw pixels."""
    if mime is None:
        return None
    for url in mime.urls():
        if url.isLocalFile() and url.toLocalFile().lower().endswith(IMAGE_SUFFIXES):
            pix = QPixmap(url.toLocalFile())
            if not pix.isNull():
                return pix
    if mime.hasImage():
        data = mime.imageData()
        if isinstance(data, QImage) and not data.isNull():
            return QPixmap.fromImage(data)
        if isinstance(data, QPixmap) and not data.isNull():
            return data
    return None


def mime_has_image(mime) -> bool:
    return mime.hasImage() or any(
        u.isLocalFile() and u.toLocalFile().lower().endswith(IMAGE_SUFFIXES) for u in mime.urls())


# ---- LaTeX cleanup -------------------------------------------------------------
# Models often answer in LaTeX despite FORMAT_HINT; QTextBrowser can't render it, so
# turn the common commands into readable Unicode.

_LATEX_SYMBOLS = {
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓", "le": "≤", "leq": "≤",
    "ge": "≥", "geq": "≥", "neq": "≠", "ne": "≠", "approx": "≈", "equiv": "≡", "infty": "∞",
    "pi": "π", "theta": "θ", "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "Delta": "Δ", "lambda": "λ", "mu": "μ", "sigma": "σ", "Sigma": "Σ", "omega": "ω",
    "phi": "φ", "epsilon": "ε", "to": "→", "rightarrow": "→", "Rightarrow": "⇒",
    "implies": "⇒", "iff": "⇔", "sum": "∑", "prod": "∏", "int": "∫", "partial": "∂",
    "nabla": "∇", "circ": "°", "degree": "°", "angle": "∠", "perp": "⊥", "parallel": "∥",
    "in": "∈", "notin": "∉", "subset": "⊂", "cup": "∪", "cap": "∩", "emptyset": "∅",
    "forall": "∀", "exists": "∃", "therefore": "∴", "cdots": "⋯", "ldots": "…", "dots": "…",
    "left": "", "right": "", "displaystyle": "", "quad": "  ", "qquad": "    ",
    "sin": "sin", "cos": "cos", "tan": "tan", "log": "log", "ln": "ln", "lim": "lim",
}
_SUPERSCRIPT = str.maketrans("0123456789+-=()nix", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱˣ")
_SUBSCRIPT = str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")
_GROUP = r"\{([^{}]*)\}"


def _script(text: str, table, marker: str) -> str:
    converted = text.translate(table)
    if all(ord(ch) > 127 for ch in converted):
        return converted
    return f"{marker}({text})" if len(text) > 1 else marker + text


def _wrap(text: str) -> str:
    text = text.strip()
    return text if re.fullmatch(r"[\w.√²³]+", text) else f"({text})"


def latex_to_text(text: str) -> str:
    if "\\" not in text and "$" not in text:
        return text
    for _ in range(8):  # innermost braces first, so nested commands unwrap step by step
        before = text
        text = re.sub(r"\\[dt]?frac" + _GROUP + _GROUP,
                      lambda m: f"{_wrap(m[1])}/{_wrap(m[2])}", text)
        text = re.sub(r"\\sqrt\[([^\]]*)\]" + _GROUP,
                      lambda m: f"{m[1].translate(_SUPERSCRIPT)}√{_wrap(m[2])}", text)
        text = re.sub(r"\\sqrt" + _GROUP, lambda m: f"√{_wrap(m[1])}", text)
        text = re.sub(r"\\boxed" + _GROUP, r"**\1**", text)
        text = re.sub(r"\\(?:text|mathrm|mathbf|mathit|operatorname|textbf)" + _GROUP, r"\1", text)
        text = re.sub(r"\^" + _GROUP, lambda m: _script(m[1], _SUPERSCRIPT, "^"), text)
        text = re.sub(r"_" + _GROUP, lambda m: _script(m[1], _SUBSCRIPT, "_"), text)
        if text == before:
            break
    text = re.sub(r"\\(?:begin|end)\{[a-z*]+\}", "", text)
    text = re.sub(r"\s*\\\\\s*", "\n\n", text)  # \\ line breaks
    text = re.sub(r"\s*&\s*([=<>≤≥≈])", r" \1", text)  # aligned-environment anchors
    text = re.sub(r"\^([0-9n])", lambda m: m[1].translate(_SUPERSCRIPT), text)
    text = re.sub(r"\^\\circ", "°", text)
    text = re.sub(r"\\([A-Za-z]+)", lambda m: _LATEX_SYMBOLS.get(m[1], m[0]), text)
    text = re.sub(r"\\[,;:! ]", " ", text)
    text = text.replace("\\{", "{").replace("\\}", "}")
    text = re.sub(r"\\\[|\\\]|\\\(|\\\)|\$\$", "", text)
    text = re.sub(r"\$(?=\S)([^$\n]+?)(?<=\S)\$(?!\d)", r"\1", text)
    return text


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
    """Marshals the pynput listener thread's callbacks onto the Qt main thread."""
    triggered = pyqtSignal()
    capture = pyqtSignal()


class Solver(QObject):
    """Streams a model response from a background thread.

    `turns` is the conversation so far: dicts with "role" ("user" / "assistant"), "text" and,
    for the turn that carries the snip, "image": True.
    """
    chunk = pyqtSignal(int, str)
    finished = pyqtSignal(int)
    failed = pyqtSignal(int, str)

    def solve(self, job_id: int, provider_id: str, model: str, api_key: str,
              png, turns: list) -> None:
        threading.Thread(target=self._run, args=(job_id, provider_id, model, api_key, png, turns),
                         daemon=True).start()

    def _run(self, job_id, provider_id, model, api_key, png, turns):
        provider = PROVIDERS[provider_id]
        try:
            png = shrink_png(png) if png else None
            if provider["kind"] == "gemini":
                pieces = self._gemini(model, api_key, png, turns)
            else:
                pieces = self._openai_compatible(provider["base_url"], model, api_key, png, turns)
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
    def _gemini(model, api_key, png, turns):
        if genai is None:
            raise RuntimeError("The google-genai package is not installed. Run: pip install google-genai")
        client = genai.Client(api_key=api_key)
        contents = []
        for turn in turns:
            parts = []
            if turn.get("image") and png:
                parts.append(genai_types.Part.from_bytes(data=png, mime_type="image/png"))
            parts.append(genai_types.Part.from_text(text=turn["text"]))
            contents.append(genai_types.Content(
                role="user" if turn["role"] == "user" else "model", parts=parts))
        for part in client.models.generate_content_stream(model=model, contents=contents):
            yield part.text

    @staticmethod
    def _openai_compatible(base_url, model, api_key, png, turns):
        messages = []
        for turn in turns:
            content = turn["text"]
            if turn.get("image") and png:
                content = [
                    {"type": "text", "text": turn["text"]},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}},
                ]
            messages.append({"role": turn["role"], "content": content})
        body = json.dumps({"model": model, "stream": True, "messages": messages}).encode("utf-8")
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
        # QRect(QPoint, QPoint) counts both corners in, which makes the box 1px too big.
        x1, x2 = sorted((self._origin.x(), self._current.x()))
        y1, y2 = sorted((self._origin.y(), self._current.y()))
        return QRect(x1, y1, x2 - x1, y2 - y1).intersected(self.rect())

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
        self._test_job = 0
        self._test_reply = ""
        self._tester = Solver()
        self._tester.chunk.connect(self._on_test_chunk)
        self._tester.finished.connect(self._on_test_done)
        self._tester.failed.connect(self._on_test_failed)
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

        self.test_result = QLabel(objectName="note")
        self.test_result.setWordWrap(True)
        self.test_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.test_result.hide()
        layout.addWidget(self.test_result)

        buttons = QHBoxLayout()
        self.test_btn = QPushButton("Test connection")
        self.test_btn.setToolTip("Send a tiny text request with these settings")
        self.test_btn.clicked.connect(self._test)
        buttons.addWidget(self.test_btn)
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
        self._test_job += 1
        self.test_btn.setEnabled(True)
        self.test_result.hide()

    def _test(self):
        pid = self._current
        info = PROVIDERS[pid]
        model = self.model_edit.text().strip() or info["model"]
        key = self.key_edit.text().strip()
        for name in info["env"]:
            key = key or os.environ.get(name, "").strip()
        if info["env"] and not key:
            self._show_test_result("Paste an API key first.", ok=False)
            return
        self._test_job += 1
        self._test_reply = ""
        self.test_btn.setEnabled(False)
        self._show_test_result(f"Testing {model}...", ok=None)
        self._tester.solve(self._test_job, pid, model, key, None,
                           [{"role": "user", "text": "Reply with exactly: OK"}])

    def _on_test_chunk(self, job_id, text):
        if job_id == self._test_job:
            self._test_reply += text

    def _on_test_done(self, job_id):
        if job_id == self._test_job:
            self.test_btn.setEnabled(True)
            reply = self._test_reply.strip().replace("\n", " ")[:80]
            self._show_test_result(f"✓ Connected. The model replied: {reply}", ok=True)

    def _on_test_failed(self, job_id, message):
        if job_id == self._test_job:
            self.test_btn.setEnabled(True)
            self._show_test_result("✗ " + message, ok=False)

    def _show_test_result(self, text, ok):
        color = {True: "#34d399", False: "#fb7185"}.get(ok, "#9d9cb3")
        self.test_result.setStyleSheet(f"color: {color};")
        self.test_result.setText(text)
        self.test_result.show()

    def done(self, result):
        # A test request may still be running; make sure it can't touch this closed dialog.
        self._test_job += 1
        for signal in (self._tester.chunk, self._tester.finished, self._tester.failed):
            with contextlib.suppress(TypeError, RuntimeError):
                signal.disconnect()
        super().done(result)

    def _save(self):
        self._stash()
        settings = self.dock.settings
        for pid, (model, key) in self._pending.items():
            settings.setValue(f"{pid}/model", model)
            settings.setValue(f"{pid}/api_key", key)
        settings.setValue("provider", self._current)
        settings.sync()
        self.accept()


class Session:
    """One problem: the snip plus the conversation about it. Kept in memory only."""

    def __init__(self, png: bytes, pixmap: QPixmap, mode: str):
        self.png = png
        self.pixmap = pixmap
        self.created = time.strftime("%H:%M")
        self.turns = [{"role": "user", "text": mode_prompt(mode), "image": True}]

    def last_answer(self) -> str:
        for turn in reversed(self.turns):
            if turn["role"] == "assistant" and turn["text"]:
                return turn["text"]
        return ""

    def title(self) -> str:
        for line in latex_to_text(self.last_answer()).splitlines():
            line = line.strip(" #*>-_`")
            if line:
                return line[:48] + ("…" if len(line) > 48 else "")
        return "(no answer)"


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
        self._job_turn = None   # assistant turn the running job streams into
        self._job_session = None
        self._session = None    # problem currently shown
        self._history = []      # earlier problems this session, oldest first
        self._overlay = None
        self._anim = None
        self._visible = False
        self.hotkey_available = True

        self.setWindowTitle("MathSnap Dock")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Qt.Tool windows vanish on macOS when the app loses focus unless this is set.
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.setAcceptDrops(True)
        self.setStyleSheet(STYLE)
        self._build_ui()
        QShortcut(QKeySequence.StandardKey.Quit, self, activated=QApplication.quit)
        # A focused text field handles Cmd+V itself, so this only fires elsewhere in the dock.
        QShortcut(QKeySequence.StandardKey.Paste, self, activated=self.paste_image)
        self.set_status("Ready")
        self._refresh_badge()
        self._update_buttons()

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
        self.capture_btn.setToolTip(f"Snip a problem on any display ({CAPTURE_KEYS})")
        self.capture_btn.clicked.connect(self.start_capture)
        self.thumb = QLabel("Your snip appears here\nor drop / paste an image", objectName="thumb")
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setFixedWidth(190)
        self.thumb.setMinimumHeight(70)
        left.addWidget(self.capture_btn)
        left.addWidget(self.thumb, 1)
        row.addLayout(left)

        # Middle: solution and follow-up box
        middle = QVBoxLayout()
        middle.setSpacing(6)
        head = QHBoxLayout()
        head.addWidget(QLabel("SOLUTION", objectName="section"))
        self.badge = QLabel(objectName="badge")
        self.badge.setMaximumWidth(280)
        # Let the badge shrink before the buttons next to it get squashed on small screens.
        self.badge.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.badge.setMinimumWidth(60)
        head.addWidget(self.badge)
        head.addStretch(1)
        self.history_btn = QPushButton("History ▾", objectName="mini")
        self.history_btn.setToolTip("Reopen an earlier problem from this session")
        self.history_menu = QMenu(self)
        self.history_menu.aboutToShow.connect(self._fill_history_menu)
        self.history_btn.setMenu(self.history_menu)
        self.retry_btn = QPushButton("↻ Retry", objectName="mini")
        self.retry_btn.clicked.connect(self.retry_or_stop)
        self.copy_btn = QPushButton("Copy", objectName="mini")
        self.copy_btn.clicked.connect(self.copy_answer)
        head.addWidget(self.history_btn)
        head.addWidget(self.retry_btn)
        head.addWidget(self.copy_btn)
        middle.addLayout(head)
        self.output = QTextBrowser()
        self.output.setOpenExternalLinks(True)
        self.output.setPlaceholderText(
            "Capture a math problem and the step-by-step solution will appear here.\n"
            f"Tip: press {TOGGLE_KEYS} anywhere to show or hide this dock, "
            f"or {CAPTURE_KEYS} to snip straight away.")
        middle.addWidget(self.output, 1)
        self.followup = QLineEdit()
        self.followup.setPlaceholderText("Ask a follow-up about this problem, then press Enter")
        self.followup.returnPressed.connect(self.ask_followup)
        middle.addWidget(self.followup)
        row.addLayout(middle, 1)

        # Right: status, close, mode, paste, settings
        right = QVBoxLayout()
        right.setSpacing(8)
        top = QHBoxLayout()
        self.status = QLabel(objectName="pill")
        top.addWidget(self.status)
        top.addStretch(1)
        close_btn = QPushButton("✕", objectName="ghost")
        close_btn.setToolTip(f"Hide ({TOGGLE_KEYS} to show again)")
        close_btn.clicked.connect(self.close_clicked)
        quit_btn = QPushButton("⏻", objectName="ghost")
        quit_btn.setToolTip("Quit MathSnap Dock")
        quit_btn.clicked.connect(QApplication.quit)
        top.addWidget(quit_btn)
        top.addWidget(close_btn)
        right.addLayout(top)
        self.mode_box = QComboBox()
        self.mode_box.setToolTip("How much of the solution to show")
        for mode_id, (label, _) in MODES.items():
            self.mode_box.addItem(label, mode_id)
        self.mode_box.setCurrentIndex(max(0, self.mode_box.findData(self.mode())))
        self.mode_box.currentIndexChanged.connect(
            lambda: self.settings.setValue("mode", self.mode_box.currentData()))
        right.addWidget(self.mode_box)
        paste_btn = QPushButton("📋  Paste image")
        paste_btn.setToolTip("Solve an image from the clipboard. You can also drop an image file here.")
        paste_btn.clicked.connect(self.paste_image)
        right.addWidget(paste_btn)
        right.addStretch(1)
        hint = QLabel(f"{TOGGLE_KEYS} toggle  ·  {CAPTURE_KEYS} snip", objectName="sub")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right.addWidget(hint)
        settings_btn = QPushButton("🔑  Settings")
        settings_btn.clicked.connect(self.open_settings)
        right.addWidget(settings_btn)
        holder = QWidget()
        holder.setLayout(right)
        holder.setFixedWidth(190)
        row.addWidget(holder)

    def set_status(self, text: str):
        color = STATUS_COLORS.get(text, "#fb7185")
        self.status.setText(f'<span style="color:{color}">●</span>&nbsp;&nbsp;{text}')

    def _refresh_badge(self):
        pid = self.provider_id()
        name = PROVIDERS[pid]["label"].split("·")[0].strip()
        self.badge.setText(f"{name} · {self.model_for(pid)}")

    def _solving(self) -> bool:
        return self._job_turn is not None

    def _update_buttons(self):
        solving = self._solving()
        has_session = self._session is not None
        self.retry_btn.setText("■ Stop" if solving else "↻ Retry")
        self.retry_btn.setToolTip("Stop this answer" if solving else
                                  "Ask again, e.g. after switching provider or mode")
        self.retry_btn.setEnabled(solving or has_session)
        self.copy_btn.setEnabled(bool(has_session and self._session.last_answer()))
        self.history_btn.setEnabled(bool(self._history))
        self.followup.setEnabled(has_session and not solving)

    def _set_thumb(self, pixmap: QPixmap):
        # Scale in device pixels so the preview stays sharp on Retina screens.
        dpr = self.devicePixelRatioF()
        size = self.thumb.contentsRect().size()
        scaled = pixmap.scaled(round(size.width() * dpr), round(size.height() * dpr),
                               Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(dpr)
        self.thumb.setPixmap(scaled)

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
        mac_float_everywhere(self, MAC_LEVEL_PANEL)
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
        if self._overlay is not None or QApplication.activeModalWidget() is not None:
            return  # mid-snip or in Settings; ignore the hotkey
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

    def mode(self) -> str:
        mode = self.settings.value("mode", DEFAULT_MODE, type=str)
        return mode if mode in MODES else DEFAULT_MODE

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
            if not self._solving():
                self.set_status("Ready")
        return saved

    # ---- Getting an image: snip, paste, drop ----------------------------------

    def start_capture(self):
        if self._overlay is not None or QApplication.activeModalWidget() is not None:
            return
        if not mac_screen_capture_allowed():
            # Without this permission macOS hands back the wallpaper with every window removed.
            self.slide_in()
            self._show_error(
                "macOS is blocking screen capture, so a snip would show only your wallpaper.\n\n"
                "Open **System Settings → Privacy & Security → Screen & System Audio Recording**, "
                "switch on **MathSnap Dock** (or the terminal you launched it from), then quit "
                "with ⏻ and reopen the app.\n\n"
                "If it is already switched on, remove it from that list with the − button, "
                "add it again, and reopen the app.\n\n"
                "Meanwhile you can take a screenshot with ⇧⌘⌃4 and use 📋 Paste image.",
                status="Permission needed")
            return
        self.set_status("Capturing...")
        self.capture_btn.setEnabled(False)
        screen = screen_under_cursor()  # snip whichever display the pointer is on
        if self._anim is not None:
            self._anim.stop()
        self.hide()
        # Give the window server time to actually remove the panel from the screen.
        QTimer.singleShot(250, lambda: self._grab_and_show_overlay(screen))

    def _grab_and_show_overlay(self, screen):
        if screen not in QGuiApplication.screens():  # display unplugged meanwhile
            screen = QGuiApplication.primaryScreen()
        shot = grab_screen(screen)
        if shot.isNull():
            self._restore_panel()
            self._show_error("Could not capture the screen. On macOS, grant Screen Recording "
                             "permission to MathSnap Dock (or the app running Python), then "
                             "restart it. You can still use 📋 Paste image meanwhile.")
            return
        self._overlay = SnipOverlay(screen, shot)
        self._overlay.selected.connect(self._on_snip)
        self._overlay.cancelled.connect(self._on_snip_cancelled)
        self._overlay.show()
        mac_float_everywhere(self._overlay, MAC_LEVEL_OVERLAY)
        mac_activate_app()  # otherwise Esc goes to whatever app was in front
        self._overlay.raise_()
        self._overlay.activateWindow()
        self._overlay.setFocus()

    def _restore_panel(self):
        self._overlay = None
        self.capture_btn.setEnabled(True)
        self.slide_in()

    def _on_snip_cancelled(self):
        self._restore_panel()
        self.set_status("Solving..." if self._solving() else "Ready")

    def _on_snip(self, crop: QPixmap):
        self._restore_panel()
        self.new_problem(crop)

    def paste_image(self):
        pixmap = pixmap_from_mime(QGuiApplication.clipboard().mimeData())
        if pixmap is None:
            self.set_status("No image")
            self.output.setMarkdown(
                "**The clipboard has no image.** Copy a screenshot (on macOS ⇧⌘⌃4 copies a "
                "region straight to the clipboard) or an image file, then press 📋 Paste image.")
            return
        self.slide_in()
        self.new_problem(pixmap)

    def dragEnterEvent(self, event):
        if mime_has_image(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        pixmap = pixmap_from_mime(event.mimeData())
        if pixmap is not None:
            event.acceptProposedAction()
            self.new_problem(pixmap)

    # ---- Problems, history and follow-ups ------------------------------------

    def new_problem(self, pixmap: QPixmap):
        png = pixmap_to_png(pixmap)  # in-memory buffer; nothing is written to disk
        session = Session(png, pixmap, self.mode())
        self._history.append(session)
        del self._history[:-MAX_HISTORY]
        self._show_session(session)
        self._solve()

    def _show_session(self, session: Session):
        self._session = session
        self._set_thumb(session.pixmap)
        self.followup.clear()
        self._render()
        self._update_buttons()

    def _fill_history_menu(self):
        self.history_menu.clear()
        for session in reversed(self._history):
            action = self.history_menu.addAction(f"{session.created}   {session.title()}")
            action.setCheckable(True)
            action.setChecked(session is self._session)
            action.triggered.connect(lambda _=False, s=session: self._show_session(s))
        self.history_menu.addSeparator()
        clear = self.history_menu.addAction("Clear history")
        clear.triggered.connect(self._clear_history)

    def _clear_history(self):
        self._stop()
        self._history.clear()
        self._session = None
        self.thumb.clear()
        self.thumb.setText("Your snip appears here\nor drop / paste an image")
        self.output.clear()
        self.set_status("Ready")
        self._update_buttons()

    def ask_followup(self):
        question = self.followup.text().strip()
        if not question or self._session is None or self._solving():
            return
        self.followup.clear()
        self._session.turns.append({"role": "user", "text": question})
        self._solve()

    def retry_or_stop(self):
        if self._solving():
            self._stop()
            return
        session = self._session
        if session is None:
            return
        if session.turns[-1]["role"] == "assistant":
            session.turns.pop()
        if len(session.turns) == 1:  # first answer: pick up a newly chosen mode too
            session.turns[0]["text"] = mode_prompt(self.mode())
        self._solve()

    def _stop(self):
        if not self._solving():
            return
        self._job_id += 1  # the background stream keeps running but is ignored from now on
        self._job_turn["text"] += "\n\n*(stopped)*"
        self._job_turn = None
        self.set_status("Stopped")
        self._render()
        self._update_buttons()

    def copy_answer(self):
        answer = self._session.last_answer() if self._session else ""
        if answer:
            QGuiApplication.clipboard().setText(latex_to_text(answer))
            self.copy_btn.setText("Copied ✓")
            QTimer.singleShot(1200, lambda: self.copy_btn.setText("Copy"))

    # ---- Solving ------------------------------------------------------------

    def _solve(self):
        session = self._session
        pid = self.provider_id()
        if PROVIDERS[pid]["env"] and not self.api_key(pid):
            self._show_error("No API key set for this provider. Add a free one in 🔑 Settings "
                             "(or switch to Ollama, which needs no key), then press ↻ Retry.",
                             status="No API key")
            if not self.open_settings() or session is not self._session:
                return
            pid = self.provider_id()
            if PROVIDERS[pid]["env"] and not self.api_key(pid):
                return
        self._stop()
        # Earlier replies that failed or were empty would only confuse the model.
        turns = [{k: v for k, v in t.items() if k != "error"} for t in session.turns
                 if t["role"] == "user" or (t["text"] and not t.get("error"))]
        self._job_id += 1
        self._job_turn = {"role": "assistant", "text": ""}
        session.turns.append(self._job_turn)
        self._job_session = session
        self.set_status("Solving...")
        self._render()
        self._update_buttons()
        self.solver.solve(self._job_id, pid, self.model_for(pid), self.api_key(pid),
                          session.png, turns)

    def _render(self, keep_scroll=False):
        session = self._session
        if session is None:
            return
        parts = []
        for i, turn in enumerate(session.turns):
            if turn["role"] == "user":
                if i > 0:
                    parts.append(f"**🙋 {turn['text']}**")
                continue
            body = latex_to_text(turn["text"])
            if turn.get("error"):
                body = (body + "\n\n---\n\n" if body else "") + f"**⚠️ Error**\n\n{turn['error']}"
            elif not body and turn is self._job_turn:
                body = "*Thinking…*"
            parts.append(body)
        bar = self.output.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        old_value = bar.value()
        self.output.setMarkdown("\n\n---\n\n".join(p for p in parts if p))
        if keep_scroll and not at_bottom:
            bar.setValue(old_value)
        else:
            bar.setValue(bar.maximum())

    def _on_chunk(self, job_id: int, text: str):
        if job_id != self._job_id:
            return  # stale stream from an earlier or stopped request
        self._job_turn["text"] += text
        if self._job_session is self._session:
            self._render(keep_scroll=True)

    def _on_finished(self, job_id: int):
        if job_id != self._job_id:
            return
        self._job_turn = None
        self.set_status("Ready")
        self._update_buttons()

    def _on_failed(self, job_id: int, message: str):
        if job_id != self._job_id:
            return
        self._job_turn["error"] = message
        self._job_turn = None
        self.set_status("Error")
        if self._job_session is self._session:
            self._render()
        self._update_buttons()

    def _show_error(self, message: str, status: str = "Error"):
        self.output.setMarkdown(f"**⚠️ {status}**\n\n{message}")
        self.set_status(status)


def start_hotkey(bridge: HotkeyBridge):
    """Register the global toggle and capture hotkeys. Returns the listener or None."""
    if pynput_keyboard is None or not mac_input_monitoring_allowed():
        return None  # on macOS pynput would start but never see a key press
    try:
        listener = pynput_keyboard.GlobalHotKeys({
            HOTKEY: bridge.triggered.emit,
            CAPTURE_HOTKEY: bridge.capture.emit,
        })
        listener.daemon = True
        listener.start()
        return listener
    except Exception:
        return None


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("MathSnap Dock")
    app.setQuitOnLastWindowClosed(False)  # hiding the dock must not quit the app
    mac_run_as_accessory()

    dock = Dock()
    bridge = HotkeyBridge()
    bridge.triggered.connect(dock.toggle)
    bridge.capture.connect(dock.start_capture)
    listener = start_hotkey(bridge)
    dock.slide_in()

    notices = []
    seen = dock.settings.value("last_version", "", type=str)
    is_existing_user = bool(seen) or "provider" in dock.settings.allKeys()
    if seen != APP_VERSION:
        dock.settings.setValue("last_version", APP_VERSION)
        if is_existing_user:
            notices.append(WHATS_NEW.replace("{capture}", CAPTURE_KEYS))
            dock.set_status("Updated")
    if listener is None:
        dock.hotkey_available = False
        notices.append(
            f"**⚠️ Hotkey off**\n\nThe global {TOGGLE_KEYS} and {CAPTURE_KEYS} hotkeys are "
            "unavailable.\n\n"
            "Install them with `pip install pynput` and, on macOS, switch on **MathSnap Dock** "
            "(or the app running Python) under **System Settings → Privacy & Security → "
            "Accessibility** and **Input Monitoring**, then reopen the app. Until then, the ✕ "
            "button quits the app instead of hiding it. 📸 Capture Math and 📋 Paste image "
            "work without these permissions.")
        dock.set_status("Hotkey off")
    if notices:
        dock.output.setMarkdown("\n\n---\n\n".join(notices))

    code = app.exec()
    if listener is not None:
        listener.stop()
    sys.exit(code)


if __name__ == "__main__":
    main()
