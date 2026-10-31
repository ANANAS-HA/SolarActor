"""Email alerts from the console when a live run needs a human: an action waiting for approval, or an
escalation. Sent by the console layer (ui/live.py), never by the agent, through Resend's REST API.

    alerter = Alerter.from_env()              # RESEND_API_KEY, RAQEEB_ALERT_FROM, RAQEEB_ALERT_TO (.env or env)
    record = alerter.send(Alert(...))         # returns at once; delivery runs in a daemon thread
    alerter.drain()                           # finished deliveries, for the audit table

Off when RAQEEB_ALERTS=0 (the Docker image's default): nothing is sent or recorded. When the key is unset
the alert is only recorded (status NOT_CONFIGURED) and the console shows "Alert (email not configured)".
One email per request, one retry at most, never blocking the run; failures are logged and ignored.
The body carries the scenario (id, start, duration), the sim time, a one-line summary of the agent's
diagnosis so far, what needs deciding, the agent's rationale and expected effect, and a line to open the
console; Arabic and English summary lines; no plant data beyond that.

    uv run python -m raqeeb.ui.alerts --preview out.json [--send]   # the exact Resend payload, to a file

The preview writes what would be posted (from, to, subject, text) without sending; --send posts that one
sample email with the .env settings (Resend's free tier is small: test the format with the file).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Callable

from dotenv import dotenv_values

from raqeeb.config import ENV_FILE
from raqeeb.contracts import DISPLAY_TZ

log = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
ALERTS_ENV = "RAQEEB_ALERTS"  # "0" switches alerts off entirely (the public image)
KEY_ENV, FROM_ENV, TO_ENV = "RESEND_API_KEY", "RAQEEB_ALERT_FROM", "RAQEEB_ALERT_TO"
TIMEOUT_S = 10.0
RETRIES = 1  # one retry at most

APPROVAL, ESCALATION = "approval", "escalation"
SENT, FAILED, NOT_CONFIGURED, OFF = "sent", "failed", "not configured", "off"
BANNER_NOT_CONFIGURED = "Alert (email not configured)"

Poster = Callable[[str, dict, dict], int]  # (url, headers, payload) -> HTTP status


@dataclass(frozen=True)
class Alert:
    kind: str  # APPROVAL / ESCALATION
    ref: str  # action id or escalation subject
    scenario_id: str
    sim_time: datetime
    decision: str  # what needs deciding, one line
    rationale: str
    expected_effect: str
    scenario: str = ""  # start and duration, from the agent's view of the scenario
    summary: str = ""  # the agent's diagnosis so far (cause labels) and what else is open

    @property
    def subject(self) -> str:
        what = "action awaiting approval" if self.kind == APPROVAL else "escalation"
        return f"[Raqeeb] {self.scenario_id}: {what} ({self.ref})"

    def body(self) -> str:
        when = self.sim_time.astimezone(ZoneInfo(DISPLAY_TZ)).strftime("%Y-%m-%d %H:%M %Z")
        if self.kind == APPROVAL:
            ar = "يحتاج إجراء إلى موافقة المشغّل."
            en = "An action is waiting for operator approval."
        else:
            ar = "رقيب أحال حالة إلى المشغّل."
            en = "Raqeeb escalated a case to the operator."
        lines = [
            f"{en} / {ar}",
            "",
            f"Scenario: {self.scenario_id}" + (f" ({self.scenario})" if self.scenario else ""),
            f"Sim time: {when}",
        ]
        if self.summary:
            lines.append(f"Diagnosis so far: {self.summary}")
        lines += [
            "",
            f"Needs deciding: {self.decision}",
            f"Rationale: {self.rationale}",
            f"Expected effect: {self.expected_effect}",
            "",
            "Open the console to approve or reject. / افتح وحدة التحكم للموافقة أو الرفض.",
            "",
            "Raqeeb console alert. Synthetic plant and scenario; no plant data is included.",
        ]
        return "\n".join(lines)


@dataclass
class AlertRecord:
    alert: Alert
    status: str  # SENT / FAILED / NOT_CONFIGURED / OFF
    attempts: int = 0
    error: str = ""

    def payload(self) -> dict:
        return {"kind": self.alert.kind, "ref": self.alert.ref, "status": self.status,
                "attempts": self.attempts, "error": self.error}


@dataclass(frozen=True)
class AlertConfig:
    enabled: bool = True
    api_key: str = ""
    sender: str = ""
    recipients: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.sender and self.recipients)

    @classmethod
    def from_env(cls, env_file=ENV_FILE) -> AlertConfig:
        """Process environment wins over .env, as load_settings."""
        values = {k: v for k, v in dotenv_values(env_file).items() if v} if env_file.exists() else {}
        values.update({k: v for k, v in os.environ.items() if k in (ALERTS_ENV, KEY_ENV, FROM_ENV, TO_ENV) and v})
        return cls(enabled=values.get(ALERTS_ENV, "1") != "0", api_key=values.get(KEY_ENV, ""),
                   sender=values.get(FROM_ENV, ""),
                   recipients=tuple(r.strip() for r in values.get(TO_ENV, "").split(",") if r.strip()))

    def __repr__(self) -> str:  # never print the key
        return f"AlertConfig(enabled={self.enabled}, configured={self.configured}, recipients={len(self.recipients)})"


def post_json(url: str, headers: dict, payload: dict) -> int:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


class Alerter:
    """Sends alerts without blocking: send() returns a record at once and the HTTP call runs in a daemon
    thread (synchronously when `background=False`, for tests). Finished deliveries queue up for drain()."""

    def __init__(self, config: AlertConfig, post: Poster = post_json, background: bool = True):
        self.config = config
        self._post = post
        self._background = background
        self._done: deque[AlertRecord] = deque()
        self._lock = threading.Lock()
        self.records: list[AlertRecord] = []  # every alert of the run, in order (shown by the console)
        self._sent: set[tuple[str, str]] = set()  # one email per request

    @classmethod
    def from_env(cls) -> Alerter:
        return cls(AlertConfig.from_env())

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def configured(self) -> bool:
        return self.config.configured

    def send(self, alert: Alert) -> AlertRecord | None:
        """None when alerts are off or this request was already alerted; otherwise the record (status
        NOT_CONFIGURED without a key, or "sending" while the thread works)."""
        if not self.config.enabled or (alert.kind, alert.ref) in self._sent:
            return None
        self._sent.add((alert.kind, alert.ref))
        if not self.config.configured:
            record = AlertRecord(alert, NOT_CONFIGURED)
            self.records.append(record)
            return record
        record = AlertRecord(alert, "sending")
        self.records.append(record)
        if self._background:
            threading.Thread(target=self._deliver, args=(record,), daemon=True, name="raqeeb-alert").start()
        else:
            self._deliver(record)
        return record

    def _deliver(self, record: AlertRecord) -> None:
        headers = {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}
        body = payload(record.alert, self.config)
        error = ""
        for attempt in range(1 + RETRIES):
            record.attempts = attempt + 1
            try:
                status = self._post(RESEND_URL, headers, body)
                if 200 <= status < 300:
                    record.status = SENT
                    break
                error = f"HTTP {status}"
            except Exception as exc:  # noqa: BLE001 - delivery failures never reach the run
                error = f"{type(exc).__name__}: {exc}"
        else:
            record.status, record.error = FAILED, error
            log.warning("alert email not sent (%s): %s", record.alert.ref, error)
        with self._lock:
            self._done.append(record)

    def drain(self) -> list[AlertRecord]:
        """Deliveries finished since the last call (for the audit table)."""
        with self._lock:
            out, self._done = list(self._done), deque()
        return out


def approval_alert(scenario_id: str, now: datetime, action_id: str, action: str, target: str,
                   rationale: str, expected_effect: str, scenario: str = "", summary: str = "") -> Alert:
    return Alert(APPROVAL, action_id, scenario_id, now, f"approve or reject {action} on {target}",
                 rationale, expected_effect, scenario, summary)


def escalation_alert(scenario_id: str, now: datetime, subject: str, devices: list[str], reason: str,
                     scenario: str = "", summary: str = "") -> Alert:
    where = f" ({', '.join(devices)})" if devices else ""
    return Alert(ESCALATION, subject, scenario_id, now, f"escalation: {subject}{where}", reason,
                 "a human takes over this cause", scenario, summary)


def payload(alert: Alert, config: AlertConfig) -> dict:
    """What is posted to Resend for this alert."""
    return {"from": config.sender, "to": list(config.recipients), "subject": alert.subject, "text": alert.body()}


def _sample() -> Alert:
    start = datetime(2025, 7, 10, 3, 0, tzinfo=ZoneInfo("UTC"))
    return approval_alert(
        "scn_0007", start.replace(hour=9, minute=15), "act-3", "SET_INVERTER_POWER_LIMIT", "INV-12",
        "INV-12 runs 14 °C hotter than its peers at the same load; a derate protects it until the fan is checked",
        "INV-12 output limited to 70% of rating; about 1.2% of plant power until the technician clears it",
        scenario="started 2025-07-10 06:00 Asia/Riyadh, 12 h", summary="SOILING@SITE, SENSOR_SOILING@MET-01; "
        "1 action waiting for approval, 0 escalations")


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Preview (and optionally send) one sample alert email.")
    ap.add_argument("--preview", metavar="FILE", required=True, help="write the Resend payload here as JSON")
    ap.add_argument("--send", action="store_true", help="also post that one email with the .env settings")
    args = ap.parse_args(argv)
    config = AlertConfig.from_env()
    alert = _sample()
    body = payload(alert, config)
    Path(args.preview).write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"payload written to {args.preview} ({config!r})")
    if args.send:
        if not config.configured:
            print("not sent: RESEND_API_KEY, RAQEEB_ALERT_FROM and RAQEEB_ALERT_TO are not all set", file=sys.stderr)
            return 1
        record = Alerter(config, background=False).send(alert)
        print(f"{record.status} after {record.attempts} attempt(s)" + (f": {record.error}" if record.error else ""))
        return 0 if record.status == SENT else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
