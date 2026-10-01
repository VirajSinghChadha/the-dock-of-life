# The Dock of Life

**MathSnap Dock** is a bottom-anchored desktop bar: drag a box around any math problem on your screen and get a step-by-step solution, using an AI provider that costs nothing.

![MathSnap Dock](docs/dock.png)

## Free AI options

Pick one in **🔑 Settings**. The model name is editable for every provider.

| Provider | Cost | Key | Default model |
| --- | --- | --- | --- |
| Google Gemini | Free tier, daily limits | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | `gemini-2.5-flash` |
| Groq | Free tier, rate limited | [console.groq.com/keys](https://console.groq.com/keys) | `meta-llama/llama-4-scout-17b-16e-instruct` |
| OpenRouter | Free models | [openrouter.ai/keys](https://openrouter.ai/keys) | `openrouter/free` |
| Ollama | Free, local, offline, no account | none | `gemma3:4b` |

Keys can also be supplied as `GEMINI_API_KEY`, `GROQ_API_KEY` or `OPENROUTER_API_KEY`. For Ollama, install it from [ollama.com](https://ollama.com/download) and run `ollama pull gemma3:4b`.

<img src="docs/settings.png" width="420" alt="Settings dialog">

## Download (macOS)

**[Download MathSnapDock-arm64.dmg](https://github.com/VirajSinghChadha/the-dock-of-life/releases/latest/download/MathSnapDock-arm64.dmg)** for Apple Silicon Macs. Open it and drag **MathSnap Dock** into Applications.

The app is not notarized by Apple, so the first launch is blocked: open it once, then go to System Settings → Privacy & Security and click **Open Anyway**. Grant it Screen Recording, Accessibility and Input Monitoring there too.

To build the DMG yourself (for example on an Intel Mac), run `./build_dmg.sh`.

## Run from source

```bash
pip install PyQt6 pillow google-genai "pynput>=1.7.7"
python3 mathsnap_dock.py
```

On macOS, grant the app that launches Python (Terminal, iTerm, ...) **Screen Recording**, **Accessibility** and **Input Monitoring** under System Settings → Privacy & Security, then restart it.

## Usage

- `Option+Space` / `Alt+Space`: show or hide the dock
- `Ctrl+Option+S` / `Ctrl+Alt+S`: snip straight away, even while the dock is hidden
- 📸 Capture Math: drag a red box around a problem on whichever display the pointer is on (Esc or right-click cancels)
- 📋 Paste image: solve a screenshot from the clipboard (on macOS, `Shift+Cmd+Ctrl+4` copies a region). You can also drop an image file onto the dock.
- Mode: *Step by step*, *Answer only*, *Hint only* (no spoilers) or *Explain simply*
- Follow-up box: ask a question about the current problem; the model sees the snip and the earlier answers
- History ▾: reopen any of the last 20 problems from this session (kept in memory only)
- ↻ Retry / ■ Stop: re-solve, e.g. after switching provider or mode, or stop a running answer
- Copy: copies the latest answer, with LaTeX turned into readable text
- 🔑 Settings → **Test connection**: checks the provider, model and key before you snip
- ✕: hide the dock
- ⏻ or `Cmd+Q` / `Ctrl+Q`: quit

The dock floats over every Space, including full-screen apps, so it has no Dock icon. If the global hotkeys can't work (pynput missing, or no Accessibility permission on macOS), the dock says so and ✕ quits instead of hiding.

## Snips show only the wallpaper?

That means macOS Screen Recording permission is missing. Enable **MathSnap Dock** under System Settings → Privacy & Security → Screen & System Audio Recording, then quit and reopen the app. After installing a new version, remove the old entry with the − button and add the app again.
