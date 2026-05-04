#!/usr/bin/env python3
"""
THE COMPUTER IS YOUR FRIEND.  TRUST THE COMPUTER.

PARANOIA "insane computer" front-end.

Two windows:
  * DISPLAY   -- 4:3 viewport on the chosen (typically secondary) monitor.
                 Big bold white-on-black text that reveals character-by-
                 character in lockstep with the spoken audio.
  * CONTROL   -- small window on your laptop with the input prompt and
                 instructions.  Keep keyboard focus here while typing.

Each terminating punctuation mark (. ! ? ; :) -- or Enter -- commits the
current sentence: it is rendered to WAV via offline TTS (Windows SAPI5),
treated with a phaser, and played back while the same text scrolls onto
the display window.

Hotkeys (either window focused)
  ESC          quit
  F11          toggle DISPLAY fullscreen on its monitor
  Backspace    delete character
  Ctrl+U       clear current input line
  Ctrl+L       clear display
"""
from __future__ import annotations

import argparse
import json
import subprocess
import math
import os
import queue
import random
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Optional

import pygame


def _resource_path(rel_path: str) -> str:
    """Return absolute path to a bundled resource.

    Works for both normal Python execution and PyInstaller frozen bundles
    (where data files are extracted to ``sys._MEIPASS``).
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, rel_path)  # type: ignore[attr-defined]
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), rel_path)


# --- Optional deps ---------------------------------------------------------
try:
    from screeninfo import get_monitors
except Exception:  # pragma: no cover
    get_monitors = None

try:
    import pyttsx3
except Exception:  # pragma: no cover
    pyttsx3 = None

try:
    import numpy as np
    import soundfile as sf
    import sounddevice as sd
    HAVE_AUDIO_FX = True
except Exception:  # pragma: no cover
    HAVE_AUDIO_FX = False


TERMINATORS = set(".!?;:")


# =========================================================================
# Monitors
# =========================================================================
@dataclass
class Monitor:
    index: int
    x: int
    y: int
    w: int
    h: int
    name: str = ""

    def __str__(self):
        return f"#{self.index}: {self.w}x{self.h} @ ({self.x},{self.y}) {self.name}"


def list_monitors() -> list[Monitor]:
    if get_monitors is None:
        return []
    return [Monitor(i, m.x, m.y, m.width, m.height,
                    getattr(m, "name", "") or "")
            for i, m in enumerate(get_monitors())]


def pick_monitor(args, mons):
    if not mons:
        return None
    if args.monitor is not None:
        if 0 <= args.monitor < len(mons):
            return mons[args.monitor]
        print(f"[warn] monitor {args.monitor} not found; using 0", file=sys.stderr)
        return mons[0]
    return mons[-1] if len(mons) > 1 else mons[0]


# =========================================================================
# Subliminal glitching (one-shot, applied at commit time)
# =========================================================================
LETTER_LOOKALIKES = {
    "o": "0", "0": "o", "i": "l", "l": "i", "I": "1", "1": "I",
    "S": "5", "5": "S", "B": "8", "8": "B", "g": "q", "q": "g",
    "u": "n", "n": "u",
}


def _swap_two_chars(word: str) -> str:
    if len(word) < 2:
        return word
    i = random.randrange(len(word) - 1)
    return word[:i] + word[i + 1] + word[i] + word[i + 2:]


def _reverse_word(word: str) -> str:
    if word and word[0].isupper() and len(word) > 1:
        return word[0] + word[:0:-1].lower()
    return word[::-1]


def _letter_lookalike(word: str) -> str:
    candidates = [i for i, c in enumerate(word) if c in LETTER_LOOKALIKES]
    if not candidates:
        return word
    i = random.choice(candidates)
    return word[:i] + LETTER_LOOKALIKES[word[i]] + word[i + 1:]


def subliminal_glitch(text: str, intensity: float = 1.0) -> str:
    """Occasionally tweak a single word or letter. Subtle by default."""
    if intensity <= 0 or not text.strip():
        return text
    words = text.split(" ")
    eligible = [i for i, w in enumerate(words)
                if sum(c.isalpha() for c in w) >= 3]

    # ~25% chance of a single word transform
    if eligible and random.random() < 0.25 * intensity:
        i = random.choice(eligible)
        w = words[i]
        head, tail = w, ""
        while head and not head[-1].isalnum():
            tail = head[-1] + tail
            head = head[:-1]
        op = random.choice(("swap", "swap", "reverse", "lookalike"))
        if op == "reverse" and len(head) >= 4:
            head = _reverse_word(head)
        elif op == "lookalike":
            head = _letter_lookalike(head)
        else:
            head = _swap_two_chars(head)
        words[i] = head + tail

    # ~15% chance of an additional single-letter lookalike elsewhere
    if eligible and random.random() < 0.15 * intensity:
        i = random.choice(eligible)
        words[i] = _letter_lookalike(words[i])

    return " ".join(words)


# =========================================================================
# Soundboard
# =========================================================================
@dataclass
class SoundboardEntry:
    key_str: str       # original e.g. "Shift+F1"
    label: str
    phrase: str
    mods: int          # required modifier mask (KMOD_CTRL etc.)
    key: int           # pygame key constant


_KEY_MAP_F = {f"f{i}": getattr(pygame, f"K_F{i}")
              for i in range(1, 16) if hasattr(pygame, f"K_F{i}")}
_KEY_MAP_DIGITS = {str(i): getattr(pygame, f"K_{i}") for i in range(0, 10)}
_KEY_MAP_LETTERS = {chr(c): getattr(pygame, f"K_{chr(c)}") for c in range(ord("a"), ord("z") + 1)}


def _parse_keybinding(spec: str) -> Optional[tuple[int, int]]:
    """Parse 'Ctrl+Shift+F3' -> (mods, key). Returns None on failure."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    for p in parts[:-1]:
        if p in ("ctrl", "control"):
            mods |= pygame.KMOD_CTRL
        elif p == "shift":
            mods |= pygame.KMOD_SHIFT
        elif p in ("alt", "meta"):
            mods |= pygame.KMOD_ALT
        else:
            return None
    last = parts[-1]
    if last in _KEY_MAP_F:
        return mods, _KEY_MAP_F[last]
    if last in _KEY_MAP_DIGITS:
        return mods, _KEY_MAP_DIGITS[last]
    if last in _KEY_MAP_LETTERS:
        return mods, _KEY_MAP_LETTERS[last]
    if last == "space":
        return mods, pygame.K_SPACE
    if last == "enter" or last == "return":
        return mods, pygame.K_RETURN
    return None


_RELEVANT_MODS = pygame.KMOD_CTRL | pygame.KMOD_SHIFT | pygame.KMOD_ALT


def load_soundboard(path: Optional[str]) -> list[SoundboardEntry]:
    """Load entries from a JSON file. Returns [] if file missing/empty."""
    if not path:
        return []
    p = os.path.expanduser(path)
    if not os.path.isfile(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[soundboard] failed to load {p}: {e}", file=sys.stderr)
        return []
    raw_entries = data.get("entries") if isinstance(data, dict) else data
    if not isinstance(raw_entries, list):
        return []
    out: list[SoundboardEntry] = []
    seen_bindings: set[tuple[int, int]] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        key_str = str(raw.get("key", "")).strip()
        phrase = str(raw.get("phrase", "")).strip()
        label = str(raw.get("label", "")).strip() or phrase[:40]
        if not key_str or not phrase:
            continue
        parsed = _parse_keybinding(key_str)
        if parsed is None:
            print(f"[soundboard] cannot parse key {key_str!r}", file=sys.stderr)
            continue
        mods, key = parsed
        if (mods, key) in seen_bindings:
            print(f"[soundboard] duplicate binding {key_str!r}, skipping",
                  file=sys.stderr)
            continue
        seen_bindings.add((mods, key))
        out.append(SoundboardEntry(key_str=key_str, label=label,
                                    phrase=phrase, mods=mods, key=key))
    return out


def soundboard_lookup(entries: list[SoundboardEntry], key: int,
                      mods: int) -> Optional[SoundboardEntry]:
    norm = 0
    if mods & pygame.KMOD_CTRL:
        norm |= pygame.KMOD_CTRL
    if mods & pygame.KMOD_SHIFT:
        norm |= pygame.KMOD_SHIFT
    if mods & pygame.KMOD_ALT:
        norm |= pygame.KMOD_ALT
    for e in entries:
        if e.key == key and e.mods == norm:
            return e
    return None



# =========================================================================
@dataclass
class Utterance:
    text: str
    start_t: float
    duration: float


class Speaker:
    def __init__(self, fx: bool = True, rate: int = 140,
                 voice_mode: str = "random", chaos: float = 1.0):
        self.fx = fx and HAVE_AUDIO_FX
        self.rate = rate
        self.chaos = max(0.0, min(2.0, chaos))
        self.voice_mode = voice_mode  # 'random' | 'fixed' | name substring
        self._voices: list = []
        self._voice_idx = 0
        self._last_voice_id: Optional[str] = None
        self.q: "queue.Queue[Optional[str]]" = queue.Queue()
        self._lock = threading.Lock()
        self._current: Optional[Utterance] = None
        self._last: Optional[Utterance] = None
        self._stop_evt = threading.Event()
        self._engine = None  # persistent SAPI engine, lazily created on worker thread
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def say(self, text: str):
        self.q.put(text)

    def shutdown(self):
        self._stop_evt.set()
        try:
            sd.stop()
        except Exception:
            pass
        self.q.put(None)

    def clear_last(self):
        with self._lock:
            self._last = None

    def snapshot(self):
        with self._lock:
            return self._current, self._last

    def get_display_text(self) -> str:
        cur, last = self.snapshot()
        if cur is not None:
            elapsed = time.monotonic() - cur.start_t
            if cur.duration <= 0:
                return cur.text
            frac = max(0.0, min(1.0, elapsed / cur.duration))
            n = max(0, int(round(len(cur.text) * frac)))
            return cur.text[:n]
        return last.text if last else ""

    # ---- worker ---------------------------------------------------------
    def _ensure_voices(self):
        """Populate ``self._voices`` once via a throwaway engine.

        We only do this so :py:meth:`_pick_voice` has something to work
        with; the real synthesis runs in a child process to avoid the
        well-known SAPI/COM state corruption that hangs pyttsx3 after a
        few utterances when reused on a single thread.
        """
        if self._voices:
            return
        try:
            eng = pyttsx3.init()
            self._voices = list(eng.getProperty("voices")) or []
            try:
                eng.stop()
            except Exception:
                pass
            del eng
        except Exception:
            self._voices = []

    def _render_wav_subprocess(self, text: str, wav_path: str) -> bool:
        """Render ``text`` to ``wav_path`` in a fresh Python subprocess.

        Each call gets a brand-new interpreter, so SAPI/COM state cannot
        leak across utterances.  Returns True on success.
        """
        self._ensure_voices()
        v = self._pick_voice()
        voice_id = getattr(v, "id", "") if v is not None else ""
        jitter = int(random.uniform(-25, 25) * self.chaos)
        rate = max(60, self.rate + jitter)
        script = (
            "import sys, pyttsx3\n"
            "text, wav, voice_id, rate = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])\n"
            "e = pyttsx3.init()\n"
            "if voice_id:\n"
            "    try: e.setProperty('voice', voice_id)\n"
            "    except Exception: pass\n"
            "e.setProperty('rate', rate)\n"
            "e.setProperty('volume', 1.0)\n"
            "e.save_to_file(text, wav)\n"
            "e.runAndWait()\n"
        )
        try:
            res = subprocess.run(
                [sys.executable, "-c", script, text, wav_path, voice_id, str(rate)],
                capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            print("[speaker] TTS subprocess timed out", file=sys.stderr)
            return False
        except Exception as e:
            print(f"[speaker] TTS subprocess failed to launch: {e}", file=sys.stderr)
            return False
        if res.returncode != 0:
            print(f"[speaker] TTS subprocess exit {res.returncode}: "
                  f"{(res.stderr or '').strip()[:200]}", file=sys.stderr)
            return False
        if voice_id:
            self._last_voice_id = voice_id
        return os.path.exists(wav_path) and os.path.getsize(wav_path) >= 64

    def _pick_voice(self):
        if not self._voices:
            return None
        mode = self.voice_mode
        if mode == "fixed":
            return self._voices[0]
        if mode == "random":
            # Always swap to a DIFFERENT voice than the last one used.
            choices = [v for v in self._voices
                       if getattr(v, "id", None) != self._last_voice_id]
            if not choices:
                choices = list(self._voices)
            return random.choice(choices)
        # treat as substring match against voice name/id
        m = mode.lower()
        for v in self._voices:
            name = (getattr(v, "name", "") or "").lower()
            vid = (getattr(v, "id", "") or "").lower()
            if m in name or m in vid:
                return v
        return self._voices[0]

    def _run(self):
        if pyttsx3 is None:
            print("[speaker] pyttsx3 not installed -- audio disabled",
                  file=sys.stderr)
            while not self._stop_evt.is_set():
                if self.q.get() is None:
                    return
            return

        while not self._stop_evt.is_set():
            item = self.q.get()
            if item is None:
                return
            text = item.strip()
            if not text:
                continue
            try:
                self._speak(text)
            except Exception as e:
                print(f"[speaker] error: {e}", file=sys.stderr)
                with self._lock:
                    self._current = None

    def _speak(self, text: str):
        if not self.fx:
            self._speak_plain(text)
            return
        with tempfile.TemporaryDirectory() as td:
            wav = os.path.join(td, "u.wav")
            if not self._render_wav_subprocess(text, wav):
                self._speak_plain(text)
                return
            data, fs = sf.read(wav, dtype="float32", always_2d=False)
            if data.ndim > 1:
                data = data.mean(axis=1)
            # Per-utterance chaos: roll around the configured base so each
            # prompt has its own intensity. Skewed log-uniform so we get a
            # mix of mild and unhinged readings rather than a flat average.
            base = max(0.05, self.chaos)
            lo = base * 0.25
            hi = base * 1.75
            u_chaos = math.exp(random.uniform(math.log(lo), math.log(hi)))
            u_chaos = max(0.0, min(2.0, u_chaos))
            out, fs = _process_chain(data, fs, chaos=u_chaos)
            duration = len(out) / float(fs)
            # measured startup latency from sd.play() to first audible sample
            # on this machine is ~50ms; offset start_t so the reveal lines up
            # with what's actually coming out of the speakers.
            startup = 0.05
            with self._lock:
                self._current = Utterance(text,
                                          time.monotonic() + startup,
                                          duration)
            sd.play(out, fs)
            sd.wait()
            with self._lock:
                self._last = self._current
                self._current = None

    def _speak_plain(self, text: str):
        # If we have soundfile+sounddevice we still want exact-duration
        # playback so the visual reveal matches.  Render to WAV, read it
        # back, and play through sounddevice -- skipping the FX chain.
        if HAVE_AUDIO_FX:
            with tempfile.TemporaryDirectory() as td:
                wav = os.path.join(td, "u.wav")
                if self._render_wav_subprocess(text, wav):
                    data, fs = sf.read(wav, dtype="float32", always_2d=False)
                    if data.ndim > 1:
                        data = data.mean(axis=1)
                    duration = len(data) / float(fs)
                    startup = 0.05
                    with self._lock:
                        self._current = Utterance(text,
                                                  time.monotonic() + startup,
                                                  duration)
                    sd.play(data, fs)
                    sd.wait()
                    with self._lock:
                        self._last = self._current
                        self._current = None
                    return
        # Fallback: live SAPI playback with a duration estimate.
        # Use a one-shot engine so COM state can't accumulate.
        try:
            eng = pyttsx3.init()
            self._ensure_voices()
            v = self._pick_voice()
            if v is not None:
                try:
                    eng.setProperty("voice", v.id)
                    self._last_voice_id = v.id
                except Exception:
                    pass
            jitter = int(random.uniform(-25, 25) * self.chaos)
            eng.setProperty("rate", max(60, self.rate + jitter))
            eng.setProperty("volume", 1.0)
        except Exception as e:
            print(f"[speaker] live SAPI init failed: {e}", file=sys.stderr)
            return
        words = max(1, len(text.split()))
        est = max(0.6, words / max(60, self.rate) * 60.0)
        with self._lock:
            self._current = Utterance(text, time.monotonic(), est)
        eng.say(text)
        eng.runAndWait()
        try:
            eng.stop()
        except Exception:
            pass
        del eng
        with self._lock:
            self._last = self._current
            self._current = None


# =========================================================================
# Audio FX chain
# =========================================================================
def _process_chain(y: "np.ndarray", fs: int,
                   chaos: float = 1.0) -> tuple["np.ndarray", int]:
    """Run a randomized chain of weird-computer effects.

    Always: phaser (with modulated LFO).
    Sometimes: pitch shift, tremolo, ring-mod, bit-crush, comb/metallic,
               short pitch-warp segment, stutter.

    Per-utterance the per-effect probabilities are themselves jittered, so
    the same `chaos` setting still produces wildly different "personalities"
    from one line to the next.
    """
    out = y.astype(np.float32, copy=True)

    def p(base: float) -> float:
        # jitter each base probability by a random factor in [0.4, 1.8]
        return min(1.0, base * chaos * random.uniform(0.4, 1.8))

    # 1. per-utterance pitch shift via resampling.
    if random.random() < p(0.55):
        semis = random.uniform(-3.5, 3.5)
        out = _pitch_shift_resample(out, semis)

    # 2. tremolo (amplitude LFO)
    if random.random() < p(0.45):
        out = _tremolo(out, fs,
                       rate=random.uniform(3.0, 11.0),
                       depth=random.uniform(0.15, 0.45))

    # 3. modulated phaser (always on, but params vary wildly)
    out = _apply_phaser_modulated(out, fs, chaos=chaos)

    # 4. ring modulator
    if random.random() < p(0.30):
        out = _ring_mod(out, fs,
                        f=random.uniform(40.0, 180.0),
                        depth=random.uniform(0.2, 0.55))

    # 5. bit-crush / sample-and-hold
    if random.random() < p(0.20):
        out = _bit_crush(out,
                         bits=random.choice([5, 6, 7, 8]),
                         hold=random.choice([1, 2, 2, 3]))

    # 6. comb filter -> metallic / cylinder / robot ring
    if random.random() < p(0.30):
        out = _comb(out, fs,
                    delay_ms=random.uniform(2.0, 9.0),
                    feedback=random.uniform(0.4, 0.7),
                    mix=random.uniform(0.25, 0.45))

    # 7. brief mid-utterance pitch warp ("glitch dive")
    if random.random() < p(0.35):
        out = _pitch_warp_segment(out, fs)

    # 8. occasional brief stutter repeat
    if random.random() < p(0.18):
        out = _stutter(out, fs)

    # final normalize
    peak = float(np.max(np.abs(out))) if len(out) else 1.0
    if peak > 0.99:
        out = out * (0.99 / peak)
    return out.astype(np.float32), fs


def _pitch_shift_resample(y: "np.ndarray", semitones: float) -> "np.ndarray":
    if semitones == 0 or len(y) == 0:
        return y
    factor = 2.0 ** (semitones / 12.0)
    new_n = max(1, int(len(y) / factor))
    src_idx = np.linspace(0, len(y) - 1, new_n)
    return np.interp(src_idx, np.arange(len(y)), y).astype(np.float32)


def _tremolo(y: "np.ndarray", fs: int, rate: float, depth: float) -> "np.ndarray":
    n = len(y)
    if n == 0:
        return y
    t = np.arange(n) / fs
    lfo = 1.0 - depth * 0.5 * (1.0 - np.cos(2 * math.pi * rate * t))
    return (y * lfo).astype(np.float32)


def _ring_mod(y: "np.ndarray", fs: int, f: float, depth: float) -> "np.ndarray":
    n = len(y)
    if n == 0:
        return y
    t = np.arange(n) / fs
    car = np.cos(2 * math.pi * f * t)
    return (y * (1.0 - depth + depth * car)).astype(np.float32)


def _bit_crush(y: "np.ndarray", bits: int = 6, hold: int = 1) -> "np.ndarray":
    if len(y) == 0:
        return y
    levels = 2 ** max(2, bits)
    q = np.round(y * (levels / 2)) / (levels / 2)
    if hold > 1:
        q = np.repeat(q[::hold], hold)[: len(y)]
        if len(q) < len(y):
            q = np.concatenate([q, np.zeros(len(y) - len(q), dtype=np.float32)])
    return q.astype(np.float32)


def _comb(y: "np.ndarray", fs: int, delay_ms: float,
          feedback: float, mix: float) -> "np.ndarray":
    n = len(y)
    d = max(1, int(fs * delay_ms / 1000.0))
    if n == 0:
        return y
    out = y.astype(np.float32, copy=True)
    buf = np.zeros(d, dtype=np.float32)
    bi = 0
    fb = float(np.clip(feedback, 0.0, 0.92))
    for i in range(n):
        delayed = buf[bi]
        v = y[i] + fb * delayed
        buf[bi] = v
        bi = (bi + 1) % d
        out[i] = (1.0 - mix) * y[i] + mix * delayed
    return out


def _pitch_warp_segment(y: "np.ndarray", fs: int) -> "np.ndarray":
    n = len(y)
    if n < fs // 2:
        return y
    seg_len = random.randint(int(fs * 0.10), int(fs * 0.35))
    start = random.randint(fs // 8, n - seg_len - 1)
    seg = y[start:start + seg_len]
    # warp the segment by a curved time map (slow down then speed up, or
    # vice-versa) -- creates a brief pitch dive / squeal.
    direction = random.choice((-1.0, 1.0))
    intensity = random.uniform(0.25, 0.55) * direction
    tau = np.linspace(0, 1, seg_len)
    warp = tau + intensity * np.sin(np.pi * tau) * 0.5
    warp = np.clip(warp, 0.0, 1.0)
    src = warp * (seg_len - 1)
    warped = np.interp(src, np.arange(seg_len), seg).astype(np.float32)
    out = y.copy()
    # crossfade ends so the splice isn't clicky
    fade = min(256, seg_len // 6)
    if fade > 4:
        env = np.linspace(0, 1, fade, dtype=np.float32)
        warped[:fade] = warped[:fade] * env + seg[:fade] * (1 - env)
        warped[-fade:] = warped[-fade:] * (1 - env) + seg[-fade:] * env
    out[start:start + seg_len] = warped
    return out


def _stutter(y: "np.ndarray", fs: int) -> "np.ndarray":
    n = len(y)
    if n <= fs // 3:
        return y
    out = y.copy()
    start = random.randint(fs // 8, n - fs // 4)
    chunk_len = random.randint(int(fs * 0.04), int(fs * 0.10))
    chunk = out[start:start + chunk_len].copy()
    reps = random.randint(2, 3)
    tile = np.tile(chunk, reps)
    fade = min(256, chunk_len // 4)
    if fade > 4:
        env = np.linspace(0, 1, fade, dtype=np.float32)
        tile[:fade] = (tile[:fade] * env
                       + out[start:start + fade] * (1 - env))
    end = min(n, start + len(tile))
    out[start:end] = tile[:end - start]
    return out


# Phaser with LFO whose rate AND depth themselves wobble over time
def _apply_phaser_modulated(y: "np.ndarray", fs: int,
                            chaos: float = 1.0) -> "np.ndarray":
    n = len(y)
    if n == 0:
        return y

    stages = random.choice((4, 4, 6, 8))
    mix = random.uniform(0.45, 0.7)
    feedback = random.uniform(0.2, 0.55)
    fmin = random.uniform(180.0, 350.0)
    fmax = random.uniform(1100.0, 2200.0)

    base_rate = random.uniform(0.30, 1.4)
    # The phaser's LFO rate itself wobbles via a slow second LFO.
    mod_rate = random.uniform(0.05, 0.35)
    mod_depth = random.uniform(0.4, 1.6) * chaos

    t = np.arange(n) / fs
    rate_t = base_rate * (1.0 + mod_depth * np.sin(2 * math.pi * mod_rate * t))
    rate_t = np.clip(rate_t, 0.05, 6.0)
    # integrate to get phase
    phase = 2 * math.pi * np.cumsum(rate_t) / fs + random.uniform(0, 2 * math.pi)
    lfo = 0.5 * (1.0 + np.sin(phase))
    f = fmin * (fmax / fmin) ** lfo
    tan_term = np.tan(np.pi * f / fs)
    a_arr = ((tan_term - 1.0) / (tan_term + 1.0)).astype(np.float32)

    wet = y.astype(np.float32, copy=True)
    fb = 0.0
    for stage in range(stages):
        out = np.empty_like(wet)
        prev_x = 0.0
        prev_y = 0.0
        if stage == 0:
            for i in range(n):
                xi = wet[i] + feedback * fb
                yi = a_arr[i] * xi + prev_x - a_arr[i] * prev_y
                out[i] = yi
                prev_x = xi
                prev_y = yi
        else:
            for i in range(n):
                xi = wet[i]
                yi = a_arr[i] * xi + prev_x - a_arr[i] * prev_y
                out[i] = yi
                prev_x = xi
                prev_y = yi
        wet = out
        fb = float(wet[-1])

    return ((1.0 - mix) * y + mix * wet).astype(np.float32)


# Phaser: 4 cascaded all-pass filters with LFO-swept break frequency
# =========================================================================
def _apply_phaser(y: "np.ndarray", fs: int,
                  stages: int = 4, mix: float = 0.55,
                  fmin: float = 250.0, fmax: float = 1600.0,
                  rate_hz: Optional[float] = None,
                  feedback: float = 0.30) -> "np.ndarray":
    n = len(y)
    if n == 0:
        return y
    if rate_hz is None:
        rate_hz = random.uniform(0.35, 0.9)

    t = np.arange(n) / fs
    phase0 = random.uniform(0, 2 * math.pi)
    lfo = 0.5 * (1.0 + np.sin(2 * math.pi * rate_hz * t + phase0))
    f = fmin * (fmax / fmin) ** lfo
    tan_term = np.tan(np.pi * f / fs)
    a = ((tan_term - 1.0) / (tan_term + 1.0)).astype(np.float32)

    wet = y.astype(np.float32, copy=True)
    fb = 0.0
    a_arr = a

    # Per-sample, per-stage recursion (sequential by nature). N is small
    # enough (a few seconds at 22 kHz) that this finishes in well under a
    # second in pure Python.
    for stage in range(stages):
        out = np.empty_like(wet)
        prev_x = 0.0
        prev_y = 0.0
        w_in = wet
        if stage == 0:
            for i in range(n):
                xi = w_in[i] + feedback * fb
                yi = a_arr[i] * xi + prev_x - a_arr[i] * prev_y
                out[i] = yi
                prev_x = xi
                prev_y = yi
        else:
            for i in range(n):
                xi = w_in[i]
                yi = a_arr[i] * xi + prev_x - a_arr[i] * prev_y
                out[i] = yi
                prev_x = xi
                prev_y = yi
        wet = out
        fb = float(wet[-1])

    mixed = ((1.0 - mix) * y + mix * wet).astype(np.float32)

    # Rare brief stutter
    if random.random() < 0.18 and n > fs // 3:
        start = random.randint(fs // 8, n - fs // 4)
        chunk_len = random.randint(int(fs * 0.04), int(fs * 0.10))
        chunk = mixed[start:start + chunk_len].copy()
        reps = random.randint(2, 3)
        tile = np.tile(chunk, reps)
        fade = min(256, chunk_len // 4)
        if fade > 4:
            env = np.linspace(0, 1, fade, dtype=np.float32)
            tile[:fade] = (tile[:fade] * env
                           + mixed[start:start + fade] * (1 - env))
        end = min(n, start + len(tile))
        mixed[start:end] = tile[:end - start]

    peak = float(np.max(np.abs(mixed))) if len(mixed) else 1.0
    if peak > 0.99:
        mixed = mixed * (0.99 / peak)
    return mixed.astype(np.float32)


# =========================================================================
# Rendering
# =========================================================================
def wrap_lines(font: pygame.font.Font, text: str, max_w: int) -> list[str]:
    words = text.split(" ")
    lines, cur = [], ""
    for w in words:
        trial = w if not cur else cur + " " + w
        if font.size(trial)[0] <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_centered_4x3(surface: pygame.Surface, full_text: str,
                        revealed_n: int, font_name: str,
                        margin_frac: float = 0.08,
                        force_upper: bool = True):
    """Render `full_text[:revealed_n]` centered inside a 4:3 viewport.

    Layout (font size, line wrap) is sized for `full_text` so revealing more
    characters does NOT cause the text to reflow.
    """
    sw, sh = surface.get_size()
    if sw <= 0 or sh <= 0:
        return
    if sw / sh > 4 / 3:
        vh = sh
        vw = int(sh * 4 / 3)
    else:
        vw = sw
        vh = int(sw * 3 / 4)
    vx = (sw - vw) // 2
    vy = (sh - vh) // 2

    if not full_text:
        return
    if force_upper:
        full_text = full_text.upper()

    max_w = int(vw * (1 - 2 * margin_frac))
    max_h = int(vh * (1 - 2 * margin_frac))

    # Scale max font size to viewport so it fills both small and huge windows.
    start = max(48, min(int(vh * 0.45), 360))
    size = start
    font = None
    full_lines: list[str] = []
    line_h = 0
    while size >= 24:
        font = pygame.font.SysFont(font_name, size, bold=False)
        full_lines = wrap_lines(font, full_text, max_w)
        line_h = font.get_linesize()
        block_h = line_h * len(full_lines)
        widest = max(font.size(l)[0] for l in full_lines)
        if block_h <= max_h and widest <= max_w:
            break
        size = max(24, int(size * 0.9))

    assert font is not None
    block_h = line_h * len(full_lines)
    y0 = vy + (vh - block_h) // 2

    consumed = 0
    revealed_n = max(0, min(revealed_n, len(full_text)))
    for i, line in enumerate(full_lines):
        if i > 0:
            consumed += 1  # the space that wrapped here
        if consumed >= revealed_n:
            break
        avail = revealed_n - consumed
        shown = line if avail >= len(line) else line[:avail]
        consumed += len(line)
        if not shown:
            continue
        surf = font.render(shown, True, (255, 255, 255))
        full_w = font.size(line)[0]
        left_x = vx + (vw - full_w) // 2
        surface.blit(surf, (left_x, y0 + i * line_h))


def draw_control(surface: pygame.Surface, typed: str, cursor_on: bool,
                 monitor_label: str, fonts: dict,
                 soundboard: Optional[list] = None):
    sw, sh = surface.get_size()
    surface.fill((10, 10, 14))

    title = fonts["title"].render("FRIEND COMPUTER :: INPUT TERMINAL",
                                  True, (180, 220, 180))
    surface.blit(title, (20, 16))

    sub = fonts["small"].render(
        "Type a sentence. End with . ! ? ; :  (or press Enter) to transmit.",
        True, (130, 170, 130))
    surface.blit(sub, (20, 16 + title.get_height() + 4))

    # leave room at the bottom for hint lines + soundboard panel
    hints = [
        "ESC  quit               F11  toggle display fullscreen",
        "Backspace  erase char   Ctrl+U  clear input   Ctrl+L  clear display",
        f"display: {monitor_label}",
    ]
    sf = fonts["small"]
    hints_h = len(hints) * sf.get_linesize()

    sb_entries = soundboard or []
    sb_h = 0
    if sb_entries:
        # two-column grid
        col_count = 2
        rows = (len(sb_entries) + col_count - 1) // col_count
        sb_line_h = sf.get_linesize() + 2
        sb_header_h = sf.get_linesize() + 4
        sb_h = sb_header_h + rows * sb_line_h + 8

    box_y = 110
    box_h = sh - box_y - 20 - hints_h - sb_h
    if box_h < 60:
        box_h = 60
    pygame.draw.rect(surface, (0, 60, 0), (16, box_y, sw - 32, box_h), 1)

    cursor = "_" if cursor_on else " "
    prompt = f"> {typed}{cursor}"
    f = fonts["input"]
    lines = wrap_lines(f, prompt, sw - 48)
    for i, line in enumerate(lines):
        s = f.render(line, True, (60, 240, 60))
        surface.blit(s, (24, box_y + 12 + i * f.get_linesize()))

    # soundboard panel
    if sb_entries:
        sb_y = box_y + box_h + 8
        hdr = sf.render("-- SOUNDBOARD --", True, (140, 180, 140))
        surface.blit(hdr, (20, sb_y))
        sb_y += sb_header_h
        col_w = (sw - 32) // 2
        for idx, e in enumerate(sb_entries):
            row = idx // 2
            col = idx % 2
            x = 20 + col * col_w
            y = sb_y + row * sb_line_h
            # truncate label
            label = e.label
            text = f"{e.key_str:<10s} {label}"
            # crop to col width
            while sf.size(text)[0] > col_w - 12 and len(text) > 12:
                text = text[:-2] + "…"
                if text.endswith("……"):
                    text = text[:-1]
            s = sf.render(text, True, (170, 200, 170))
            surface.blit(s, (x, y))

    # bottom hint lines
    y = sh - 16 - hints_h
    for h in hints:
        s = sf.render(h, True, (90, 110, 90))
        surface.blit(s, (20, y))
        y += sf.get_linesize()


# =========================================================================
# Window setup
# =========================================================================
def make_display_window(monitor: Optional[Monitor], windowed: bool,
                        size: tuple[int, int] = (960, 720)):
    if windowed or monitor is None:
        kwargs = {"resizable": True}
        if monitor is not None:
            kwargs["position"] = (monitor.x + 40, monitor.y + 40)
        win = pygame.Window("FRIEND COMPUTER :: DISPLAY", size, **kwargs)
    else:
        size = (monitor.w, monitor.h)
        win = pygame.Window("FRIEND COMPUTER :: DISPLAY", size,
                            position=(monitor.x, monitor.y),
                            borderless=True)
    win.get_surface()
    return win


def set_display_fullscreen(win: pygame.Window, monitor: Optional[Monitor],
                           on: bool, windowed_size: tuple[int, int]):
    """Toggle the display window between borderless-fullscreen on its
    monitor and a normal resizable window."""
    if on and monitor is not None:
        win.borderless = True
        win.resizable = False
        win.position = (monitor.x, monitor.y)
        win.size = (monitor.w, monitor.h)
    else:
        win.borderless = False
        win.resizable = True
        if monitor is not None:
            win.position = (monitor.x + 40, monitor.y + 40)
        win.size = windowed_size


def make_control_window(primary: Optional[Monitor],
                        display_monitor: Optional[Monitor],
                        size: tuple[int, int] = (820, 720)):
    kwargs = {"resizable": True}
    target = primary
    if (display_monitor is not None and primary is not None
            and primary.index == display_monitor.index):
        target = None
    if target is not None:
        kwargs["position"] = (target.x + 60, target.y + 80)
    win = pygame.Window("FRIEND COMPUTER :: CONTROL", size, **kwargs)
    win.get_surface()
    return win


def _parse_size(s: str) -> tuple[int, int]:
    try:
        w, h = s.lower().replace(" ", "").split("x")
        return (max(160, int(w)), max(120, int(h)))
    except Exception as e:
        raise argparse.ArgumentTypeError(
            f"size must be WxH, e.g. 1280x960 (got {s!r})") from e


# =========================================================================
# Main
# =========================================================================
def parse_args():
    p = argparse.ArgumentParser(description="PARANOIA insane-computer terminal")
    p.add_argument("--monitor", type=int, default=None,
                   help="Monitor index for the DISPLAY window.")
    p.add_argument("--windowed", action="store_true",
                   help="Single-monitor testing mode: run display in a 4:3 "
                        "window. IGNORED if a secondary monitor is detected "
                        "(the display always auto-fullscreens there).")
    p.add_argument("--list-monitors", action="store_true",
                   help="Print detected monitors and exit.")
    p.add_argument("--no-fx", action="store_true",
                   help="Disable phaser / stutter audio effects.")
    p.add_argument("--no-glitch", action="store_true",
                   help="Disable subliminal text glitching.")
    p.add_argument("--glitch", type=float, default=1.0,
                   help="Glitch intensity multiplier (default 1.0).")
    p.add_argument("--font", default="arial,helvetica,segoeui",
                   help="DISPLAY font family (comma-separated fallbacks). "
                        "Default: 'arial,helvetica,segoeui'.")
    p.add_argument("--control-font", default="consolas,couriernew,monospace",
                   help="CONTROL window font (default: consolas).")
    p.add_argument("--display-size", type=_parse_size, default=(960, 720),
                   metavar="WxH",
                   help="Initial DISPLAY window size when not fullscreen "
                        "(default 960x720).")
    p.add_argument("--control-size", type=_parse_size, default=(820, 720),
                   metavar="WxH",
                   help="Initial CONTROL window size (default 820x720).")
    p.add_argument("--fullscreen", action="store_true",
                   help="Start the DISPLAY in fullscreen on its monitor. "
                        "Equivalent to NOT passing --windowed.")
    p.add_argument("--rate", type=int, default=140,
                   help="Base TTS words-per-minute (default 140 -- a little "
                        "slower than natural conversation, but not glacial).")
    p.add_argument("--mute", action="store_true",
                   help="Disable TTS entirely (visual only).")
    p.add_argument("--voice", default="random",
                   help="SAPI voice selection: 'random' (default, picks a "
                        "different installed voice per utterance), 'fixed' "
                        "(use the system default), or a substring matched "
                        "against installed voice names (e.g. 'zira', 'david').")
    p.add_argument("--list-voices", action="store_true",
                   help="Print installed SAPI voices and exit.")
    p.add_argument("--chaos", type=float, default=1.0,
                   help="Audio-chaos multiplier 0..2 (default 1.0). Scales "
                        "the probability and depth of all weird FX.")
    p.add_argument("--soundboard", default="soundboard.json",
                   help="Path to a JSON soundboard file with hotkey-bound "
                        "phrases (default: soundboard.json next to the "
                        "script). Pass empty string to disable.")
    return p.parse_args()


def main():
    args = parse_args()
    mons = list_monitors()

    if args.list_monitors:
        if not mons:
            print("screeninfo not installed or no monitors detected.")
        for m in mons:
            print(m)
        return 0

    if args.list_voices:
        if pyttsx3 is None:
            print("pyttsx3 not installed.")
            return 0
        eng = pyttsx3.init()
        for v in eng.getProperty("voices"):
            print(f"- {getattr(v, 'name', '?')}  [{getattr(v, 'id', '?')}]")
        return 0

    pygame.init()
    pygame.font.init()

    display_monitor = pick_monitor(args, mons)
    primary_monitor = mons[0] if mons else None

    # Auto-fullscreen on a secondary monitor if one exists -- this is the
    # whole point of the rig. --windowed only takes effect when there is
    # no secondary display (i.e. when you're testing on the laptop alone).
    has_secondary = (
        len(mons) > 1
        and display_monitor is not None
        and primary_monitor is not None
        and display_monitor.index != primary_monitor.index
    )
    if has_secondary:
        start_windowed = False
        if args.windowed and not args.fullscreen:
            print(f"[info] secondary monitor detected ({display_monitor}); "
                  "ignoring --windowed and going fullscreen there.",
                  file=sys.stderr)
    else:
        start_windowed = args.windowed and not args.fullscreen

    display_win = make_display_window(display_monitor, start_windowed,
                                      size=args.display_size)
    control_win = make_control_window(primary_monitor, display_monitor,
                                      size=args.control_size)
    display_fullscreen = not start_windowed and display_monitor is not None

    try:
        control_win.focus()
    except Exception:
        pass

    speaker = (None if args.mute
               else Speaker(fx=not args.no_fx, rate=args.rate,
                            voice_mode=args.voice, chaos=args.chaos))

    # Resolve soundboard path: if user passed the bare default name and it
    # doesn't exist in cwd, look next to the script too.
    sb_path = args.soundboard.strip() if args.soundboard else ""
    if sb_path and not os.path.isabs(sb_path) and not os.path.isfile(sb_path):
        candidate = _resource_path(sb_path)
        if os.path.isfile(candidate):
            sb_path = candidate
    soundboard = load_soundboard(sb_path) if sb_path else []
    if soundboard:
        print(f"[soundboard] loaded {len(soundboard)} entries from {sb_path}",
              file=sys.stderr)

    fonts = {
        "title": pygame.font.SysFont(args.control_font, 22, bold=True),
        "small": pygame.font.SysFont(args.control_font, 16),
        "input": pygame.font.SysFont(args.control_font, 26),
    }

    clock = pygame.time.Clock()
    typed = ""
    cursor_on = True
    last_blink = time.time()
    glitch_intensity = 0.0 if args.no_glitch else args.glitch
    monitor_label = str(display_monitor) if display_monitor else "(unspecified)"

    # Mute-mode fallback so display still reveals timed text.
    fallback: Optional[Utterance] = None

    def commit(sentence: str):
        nonlocal fallback
        sentence = sentence.strip()
        if not sentence:
            return
        glitched = subliminal_glitch(sentence, glitch_intensity)
        if speaker:
            speaker.say(glitched)
        else:
            words = max(1, len(glitched.split()))
            est = max(1.5, words / max(60, args.rate) * 60.0)
            fallback = Utterance(glitched, time.monotonic(), est)

    running = True
    while running:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT or ev.type == pygame.WINDOWCLOSE:
                running = False
            elif ev.type == pygame.KEYDOWN:
                mods = pygame.key.get_mods()
                # Soundboard hotkeys take priority, but we still let the
                # built-in handlers run for keys without a soundboard match.
                sb_hit = soundboard_lookup(soundboard, ev.key, mods)
                # Don't shadow the explicitly reserved built-ins.
                reserved = (
                    ev.key in (pygame.K_ESCAPE, pygame.K_F11,
                               pygame.K_BACKSPACE, pygame.K_RETURN)
                    or (ev.key in (pygame.K_u, pygame.K_l)
                        and (mods & pygame.KMOD_CTRL))
                )
                if sb_hit and not reserved:
                    commit(sb_hit.phrase)
                    continue
                if ev.key == pygame.K_ESCAPE:
                    running = False
                elif ev.key == pygame.K_F11:
                    display_fullscreen = not display_fullscreen
                    set_display_fullscreen(display_win, display_monitor,
                                           display_fullscreen,
                                           args.display_size)
                elif ev.key == pygame.K_BACKSPACE:
                    typed = typed[:-1]
                elif ev.key == pygame.K_u and (mods & pygame.KMOD_CTRL):
                    typed = ""
                elif ev.key == pygame.K_l and (mods & pygame.KMOD_CTRL):
                    if speaker:
                        speaker.clear_last()
                    fallback = None
                elif ev.key == pygame.K_RETURN:
                    commit(typed)
                    typed = ""
                else:
                    ch = ev.unicode
                    if ch and ch.isprintable():
                        typed += ch
                        if ch in TERMINATORS:
                            commit(typed)
                            typed = ""

        # ---- DISPLAY ----------------------------------------------------
        disp_surf = display_win.get_surface()
        disp_surf.fill((0, 0, 0))

        full_text = ""
        revealed_n = 0
        if speaker:
            cur, last = speaker.snapshot()
            if cur is not None:
                full_text = cur.text
                if cur.duration > 0:
                    frac = (time.monotonic() - cur.start_t) / cur.duration
                    frac = max(0.0, min(1.0, frac))
                else:
                    frac = 1.0
                revealed_n = int(round(len(full_text) * frac))
            elif last is not None:
                full_text = last.text
                revealed_n = len(full_text)
        elif fallback is not None:
            full_text = fallback.text
            frac = (time.monotonic() - fallback.start_t) / max(0.01, fallback.duration)
            revealed_n = int(round(len(full_text) * max(0.0, min(1.0, frac))))
            # keep displayed after estimate elapses
            if frac >= 1.0:
                revealed_n = len(full_text)

        if full_text:
            render_centered_4x3(disp_surf, full_text, revealed_n, args.font)
        display_win.flip()

        # ---- CONTROL ----------------------------------------------------
        if time.time() - last_blink > 0.5:
            cursor_on = not cursor_on
            last_blink = time.time()
        ctrl_surf = control_win.get_surface()
        draw_control(ctrl_surf, typed, cursor_on, monitor_label, fonts,
                     soundboard=soundboard)
        control_win.flip()

        clock.tick(60)

    if speaker:
        speaker.shutdown()
    pygame.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
