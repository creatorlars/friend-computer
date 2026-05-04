# Friend Computer v1.0.0

A software tool for emulating The Computer from the Paranoia roleplaying game.

## What is this?

**Friend Computer** is a PARANOIA "insane computer" terminal — a Game Master
tool that lets you type sentences at your laptop while the text appears in big
bold white-on-black on a secondary monitor and is spoken aloud through an
offline TTS voice, with optional FM-phaser / glitch audio effects.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # Windows
# source .venv/bin/activate           # macOS / Linux
pip install -r requirements.txt
```

## Run

```powershell
# Default: fullscreen on your secondary monitor (or only monitor if just one).
python insane_computer.py

# See which monitors were detected.
python insane_computer.py --list-monitors

# Force a specific monitor.
python insane_computer.py --monitor 1

# Test mode -- run in a resizable window on the primary monitor.
python insane_computer.py --windowed

# Tone things down.
python insane_computer.py --no-fx --no-glitch
python insane_computer.py --glitch 0.02      # subtle
python insane_computer.py --glitch 0.25      # very corrupted
python insane_computer.py --rate 140 --font "OCR A Extended"
python insane_computer.py --mute             # visual only
```

## Hotkeys

| Key            | Action                                           |
| -------------- | ------------------------------------------------ |
| `ESC`          | quit                                             |
| `F11`          | toggle fullscreen                                |
| `Backspace`    | delete a character                               |
| `Enter`        | force-commit the current sentence                |
| `Ctrl+U`       | erase the current input line                     |
| `Ctrl+L`       | clear the centered display                       |
| `. ! ? ; :`    | commit current sentence -> display + speak       |

## Soundboard

Edit `soundboard.json` to bind keyboard shortcuts (F-keys, Ctrl+digit,
Alt+letter, etc.) to pre-written phrases. Pressing the hotkey is identical
to typing the phrase and pressing Enter.

## Standalone Executables

Pre-built binaries for Windows, macOS, and Linux are published with each
[release](../../releases). Download and run — no Python installation required.

## Build from Source

```bash
pip install pyinstaller
pyinstaller friend_computer.spec
# Output: dist/friend-computer  (or dist/friend-computer.exe on Windows)
```

## Notes

- If `numpy`/`soundfile`/`sounddevice` aren't installed, audio still works
  via plain `pyttsx3.say()` — just no vibrato or stutter glitches.
- On Linux, `pyttsx3` requires `espeak-ng` (`sudo apt install espeak-ng`).
- If `screeninfo` isn't installed, monitor selection is disabled and the
  window opens wherever the OS puts it; use `--windowed` for testing.
- The TTS voice can be changed system-wide in Windows
  *Settings > Time & Language > Speech*.
