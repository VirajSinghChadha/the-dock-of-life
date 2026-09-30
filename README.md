# The Dock of Life

MathSnap Dock: a bottom-anchored desktop bar that lets you drag a box around any math problem on screen and get a step-by-step solution from Google Gemini (`gemini-2.5-flash`).

## Setup

```bash
pip install PyQt6 pillow google-genai "pynput>=1.7.7"
python3 mathsnap_dock.py
```

Get a Gemini API key at https://aistudio.google.com/apikey and paste it via the 🔑 Settings button, or set `GEMINI_API_KEY`.

On macOS, grant the app that launches Python (Terminal, iTerm, ...) **Screen Recording**, **Accessibility** and **Input Monitoring** under System Settings → Privacy & Security.

## Usage

- `Option+Space` / `Alt+Space`: show or hide the dock
- 📸 Capture Math: drag a red box around a problem (Esc or right-click cancels)
- ✕: hide the dock
- `Cmd+Q` / `Ctrl+Q`: quit
