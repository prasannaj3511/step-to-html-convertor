"""Console logging with elapsed timing and phase timers."""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager

_T0 = time.time()
_QUIET = False

# Extra destinations for log lines, so a GUI or a test can watch the pipeline
# without the pipeline knowing anything about them.
_SINKS: list = []


def set_quiet(quiet: bool) -> None:
    global _QUIET
    _QUIET = quiet


def add_sink(fn) -> None:
    _SINKS.append(fn)


def remove_sink(fn) -> None:
    if fn in _SINKS:
        _SINKS.remove(fn)


def _to_sinks(line: str) -> None:
    for fn in list(_SINKS):
        try:
            fn(line)
        except Exception:
            pass  # a broken sink must never take the conversion down


def _stamp() -> str:
    el = time.time() - _T0
    return f"[{int(el // 60):02d}:{el % 60:05.2f}]"


def log(msg: str = "") -> None:
    _to_sinks(f"{_stamp()} {msg}")
    if _QUIET:
        return
    sys.stdout.write(f"{_stamp()} {msg}\n")
    sys.stdout.flush()


def warn(msg: str) -> None:
    _to_sinks(f"{_stamp()} WARN  {msg}")
    sys.stdout.write(f"{_stamp()} WARN  {msg}\n")
    sys.stdout.flush()


def error(msg: str) -> None:
    _to_sinks(f"{_stamp()} ERROR {msg}")
    sys.stderr.write(f"{_stamp()} ERROR {msg}\n")
    sys.stderr.flush()


def progress(done: int, total: int, label: str, every: int = 25) -> None:
    """Emit a progress line every `every` items (and on the final item)."""
    if done % every and done != total:
        return
    pct = 100.0 * done / total if total else 100.0
    if _SINKS:
        _to_sinks(f"{_stamp()} {pct:5.1f}%  {done}/{total} {label}")
    if _QUIET:
        return
    bar_w = 28
    filled = int(bar_w * pct / 100.0)
    bar = "#" * filled + "-" * (bar_w - filled)
    sys.stdout.write(f"\r{_stamp()} [{bar}] {pct:5.1f}%  {done}/{total} {label}   ")
    if done == total:
        sys.stdout.write("\n")
    sys.stdout.flush()


@contextmanager
def phase(name: str):
    log(f"== {name} ...")
    t = time.time()
    try:
        yield
    finally:
        log(f"== {name} done in {time.time() - t:.1f}s")


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} PB"


def human_int(n: int) -> str:
    return f"{n:,}"
