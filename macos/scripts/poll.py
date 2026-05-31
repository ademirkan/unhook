#!/usr/bin/env python3
"""
Focus Button — Mac polling client.

Polls the Cloudflare backend and reconciles Cold Turkey blocking state.

Future: this will support WebSocket transport in addition to polling.
The reconcile() function is transport-agnostic and will be reused.
"""

import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = SCRIPT_DIR / "config.json"
LOG_PATH = SCRIPT_DIR / "poll.log"
SHIFT_SCRIPT = SCRIPT_DIR / "shift.py"


# ===== Setup =====

def setup_logging() -> None:
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=3
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(handler)
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(stdout)


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ===== Backend communication (transport-specific) =====

import requests

def fetch_state(url: str, secret: str) -> Optional[dict]:
    try:
        resp = requests.get(
            f"{url}/state",
            headers={"Authorization": f"Bearer {secret}"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logging.error("Error fetching state: %s", e)
        return None


# ===== Cold Turkey integration (transport-agnostic) =====

def is_coldturkey_blocking(coldturkey_path: str, block_name: str) -> bool:
    """Check if the named Cold Turkey block is currently active."""
    try:
        result = subprocess.run(
            [coldturkey_path, "-status", block_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = (result.stdout + result.stderr).lower()
        return "active" in output or "running" in output or "locked" in output
    except subprocess.TimeoutExpired:
        logging.warning("Timeout checking Cold Turkey status")
        return False
    except FileNotFoundError:
        logging.error("Cold Turkey not found at %s", coldturkey_path)
        return False


def start_block(
    coldturkey_path: str,
    block_name: str,
    minutes: int,
    lock: bool,
) -> bool:
    cmd = [coldturkey_path, "-start", block_name]
    if lock:
        cmd.extend(["-lock", str(minutes)])
    logging.info("Starting block: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            logging.error(
                "Cold Turkey start returned %d. stdout=%s stderr=%s",
                result.returncode, result.stdout, result.stderr,
            )
            return False
        return True
    except Exception as e:
        logging.error("Failed to start Cold Turkey: %s", e)
        return False


def stop_block(coldturkey_path: str, block_name: str) -> bool:
    cmd = [coldturkey_path, "-stop", block_name]
    logging.info("Stopping block: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            logging.warning(
                "Cold Turkey stop returned %d. stdout=%s stderr=%s",
                result.returncode, result.stdout, result.stderr,
            )
        return result.returncode == 0
    except Exception as e:
        logging.error("Failed to stop Cold Turkey: %s", e)
        return False


# ===== Shift (phone) integration =====

def run_shift(action: str, config: dict) -> bool:
    """
    Trigger Shift/Unshift on the phone via shift.py. Best-effort: failures are
    logged but never raised, so a phone-side problem can't break the Cold Turkey
    reconcile loop. `action` is "shift" or "unshift".

    Optional config keys:
      shift_enabled (bool, default true)  -- turn the integration on/off
      shift_serial  (str)                 -- target a specific device serial
      shift_name    (str)                 -- target by device name (substring)
      shift_timeout (int, default 60)     -- subprocess timeout in seconds
    """
    if not config.get("shift_enabled", True):
        return False
    if not SHIFT_SCRIPT.exists():
        logging.warning("shift.py not found at %s, skipping %s", SHIFT_SCRIPT, action)
        return False

    cmd = [sys.executable, str(SHIFT_SCRIPT), action]
    if config.get("shift_serial"):
        cmd += ["--serial", config["shift_serial"]]
    elif config.get("shift_name"):
        cmd += ["--name", config["shift_name"]]

    logging.info("Triggering phone %s: %s", action, " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=config.get("shift_timeout", 60),
        )
        if result.returncode != 0:
            logging.error(
                "shift.py %s failed (rc=%d): %s",
                action, result.returncode,
                (result.stderr or result.stdout).strip(),
            )
            return False
        logging.info("shift.py %s ok: %s", action, (result.stdout or "").strip())
        return True
    except subprocess.TimeoutExpired:
        logging.error("shift.py %s timed out", action)
        return False
    except Exception as e:
        logging.error("Failed to run shift.py %s: %s", action, e)
        return False


# ===== Reconciliation (transport-agnostic) =====

class Reconciler:
    """
    Edge-triggered reconciler: only invokes Cold Turkey on state transitions.

    We deliberately do NOT call `is_coldturkey_blocking()` on every tick.
    On macOS, every invocation of `Cold Turkey Blocker.app/Contents/MacOS/...`
    triggers a full NSApplication launch — the icon flashes in the Dock /
    menu bar before the CLI subcommand exits. Polling at 5s would make that
    flash constantly.

    Instead we treat the backend's desired state as authoritative, remember
    what we last issued locally, and only shell out when something actually
    changes. Cold Turkey itself holds the block until its lock expires, so
    we don't need to re-verify every tick.

    This class is transport-agnostic and will be reused by the future
    WebSocket transport.
    """

    def __init__(self, config: dict):
        self.config = config
        self._desired_block: Optional[bool] = None
        self._session_id: Optional[int] = None

    def reconcile(self, state: dict) -> None:
        should_block = state.get("should_block", False)
        session_id = state.get("session_id")

        if (
            should_block == self._desired_block
            and session_id == self._session_id
        ):
            return

        logging.info(
            "State change: block %s->%s, session %s->%s, state=%s",
            self._desired_block, should_block,
            self._session_id, session_id, state,
        )

        if should_block:
            minutes_remaining = state.get("minutes_remaining", 0)
            if minutes_remaining <= 0:
                logging.info(
                    "should_block true but minutes_remaining=0, skipping"
                )
                return
            start_block(
                self.config["coldturkey_path"],
                self.config["block_name"],
                minutes_remaining,
                self.config.get("lock_blocks", False),
            )
            run_shift("shift", self.config)
        else:
            stop_block(
                self.config["coldturkey_path"], self.config["block_name"]
            )
            run_shift("unshift", self.config)

        self._desired_block = should_block
        self._session_id = session_id


# ===== Transports =====

class PollingTransport:
    """Polls /state at a fixed interval and calls reconcile()."""

    def __init__(self, config: dict):
        self.config = config
        self.interval = config.get("poll_interval_seconds", 60)
        self.reconciler = Reconciler(config)
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        logging.info("Starting polling transport, interval=%ds", self.interval)
        while self._running:
            try:
                state = fetch_state(self.config["url"], self.config["secret"])
                if state is not None:
                    self.reconciler.reconcile(state)
            except Exception as e:
                logging.error("Reconcile loop error: %s", e)
            
            # Sleep in small increments so we can shut down quickly
            for _ in range(self.interval):
                if not self._running:
                    break
                time.sleep(1)


# Placeholder for future WebSocket transport
# class WebSocketTransport:
#     def __init__(self, config: dict):
#         self.reconciler = Reconciler(config)
#     def run(self):
#         # 1. fetch_state() and self.reconciler.reconcile() once on connect
#         # 2. open WebSocket to /ws
#         # 3. on message: self.reconciler.reconcile(state) (or re-fetch)
#         # 4. on disconnect: reconnect with exponential backoff
#         pass


# ===== Main =====

def main() -> None:
    setup_logging()
    try:
        config = load_config()
    except Exception as e:
        logging.error("Failed to load config: %s", e)
        sys.exit(1)

    transport_type = config.get("transport", "polling")

    if transport_type == "polling":
        transport = PollingTransport(config)
    # elif transport_type == "websocket":
    #     transport = WebSocketTransport(config)
    else:
        logging.error("Unknown transport: %s", transport_type)
        sys.exit(1)

    # Handle clean shutdown on signals
    def handle_signal(signum, frame):
        logging.info("Received signal %d, shutting down", signum)
        transport.stop()
    
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        transport.run()
    except KeyboardInterrupt:
        logging.info("Interrupted, shutting down")


if __name__ == "__main__":
    main()