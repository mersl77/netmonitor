"""Rotující lokální log (žádná telemetrie)."""
from __future__ import annotations

import logging
import logging.handlers
import sys
from datetime import datetime

from .paths import log_dir


def setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)-14s %(name)s: %(message)s")
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_dir() / "netmonitor.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    _enable_crash_log()
    if sys.stderr is not None:  # ve --windowed buildu je stderr None
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.setLevel(level)
        root.addHandler(sh)


_crash_file = None


def write_crash_traceback(traceback_text: str) -> None:
    """Zapíše nezachycenou výjimku main i ve windowed buildu bez stderr."""
    try:
        if _crash_file is None:
            log_dir().mkdir(parents=True, exist_ok=True)
            _enable_crash_log()
        if _crash_file is not None:
            _crash_file.write(f"\n{datetime.now().astimezone().isoformat()} – Nezachycená výjimka main\n{traceback_text}")
            _crash_file.flush()
    except (OSError, RuntimeError, ValueError):
        logging.getLogger(__name__).warning("Traceback se nepodařilo uložit do crash.log", exc_info=True)


def _enable_crash_log() -> None:
    """Při tvrdém pádu (např. chyba v nativní knihovně) zapíše výpis všech vláken do logs/crash.log."""
    global _crash_file
    import faulthandler
    try:
        if _crash_file is None:
            _crash_file = open(log_dir() / "crash.log", "a", encoding="utf-8")  # noqa: SIM115 – musí zůstat otevřený
        faulthandler.enable(file=_crash_file, all_threads=True)
    except (OSError, RuntimeError, ValueError):
        pass
