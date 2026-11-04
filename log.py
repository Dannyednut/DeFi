"""
log.py — Central logging configuration for DeFi Research Tool.

Single source of truth for ALL output. Every module uses:

    from log import get_logger
    log = get_logger("module_name")

    log.info("message")
    log.debug("verbose detail")
    log.warning("something wrong but recoverable")
    log.error("failure")
    log.research("special research event")   # custom level, always shown

Log levels (set LOG_LEVEL in .env):
  DEBUG   — verbose: reserve math, every cycle, every decoded tx
  INFO    — normal: startup, new pools, state changes, opportunities   [DEFAULT]
  WARNING — quiet:  errors, unexpected conditions only
  ERROR   — silent: only crashes

Console output is colorized by level.
File output (./logs/app.log) is plain text, rotating at 10MB.
"""
from __future__ import annotations

import io
import logging
import logging.handlers
import os
import sys
from pathlib import Path

# ─── Custom RESEARCH level (between INFO=20 and WARNING=30) ──────────────────
RESEARCH_LEVEL = 25
logging.addLevelName(RESEARCH_LEVEL, "RESEARCH")


def _research(self, msg, *args, **kwargs):
    if self.isEnabledFor(RESEARCH_LEVEL):
        self._log(RESEARCH_LEVEL, msg, args, **kwargs)


logging.Logger.research = _research  # type: ignore[attr-defined]


# ─── ANSI colour codes (no external deps) ────────────────────────────────────
_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"

_LEVEL_COLORS = {
    "DEBUG":    "\033[36m",      # cyan
    "INFO":     "\033[32m",      # green
    "RESEARCH": "\033[35m",      # magenta
    "WARNING":  "\033[33m",      # yellow
    "ERROR":    "\033[31m",      # red
    "CRITICAL": "\033[41;97m",   # red background
}

# Fixed-width tag padding — keeps columns aligned
_TAG_WIDTH = 12


class _ColorFormatter(logging.Formatter):
    """Colorized, timestamped console formatter."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        level  = record.levelname
        color  = _LEVEL_COLORS.get(level, "")
        tag    = record.name.split(".")[-1].upper()[:_TAG_WIDTH].ljust(_TAG_WIDTH)
        ts     = self.formatTime(record, "%H:%M:%S")

        # dim timestamp | colored [TAG] | message
        line = (
            f"{_DIM}{ts}{_RESET} "
            f"{color}{_BOLD}[{tag.strip()}]{_RESET} "
            f"{record.getMessage()}"
        )

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)

        return line


class _PlainFormatter(logging.Formatter):
    """Plain timestamped formatter for file output."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        ts  = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        tag = record.name.split(".")[-1].upper()[:_TAG_WIDTH].ljust(_TAG_WIDTH)
        msg = record.getMessage()
        line = f"{ts} {record.levelname:<8} [{tag.strip()}] {msg}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


# ─── One-time setup ──────────────────────────────────────────────────────────

_configured = False


def setup_logging(log_dir: str = "./logs") -> None:
    """
    Call ONCE at startup (from main.py).
    All subsequent get_logger() calls inherit this configuration.
    """
    global _configured
    if _configured:
        return
    _configured = True

    # Force UTF-8 on Windows terminals (cp1252 chokes on arrows/emoji)
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)

    # Resolve log level from env (default INFO)
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    # Also allow RESEARCH as a string level
    if level_name == "RESEARCH":
        level = RESEARCH_LEVEL

    # ── Root logger ──────────────────────────────────────────────────────────
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # root catches everything; handlers filter

    # ── Console handler ──────────────────────────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(_ColorFormatter())
    root.addHandler(console)

    # ── Rotating file handler ─────────────────────────────────────────────────
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    file_path = Path(log_dir) / "app.log"
    fh = logging.handlers.RotatingFileHandler(
        file_path,
        maxBytes=10 * 1024 * 1024,   # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)       # always write DEBUG+ to file
    fh.setFormatter(_PlainFormatter())
    root.addHandler(fh)

    # ── Suppress noisy third-party loggers ───────────────────────────────────
    for noisy in ("websockets", "web3", "urllib3", "asyncio", "eth_abi"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    root.info(f"Logging initialised | console={level_name} | file=DEBUG -> {file_path}")


def get_logger(name: str) -> logging.Logger:
    """
    Get a named logger. Always uses the hierarchy:
      defi.<name>  (e.g. defi.main, defi.mempool.pipeline)

    Usage:
        log = get_logger("pipeline")
        log.info("started")
        log.research("new arb found: ...")
        log.debug("reserve_in=%d", r0)
    """
    # Prefix with "defi." so we can control the whole app in one shot
    full_name = f"defi.{name}" if not name.startswith("defi.") else name
    return logging.getLogger(full_name)
