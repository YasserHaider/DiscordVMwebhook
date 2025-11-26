import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

# Load environment variables for role pings using python-dotenv without altering case.
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

PING_ROLE_ID = os.getenv("PING_ROLE_ID", "")
SYSTEM_PING_ROLE_ID = os.getenv("SYSTEM_PING_ROLE_ID", "")


@dataclass
class QueueItem:
    url: str
    payload: Dict[str, Any]


class WebhookDispatcher:
    """
    Queue-based dispatcher to send webhook messages without blocking log readers.

    Design notes:
    - Uses queue.Queue to buffer messages so stdout reading never blocks on network I/O.
    - Worker thread enforces a small delay between requests to avoid Discord 429 rate limits.
    - During shutdown the worker flushes the queue; a sentinel None value stops the thread.
    - No case changes are applied; payload content is sent exactly as provided.
    """

    def __init__(self, rate_limit_seconds: float = 0.75) -> None:
        self.queue: "queue.Queue[Optional[QueueItem]]" = queue.Queue()
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
            try:
                requests.post(item.url, json=item.payload, timeout=5)
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
                requests.post(item.url, json=item.payload, timeout=5)
            except Exception:
                pass
            time.sleep(0.1)

    def enqueue(self, webhook_url: str, content: str, components: Optional[List[Dict[str, Any]]] = None) -> None:
        if not content or not webhook_url:
            return
        payload: Dict[str, Any] = {"content": content}
        if components:
            payload["components"] = components
        self.queue.put(QueueItem(url=webhook_url, payload=payload))

    def close(self, timeout: float = 3.0) -> None:
        self.queue.put(None)
        self.stop_event.set()
        self.worker.join(timeout)


class RecoveryMonitor:
    """
    Tracks recovery attempts and Redbot activity to emit a last-resort warning with a Force Start button.

    - Does not start Redbot automatically; only warns operators.
    - Uses systemctl is-active results to detect persistent downtime.
    - Requires separate infrastructure (bot interaction handler or secure endpoint) to make the button functional.
    """

    def __init__(self, console_service: str, system_webhook: str, dispatcher: WebhookDispatcher) -> None:
        self.console_service = console_service
        self.system_webhook = system_webhook
        self.dispatcher = dispatcher
        self.failure_start_time: Optional[float] = None
        self.recover_events: List[float] = []
        self.alert_sent = False
        self.lock = threading.Lock()

    def record_recover_event(self) -> None:
        with self.lock:
            now = time.time()
            self.recover_events.append(now)
            # Keep only recent events (10 minutes) to limit list growth.
            self.recover_events = [t for t in self.recover_events if now - t <= 600]

    def mark_recovered(self) -> None:
        with self.lock:
            self.failure_start_time = None
            self.alert_sent = False

    def check_service_state(self) -> None:
        with self.lock:
            now = time.time()
            status = subprocess.run(
                ["systemctl", "is-active", self.console_service],
                capture_output=True,
                text=True,
                check=False,
            )
            state = status.stdout.strip()
            if state == "active":
                self.failure_start_time = None
                self.alert_sent = False
                return
            if self.failure_start_time is None:
                self.failure_start_time = now
                return
            recent_recoveries = [t for t in self.recover_events if now - t <= 600]
            if not self.alert_sent and (now - self.failure_start_time) >= 300 and len(recent_recoveries) >= 2:
                warning_lines = [
                    "❗ Redbot appears to still be offline despite recovery attempts.",
                    "⚠️ DO NOT PRESS THIS BUTTON IMMEDIATELY.",
                    "WAIT AT LEAST 5 MINUTES BEFORE USING THIS.",
                    "ONLY USE THIS IF SYSTEMD RECOVERY HAS FAILED.",
                    "This message is a last resort reminder. It does not auto-start Redbot.",
                ]
                content = append_system_ping("\n".join(warning_lines))
                # The button requires an external interaction handler (bot or secure endpoint).
                components = [
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 2,
                                "style": 4,
                                "label": "Force Start Redbot (LAST RESORT)",
                                "custom_id": "force_start_redbot",
                            }
                        ],
                    }
                ]
                self.dispatcher.enqueue(self.system_webhook, content, components=components)
                self.alert_sent = True


def load_config() -> Dict[str, Any]:
    with open("config.json", "r", encoding="utf-8") as cfg:
        return json.load(cfg)


def append_ping(content: str, ping_id: str) -> str:
    if ping_id:
        return f"{content} <@&{ping_id}>"
    return content


def append_system_ping(content: str) -> str:
    return append_ping(content, SYSTEM_PING_ROLE_ID)


def send_system_event(dispatcher: WebhookDispatcher, webhook_url: str, message: str) -> None:
    dispatcher.enqueue(webhook_url, append_system_ping(message))


def start_journal_reader(
    unit_name: str,
    webhook_url: str,
    dispatcher: WebhookDispatcher,
    stop_event: threading.Event,
    system_webhook: str,
    recovery_monitor: Optional[RecoveryMonitor] = None,
    label: str = "",
) -> subprocess.Popen:
    # Read journalctl output for the provided unit without altering case; -o cat omits metadata.
    process = subprocess.Popen(
        ["journalctl", "-u", unit_name, "-f", "-n", "0", "-o", "cat"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )

    def reader() -> None:
        try:
            if process.stdout is None:
                raise RuntimeError(f"Failed to capture journalctl output for {unit_name}")
            for raw_line in process.stdout:
                if stop_event.is_set():
                    break
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                message = line
                if any(key in line for key in ERROR_KEYS):
                    message = append_ping(message, PING_ROLE_ID)
                dispatcher.enqueue(webhook_url, message)
                if recovery_monitor and label == "timer":
                    recovery_monitor.record_recover_event()
        finally:
            if not stop_event.is_set():
                send_system_event(
                    dispatcher,
                    system_webhook,
                    f"❌ Log stream for {unit_name} stopped unexpectedly.",
                )

    threading.Thread(target=reader, daemon=True).start()
    return process


def main() -> None:
    config = load_config()
    console_webhook = config.get("console_webhook", "")
    dashboard_webhook = config.get("dashboard_webhook", "")
    system_webhook = config.get("system_webhook", "")
    console_unit = config.get("systemd_console_service", "redbot.service")
    dashboard_unit = config.get("systemd_dashboard_service", "reddash.service")
    recover_timer_unit = config.get("systemd_recover_timer", "redbot-recover.timer")
    rate_limit_seconds = float(config.get("rate_limit_seconds", 0.75))

    dispatcher = WebhookDispatcher(rate_limit_seconds=rate_limit_seconds)
    stop_event = threading.Event()

    recovery_monitor = RecoveryMonitor(console_unit, system_webhook, dispatcher)

    # Send startup event to system webhook with optional ping.
    send_system_event(dispatcher, system_webhook, "✅ Logger service started on Azure VM")

    console_proc = start_journal_reader(
        console_unit,
        console_webhook,
        dispatcher,
        stop_event,
        system_webhook,
        recovery_monitor=recovery_monitor,
        label="console",
    )
    dashboard_proc = start_journal_reader(
        dashboard_unit,
        dashboard_webhook,
        dispatcher,
        stop_event,
        system_webhook,
        recovery_monitor=recovery_monitor,
        label="dashboard",
    )
    timer_proc = start_journal_reader(
        recover_timer_unit,
        system_webhook,
        dispatcher,
        stop_event,
        system_webhook,
        recovery_monitor=recovery_monitor,
        label="timer",
    )

    def handle_shutdown(signum: int, _frame: Any) -> None:
        # SIGTERM from systemd or Azure deallocation triggers graceful shutdown messaging.
        stop_event.set()
        send_system_event(
            dispatcher,
            system_webhook,
            "🛑 Azure VM (or logger service) shutting down, Redbot logs going offline...",
        )
        for proc in (console_proc, dashboard_proc, timer_proc):
            try:
                proc.terminate()
            except Exception:
                pass

    # Register signal handlers for SIGTERM and SIGINT.
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    def recovery_checker() -> None:
        # Periodically verifies Redbot activity and whether recovery attempts are failing.
        while not stop_event.is_set():
            recovery_monitor.check_service_state()
            time.sleep(30)

    threading.Thread(target=recovery_checker, daemon=True).start()

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    finally:
        stop_event.set()
        for proc in (console_proc, dashboard_proc, timer_proc):
            try:
                proc.terminate()
            except Exception:
                pass

    dispatcher.close(timeout=3.0)


if __name__ == "__main__":
    main()
