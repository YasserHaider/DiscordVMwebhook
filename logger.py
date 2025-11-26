import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

# Load environment variables for role pings.
load_dotenv()

# Case sensitivity rules:
# - Never transform text to lowercase or uppercase.
# - Matching uses exact substrings, preserving case.
ERROR_KEYS = [
    "ERROR",
    "Error",
    "CRITICAL",
    "Critical",
    "WARNING",
    "Warning",
    "Traceback",
    "Exception",
]

DASHBOARD_KEYS = ["Dashboard", "Webserver", "HTTP"]

PING_ROLE_ID = os.getenv("PING_ROLE_ID", "")
SYSTEM_PING_ROLE_ID = os.getenv("SYSTEM_PING_ROLE_ID", "")


class WebhookDispatcher:
    """
    Queue-based dispatcher to send webhook messages without blocking Redbot output.

    Design notes:
    - Uses queue.Queue to buffer messages so stdout reading never blocks on network I/O.
    - Worker thread enforces a small delay between requests to avoid Discord 429 rate limits.
    - During shutdown the worker flushes the queue; a sentinel None value stops the thread.
    """

    def __init__(self, rate_limit_seconds: float = 0.75) -> None:
        self.queue: "queue.Queue[Optional[Dict[str, str]]]" = queue.Queue()
        self.rate_limit_seconds = rate_limit_seconds
        self.stop_event = threading.Event()
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                item = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            webhook_url = item.get("url")
            content = item.get("content", "")
            try:
                requests.post(webhook_url, json={"content": content}, timeout=5)
            except Exception:
                # We intentionally avoid altering log flow; failures are silent but non-blocking.
                pass
            time.sleep(self.rate_limit_seconds)
        # Drain remaining items quickly on shutdown without adding new ones.
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                continue
            try:
                requests.post(item.get("url"), json={"content": item.get("content", "")}, timeout=5)
            except Exception:
                pass
            time.sleep(0.1)

    def enqueue(self, webhook_url: str, content: str) -> None:
        if not content or not webhook_url:
            return
        self.queue.put({"url": webhook_url, "content": content})

    def close(self, timeout: float = 3.0) -> None:
        self.queue.put(None)
        self.stop_event.set()
        self.worker.join(timeout)


def load_config() -> Dict[str, Any]:
    with open("config.json", "r", encoding="utf-8") as cfg:
        return json.load(cfg)


def append_ping(content: str, ping_id: str) -> str:
    if ping_id:
        return f"{content} <@&{ping_id}>"
    return content


def send_system_event(dispatcher: WebhookDispatcher, webhook_url: str, message: str) -> None:
    dispatcher.enqueue(webhook_url, append_ping(message, SYSTEM_PING_ROLE_ID))


def main() -> None:
    config = load_config()
    console_webhook = config.get("console_webhook", "")
    dashboard_webhook = config.get("dashboard_webhook", "")
    system_webhook = config.get("system_webhook", "")
    redbot_cmd = config.get("redbot_command", [])
    working_dir = config.get("working_dir")
    rate_limit_seconds = float(config.get("rate_limit_seconds", 0.75))

    if not redbot_cmd:
        print("Redbot command is not configured.", file=sys.stderr)
        sys.exit(1)

    dispatcher = WebhookDispatcher(rate_limit_seconds=rate_limit_seconds)
    stop_event = threading.Event()

    # Send startup event to system webhook with optional ping.
    send_system_event(dispatcher, system_webhook, "✅ Logger service started on Azure VM")

    # Launch Redbot process with stdout piped. stderr is merged to preserve order.
    process = subprocess.Popen(
        redbot_cmd,
        cwd=working_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )

    def handle_shutdown(signum: int, _frame: Any) -> None:
        # SIGTERM from systemd or Azure deallocation triggers graceful shutdown messaging.
        stop_event.set()
        send_system_event(dispatcher, system_webhook, "🛑 Azure VM shutting down, Redbot offline...")
        if process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass

    # Register signal handlers for SIGTERM and SIGINT.
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    def watchdog() -> None:
        # Watchdog checks every 5 seconds to ensure Redbot is alive.
        # If it detects a crash, it notifies system webhook and exits so systemd can restart.
        while not stop_event.is_set():
            time.sleep(5)
            if process.poll() is not None:
                send_system_event(dispatcher, system_webhook, "❌ Redbot process crashed!")
                stop_event.set()
                break

    watchdog_thread = threading.Thread(target=watchdog, daemon=True)
    watchdog_thread.start()

    try:
        if process.stdout is None:
            raise RuntimeError("Failed to capture Redbot stdout")
        for raw_line in process.stdout:
            if stop_event.is_set():
                break
            # Remove trailing newline only; preserve all other characters exactly.
            line = raw_line.rstrip("\n")
            if not line:
                continue
            message = line
            if any(key in line for key in ERROR_KEYS):
                message = append_ping(message, PING_ROLE_ID)
            dispatcher.enqueue(console_webhook, message)

            if any(key in line for key in DASHBOARD_KEYS):
                dispatcher.enqueue(dashboard_webhook, message)
    finally:
        stop_event.set()
        try:
            if process.poll() is None:
                process.terminate()
        except Exception:
            pass
        watchdog_thread.join(timeout=2.0)

    # If Redbot exited unexpectedly without the watchdog catching it yet, send crash notice.
    if process.returncode not in (0, None) and not stop_event.is_set():
        send_system_event(dispatcher, system_webhook, "❌ Redbot process crashed!")

    # Flush remaining messages before exit; system webhook gets final shutdown message when relevant.
    dispatcher.close(timeout=3.0)


if __name__ == "__main__":
    main()
