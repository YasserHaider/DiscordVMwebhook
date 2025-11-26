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

import psutil
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

# Important system indicators for the system-wide journal stream; keeps focus on VM-level issues.
SYSTEM_IMPORTANT_KEYS = [
    "kernel",
    "kernel:",
    "OOM",
    "oom-kill",
    "Out of memory",
    "I/O error",
    "I/O",
    "EXT4-fs error",
    "buffer I/O error",
    "segfault",
    "panic",
    "failed",
    "Failed",
    "Shutdown",
    "Reboot",
    "Azure",
    "disk",
    "journal",
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

    def __init__(self, rate_limit_seconds: float = 0.2) -> None:
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


class ErrorAggregator:
    """Captures complete error/traceback blocks for a stream without altering case."""

    def __init__(self, stream_name: str, webhook_url: str, dispatcher: WebhookDispatcher, system_stream: bool) -> None:
        self.stream_name = stream_name
        self.webhook_url = webhook_url
        self.dispatcher = dispatcher
        self.system_stream = system_stream
        self.active = False
        self.lines: List[str] = []
        self.last_line_time: float = 0.0
        self.lock = threading.Lock()

    def process_line(self, line: str) -> bool:
        """
        Returns True if the line is being captured as part of an error block.
        """
        with self.lock:
            now = time.time()
            if self.active:
                if line == "":
                    self.flush()
                    return False
                if now - self.last_line_time > 1.0:
                    self.flush()
                    # After flush, continue evaluating the line fresh below.
            if self.active:
                self.lines.append(line)
                self.last_line_time = now
                return True
            if any(key in line for key in ERROR_KEYS):
                self.active = True
                self.lines = [line]
                self.last_line_time = now
                return True
            return False

    def flush(self) -> None:
        with self.lock:
            if not self.active or not self.lines:
                self.active = False
                self.lines = []
                return
            block_text = "\n".join(self.lines)
            content = f"```log\n{block_text}\n```"
            self.dispatcher.enqueue(self.webhook_url, content)
            alert = "⚠️ SYSTEM ERROR detected!" if self.system_stream else "⚠️ ERROR detected!"
            if self.system_stream:
                alert = append_system_ping(alert)
            else:
                alert = append_ping(alert, PING_ROLE_ID)
            self.dispatcher.enqueue(self.webhook_url, alert)
            self.active = False
            self.lines = []
            self.last_line_time = 0.0


class LogStream:
    """Handles journald reading, per-stream batching, and error aggregation."""

    def __init__(
        self,
        name: str,
        command: List[str],
        webhook_url: str,
        dispatcher: WebhookDispatcher,
        stop_event: threading.Event,
        system_webhook: str,
        system_stream: bool = False,
        recovery_monitor: Optional["RecoveryMonitor"] = None,
        label: str = "",
    ) -> None:
        self.name = name
        self.command = command
        self.webhook_url = webhook_url
        self.dispatcher = dispatcher
        self.stop_event = stop_event
        self.system_webhook = system_webhook
        self.system_stream = system_stream
        self.recovery_monitor = recovery_monitor
        self.label = label
        self.buffer: List[str] = []
        self.buffer_lock = threading.Lock()
        self.error_agg = ErrorAggregator(name, webhook_url, dispatcher, system_stream)
        self.process = self._start_process()
        self.reader_thread = threading.Thread(target=self._reader, daemon=True)
        self.reader_thread.start()
        self.flush_thread = threading.Thread(target=self._flusher, daemon=True)
        self.flush_thread.start()

    def _start_process(self) -> subprocess.Popen:
        return subprocess.Popen(
            self.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
        )

    def _should_forward_system_line(self, line: str) -> bool:
        if any(key in line for key in ERROR_KEYS):
            return True
        return any(key in line for key in SYSTEM_IMPORTANT_KEYS)

    def _reader(self) -> None:
        try:
            if self.process.stdout is None:
                raise RuntimeError(f"Failed to capture journalctl output for {self.name}")
            for raw_line in self.process.stdout:
                if self.stop_event.is_set():
                    break
                line = raw_line.rstrip("\n")
                # Always notify recovery monitor when timer logs arrive.
                if self.recovery_monitor and self.label == "timer":
                    self.recovery_monitor.record_recover_event()
                # System stream filtering to focus on important VM events.
                if self.system_stream and not self._should_forward_system_line(line):
                    continue
                if self.error_agg.process_line(line):
                    continue
                if line == "":
                    continue
                with self.buffer_lock:
                    self.buffer.append(line)
        finally:
            if not self.stop_event.is_set():
                send_system_event(
                    self.dispatcher,
                    self.system_webhook,
                    f"❌ Log stream for {self.name} stopped unexpectedly.",
                )

    def _flusher(self) -> None:
        while not self.stop_event.is_set():
            time.sleep(2)
            self.error_agg.flush()
            self.flush_buffer()
        # Final flush on shutdown.
        self.error_agg.flush()
        self.flush_buffer()

    def flush_buffer(self) -> None:
        with self.buffer_lock:
            if not self.buffer:
                return
            content = "```log\n" + "\n".join(self.buffer) + "\n```"
            self.buffer.clear()
        self.dispatcher.enqueue(self.webhook_url, content)

    def terminate(self) -> None:
        try:
            self.process.terminate()
        except Exception:
            pass

    def wait(self, timeout: Optional[float] = None) -> None:
        try:
            self.process.wait(timeout=timeout)
        except Exception:
            pass


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


class StatusPanel:
    """Maintains a single live-updating status message in the system webhook channel."""

    def __init__(self, system_webhook: str) -> None:
        self.system_webhook = system_webhook
        self.message_id: Optional[str] = None
        self.last_net: Optional[psutil._common.snetio] = None
        self.lock = threading.Lock()

    def _format_status(self) -> str:
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage("/")
        load1, load5, load15 = os.getloadavg()
        boot_time = psutil.boot_time()
        uptime_seconds = time.time() - boot_time
        hours = int(uptime_seconds // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        secs = int(uptime_seconds % 60)
        uptime = f"{hours}h {minutes}m {secs}s"
        cpu_times = psutil.cpu_times_percent(interval=None)
        iowait = getattr(cpu_times, "iowait", 0.0)
        now_net = psutil.net_io_counters()
        net_line = ""
        if self.last_net:
            sent_delta = max(now_net.bytes_sent - self.last_net.bytes_sent, 0)
            recv_delta = max(now_net.bytes_recv - self.last_net.bytes_recv, 0)
            net_line = f"📡 Net: ↑ {sent_delta/1024/1024:.2f} MB/s · ↓ {recv_delta/1024/1024:.2f} MB/s\n"
        self.last_net = now_net

        status = (
            "🖥️ **VM Status — Live**\n\n"
            f"🧠 CPU: {cpu:.1f}%\n"
            f"📦 RAM: {mem.used / (1024**3):.2f} / {mem.total / (1024**3):.2f} GB ({mem.percent:.1f}%)\n"
            f"🧊 Swap: {swap.used / (1024**3):.2f} / {swap.total / (1024**3):.2f} GB ({swap.percent:.1f}%)\n"
            f"💾 Disk: {disk.percent:.1f}% used\n"
            f"🔺 Load: {load1:.2f} / {load5:.2f} / {load15:.2f}\n"
            f"🔁 I/O Wait: {iowait:.2f}%\n"
            f"⏱️ Uptime: {uptime}\n"
        )
        if net_line:
            status += net_line
        return status

    def update(self) -> None:
        content = self._format_status()
        with self.lock:
            try:
                if self.message_id is None:
                    resp = requests.post(self.system_webhook, json={"content": content}, timeout=5)
                    if resp.ok:
                        data = resp.json()
                        self.message_id = data.get("id")
                else:
                    requests.patch(
                        f"{self.system_webhook}/messages/{self.message_id}",
                        json={"content": content},
                        timeout=5,
                    )
            except Exception:
                # Never crash the logger on status update failures.
                pass


class SystemAlertMonitor:
    """Sends alerts based on system metrics thresholds with system role pings."""

    def __init__(self, dispatcher: WebhookDispatcher, system_webhook: str) -> None:
        self.dispatcher = dispatcher
        self.system_webhook = system_webhook
        self.cpu_high_since: Optional[float] = None
        self.alerted_flags: Dict[str, bool] = {
            "ram": False,
            "swap": False,
            "disk": False,
        }

    def check_metrics(self) -> None:
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage("/")

        now = time.time()
        if cpu > 90.0:
            if self.cpu_high_since is None:
                self.cpu_high_since = now
            elif now - self.cpu_high_since > 10:
                self._alert("⚠️ SYSTEM WARNING: CPU usage sustained above 90%.")
                self.cpu_high_since = now  # reset to avoid rapid repeats
        else:
            self.cpu_high_since = None

        if mem.percent > 90.0 and not self.alerted_flags["ram"]:
            self._alert("⚠️ SYSTEM WARNING: RAM usage above 90%.")
            self.alerted_flags["ram"] = True
        if mem.percent <= 85.0:
            self.alerted_flags["ram"] = False

        if swap.used > 0 and not self.alerted_flags["swap"]:
            self._alert("⚠️ SYSTEM WARNING: Swap space in use.")
            self.alerted_flags["swap"] = True
        if swap.used == 0:
            self.alerted_flags["swap"] = False

        if disk.percent > 95.0 and not self.alerted_flags["disk"]:
            self._alert("⚠️ SYSTEM WARNING: Disk usage above 95%.")
            self.alerted_flags["disk"] = True
        if disk.percent <= 90.0:
            self.alerted_flags["disk"] = False

    def _alert(self, message: str) -> None:
        self.dispatcher.enqueue(self.system_webhook, append_system_ping(message))


def start_log_streams(
    dispatcher: WebhookDispatcher,
    stop_event: threading.Event,
    system_webhook: str,
    config: Dict[str, Any],
    recovery_monitor: RecoveryMonitor,
) -> List[LogStream]:
    streams: List[LogStream] = []
    console_unit = config.get("systemd_console_service", "redbot.service")
    dashboard_unit = config.get("systemd_dashboard_service", "reddash.service")
    recover_timer_unit = config.get("systemd_recover_timer", "redbot-recover.timer")

    streams.append(
        LogStream(
            name=console_unit,
            command=["journalctl", "-u", console_unit, "-f", "-n", "0", "-o", "cat"],
            webhook_url=config.get("console_webhook", ""),
            dispatcher=dispatcher,
            stop_event=stop_event,
            system_webhook=system_webhook,
            recovery_monitor=recovery_monitor,
            label="console",
        )
    )

    streams.append(
        LogStream(
            name=dashboard_unit,
            command=["journalctl", "-u", dashboard_unit, "-f", "-n", "0", "-o", "cat"],
            webhook_url=config.get("dashboard_webhook", ""),
            dispatcher=dispatcher,
            stop_event=stop_event,
            system_webhook=system_webhook,
            recovery_monitor=recovery_monitor,
            label="dashboard",
        )
    )

    streams.append(
        LogStream(
            name=recover_timer_unit,
            command=["journalctl", "-u", recover_timer_unit, "-f", "-n", "0", "-o", "cat"],
            webhook_url=system_webhook,
            dispatcher=dispatcher,
            stop_event=stop_event,
            system_webhook=system_webhook,
            system_stream=True,
            recovery_monitor=recovery_monitor,
            label="timer",
        )
    )

    # System-wide journal stream filtered for important VM events only.
    streams.append(
        LogStream(
            name="system",
            command=["journalctl", "-f", "-n", "0", "-o", "cat"],
            webhook_url=system_webhook,
            dispatcher=dispatcher,
            stop_event=stop_event,
            system_webhook=system_webhook,
            system_stream=True,
            recovery_monitor=recovery_monitor,
            label="system",
        )
    )

    return streams


def main() -> None:
    config = load_config()
    system_webhook = config.get("system_webhook", "")
    rate_limit_seconds = float(config.get("rate_limit_seconds", 0.2))

    dispatcher = WebhookDispatcher(rate_limit_seconds=rate_limit_seconds)
    stop_event = threading.Event()

    recovery_monitor = RecoveryMonitor(config.get("systemd_console_service", "redbot.service"), system_webhook, dispatcher)

    # Send startup event to system webhook with optional ping.
    send_system_event(dispatcher, system_webhook, "✅ Logger service started on Azure VM")

    streams = start_log_streams(dispatcher, stop_event, system_webhook, config, recovery_monitor)

    def handle_shutdown(signum: int, _frame: Any) -> None:
        # SIGTERM from systemd or Azure deallocation triggers graceful shutdown messaging.
        stop_event.set()
        send_system_event(
            dispatcher,
            system_webhook,
            "🛑 Azure VM (or logger service) shutting down, Redbot logs going offline...",
        )
        for stream in streams:
            stream.terminate()

    # Register signal handlers for SIGTERM and SIGINT.
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    status_panel = StatusPanel(system_webhook)
    alerts = SystemAlertMonitor(dispatcher, system_webhook)

    def recovery_checker() -> None:
        while not stop_event.is_set():
            recovery_monitor.check_service_state()
            time.sleep(30)

    def status_updater() -> None:
        while not stop_event.is_set():
            status_panel.update()
            alerts.check_metrics()
            time.sleep(5)

    threading.Thread(target=recovery_checker, daemon=True).start()
    threading.Thread(target=status_updater, daemon=True).start()

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    finally:
        stop_event.set()
        for stream in streams:
            stream.terminate()
        # Allow processes to settle briefly then flush dispatch queue.
        for stream in streams:
            stream.wait(timeout=1.0)
        dispatcher.close(timeout=3.0)


if __name__ == "__main__":
    main()
