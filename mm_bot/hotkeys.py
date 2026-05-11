"""Terminal hotkey listener.

Runs in a background daemon thread. Puts the terminal in raw (character-at-a-time)
mode so keypresses are registered immediately — no need to press Enter.

Hotkeys
-------
  c   Cancel all open orders (bot keeps running and requotes normally)
  b   Cancel the bid + market-close any long  ("flatten buy side")
  s   Cancel the ask + market-close any short ("flatten sell side")
  f   Flatten entire position (market-close both sides)
  p   Pause quoting for 60 s  (cancels orders, holds off, then resumes)
  r   Resume immediately after a pause
  h   Print this help again
  q   Quit the bot gracefully (same as Ctrl-C)

The listener is silently disabled when stdin is not a real TTY (e.g. when
piped or run in a non-interactive shell) so it never breaks automated setups.

On Windows, raw mode uses ``msvcrt`` instead of ``termios`` / ``tty`` (same
hotkeys when running in a console).
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Callable, Dict

log = logging.getLogger("hotkeys")

_IS_WINDOWS = sys.platform == "win32"

if not _IS_WINDOWS:
    import termios
    import tty
else:
    import msvcrt  # type: ignore[import-not-found]

HOTKEY_HELP = """
┌──────────────────────────────────────────────────────┐
│              hl-mm terminal hotkeys                  │
│                                                      │
│  c  → cancel all orders        (bot keeps running)  │
│  b  → cancel bid + close long  (flatten buy side)   │
│  s  → cancel ask + close short (flatten sell side)  │
│  f  → flatten entire position  (USE WITH CARE)       │
│  p  → pause quoting for 60 s                        │
│  r  → resume immediately                            │
│  h  → show this help                                │
│  q  → quit gracefully          (same as Ctrl-C)     │
└──────────────────────────────────────────────────────┘
"""


class HotkeyListener(threading.Thread):
    def __init__(
        self,
        on_cancel: Callable[[], None],
        on_flatten_buy: Callable[[], None],
        on_flatten_sell: Callable[[], None],
        on_flatten: Callable[[], None],
        on_pause: Callable[[], None],
        on_resume: Callable[[], None],
        on_quit: Callable[[], None],
    ):
        super().__init__(name="hotkeys", daemon=True)
        self._handlers: Dict[str, Callable[[], None]] = {
            "c": on_cancel,
            "b": on_flatten_buy,
            "s": on_flatten_sell,
            "f": on_flatten,
            "p": on_pause,
            "r": on_resume,
            "q": on_quit,
            "h": self._print_help,
        }
        self._stop = threading.Event()

    def run(self) -> None:
        if not sys.stdin.isatty():
            log.debug("stdin is not a TTY — hotkey listener disabled")
            return

        log.info(HOTKEY_HELP)
        if _IS_WINDOWS:
            self._run_windows_console()
        else:
            self._run_unix_tty()

    def _run_unix_tty(self) -> None:
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while not self._stop.is_set():
                ch = sys.stdin.read(1)
                if not ch:
                    break
                # Ctrl-C (^C = \x03) and Ctrl-D (^D = \x04) still quit
                if ch in ("\x03", "\x04"):
                    log.info("Ctrl-C/D pressed → quitting")
                    self._safe_call("q")
                    break
                key = ch.lower()
                if key in self._handlers:
                    self._safe_call(key)
        except Exception as exc:
            log.debug("hotkey listener exiting: %s", exc)
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except Exception:
                pass

    def _run_windows_console(self) -> None:
        """Character-at-a-time hotkeys via msvcrt (no termios on Windows)."""
        try:
            while not self._stop.is_set():
                if msvcrt.kbhit():
                    code = msvcrt.getch()
                    if not code:
                        continue
                    if isinstance(code, bytes):
                        ch = code.decode("latin-1", errors="replace")
                    else:
                        ch = str(code)
                    # Ctrl-C / Ctrl-Break → quit (same intent as Unix branch)
                    if ch in ("\x03", "\x04"):
                        log.info("Ctrl-C/D pressed → quitting")
                        self._safe_call("q")
                        break
                    # Arrow / function keys: first byte is NUL or special
                    if ch == "\x00" or ch == "\xe0":
                        if msvcrt.kbhit():
                            msvcrt.getch()  # swallow extension byte
                        continue
                    key = ch.lower()
                    if key in self._handlers:
                        self._safe_call(key)
                else:
                    time.sleep(0.05)
        except Exception as exc:
            log.debug("hotkey listener exiting: %s", exc)

    def stop(self) -> None:
        self._stop.set()

    def _safe_call(self, key: str) -> None:
        try:
            self._handlers[key]()
        except Exception as exc:
            log.warning("hotkey '%s' handler error: %s", key, exc)

    def _print_help(self) -> None:
        sys.stdout.write("\r\n" + HOTKEY_HELP.replace("\n", "\r\n") + "\r\n")
        sys.stdout.flush()
