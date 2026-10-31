"""Console email alerts (ui/alerts.py): one email per request through a mocked HTTP call, nothing sent when
unconfigured (recorded and shown as a banner), failures logged and ignored, off with RAQEEB_ALERTS=0, and
a live run that alerts when a gated action waits. No network: the poster is always a fake."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from streamlit.testing.v1 import AppTest

from raqeeb.config import REPO_ROOT
from raqeeb.ui import alerts, live, source
from raqeeb.ui.alerts import Alert, AlertConfig, Alerter, approval_alert, escalation_alert
from raqeeb.ui.runlog import Mode

APP = REPO_ROOT / "raqeeb" / "ui" / "app.py"
NOW = datetime(2025, 7, 10, 9, 30, tzinfo=timezone.utc)
CONFIGURED = AlertConfig(enabled=True, api_key="re_test_key", sender="Raqeeb <alerts@example.com>",
                         recipients=("ops@example.com", "lead@example.com"))


class FakePost:
    def __init__(self, statuses=(200,), raise_=None):
        self.statuses, self.raise_, self.calls = list(statuses), raise_, []

    def __call__(self, url, headers, payload):
        self.calls.append((url, headers, payload))
        if self.raise_ is not None:
            raise self.raise_
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]


def _alert(ref="act-1") -> Alert:
    return approval_alert("scn_0007", NOW, ref, "SET_INVERTER_POWER_LIMIT", "INV-12",
                          "the inverter runs hot", "power held at 70% until the fan is checked")


def test_sent_once_per_request_with_the_resend_payload():
    post = FakePost()
    alerter = Alerter(CONFIGURED, post=post, background=False)
    first = alerter.send(_alert())
    assert first.status == alerts.SENT and first.attempts == 1
    assert alerter.send(_alert()) is None  # the same request again: no second email
    assert alerter.send(_alert("act-2")).status == alerts.SENT
    assert len(post.calls) == 2
    url, headers, payload = post.calls[0]
    assert url == alerts.RESEND_URL and headers["Authorization"] == "Bearer re_test_key"
    assert payload["from"] == CONFIGURED.sender and payload["to"] == list(CONFIGURED.recipients)
    assert "scn_0007" in payload["subject"] and "act-1" in payload["subject"]
    body = payload["text"]
    for part in ("scn_0007", "12:30", "approve or reject SET_INVERTER_POWER_LIMIT on INV-12",
                 "the inverter runs hot", "power held at 70%", "Open the console to approve or reject",
                 "موافقة"):
        assert part in body, part
    assert "kw" not in body.lower() and "ghi" not in body.lower()  # no plant data beyond the request
    assert [r.alert.ref for r in alerter.drain()] == ["act-1", "act-2"] and alerter.drain() == []
    assert "re_test_key" not in repr(alerter.config)


def test_not_sent_when_unconfigured():
    post = FakePost()
    alerter = Alerter(AlertConfig(enabled=True), post=post, background=False)
    record = alerter.send(_alert())
    assert record.status == alerts.NOT_CONFIGURED and not alerter.configured
    assert post.calls == [] and alerter.drain() == []
    assert record.payload() == {"kind": "approval", "ref": "act-1", "status": "not configured", "attempts": 0,
                                "error": ""}


def test_failure_is_logged_and_ignored(caplog):
    post = FakePost(raise_=OSError("no route to host"))
    alerter = Alerter(CONFIGURED, post=post, background=False)
    with caplog.at_level(logging.WARNING, logger="raqeeb.ui.alerts"):
        record = alerter.send(_alert())
    assert record.status == alerts.FAILED and record.attempts == 2 and "no route to host" in record.error
    assert len(post.calls) == 2  # one retry, never more
    assert any("alert email not sent" in m for m in caplog.messages)

    post = FakePost(statuses=[500, 200])  # a server error, then success
    record = Alerter(CONFIGURED, post=post, background=False).send(_alert())
    assert record.status == alerts.SENT and record.attempts == 2

    record = Alerter(CONFIGURED, post=FakePost(statuses=[401]), background=False).send(_alert())
    assert record.status == alerts.FAILED and record.error == "HTTP 401"


def test_refusal_keeps_the_reason_and_the_request_names_itself():
    """Cloudflare in front of api.resend.com answers Python's default User-Agent with 403 "error code: 1010";
    the request must carry its own agent, and a refusal's body must reach the record (and the console)."""
    post = FakePost()
    Alerter(CONFIGURED, post=post, background=False).send(_alert())
    headers = post.calls[0][1]
    assert headers["User-Agent"] == alerts.USER_AGENT and not headers["User-Agent"].startswith("Python-urllib")
    assert headers["Accept"] == "application/json"

    def refused(url, headers, payload):
        return 403, "error code: 1010"

    record = Alerter(CONFIGURED, post=refused, background=False).send(_alert())
    assert record.status == alerts.FAILED and record.error == "HTTP 403: error code: 1010"
    long = Alerter(CONFIGURED, post=lambda *a: (422, "x" * 1000), background=False).send(_alert())
    assert len(long.error) <= len("HTTP 422: ") + alerts.ERROR_BODY_CHARS


def test_off_sends_and_records_nothing():
    post = FakePost()
    alerter = Alerter(AlertConfig(enabled=False, api_key="k", sender="a@b", recipients=("c@d",)), post=post,
                      background=False)
    assert alerter.send(_alert()) is None and alerter.records == [] and post.calls == []


def test_config_from_env_file_and_process_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("RESEND_API_KEY=re_file\nRAQEEB_ALERT_FROM=Raqeeb <a@example.com>\n"
                   "RAQEEB_ALERT_TO=x@example.com, y@example.com\n", encoding="utf-8")
    monkeypatch.delenv("RAQEEB_ALERTS", raising=False)
    cfg = AlertConfig.from_env(env)
    assert cfg.enabled and cfg.configured and cfg.recipients == ("x@example.com", "y@example.com")
    monkeypatch.setenv("RAQEEB_ALERTS", "0")
    monkeypatch.setenv("RAQEEB_ALERT_TO", "z@example.com")
    cfg = AlertConfig.from_env(env)
    assert not cfg.enabled and cfg.recipients == ("z@example.com",)  # process env wins
    assert not AlertConfig.from_env(tmp_path / "missing.env").configured


def test_background_delivery_does_not_block():
    import threading
    gate = threading.Event()

    def slow_post(url, headers, payload):
        gate.wait(5)
        return 200

    alerter = Alerter(CONFIGURED, post=slow_post)
    record = alerter.send(_alert())
    assert record.status == "sending" and alerter.drain() == []
    gate.set()
    for _ in range(100):
        if alerter.drain():
            break
        threading.Event().wait(0.02)
    assert record.status == alerts.SENT


def test_preview_writes_the_payload_without_sending(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "ENV_FILE", tmp_path / "none.env")
    out = tmp_path / "preview.json"
    assert alerts.main(["--preview", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data) == {"from", "to", "subject", "text"} and "scn_0007" in data["subject"]
    assert "Diagnosis so far" in data["text"] and "Open the console" in data["text"]
    assert alerts.main(["--preview", str(out), "--send"]) == 1  # unconfigured: refuses, nothing posted


def test_escalation_alert_body():
    a = escalation_alert("pru_0001", NOW, "unexplained", ["INV-03", "INV-04"], "no cause fits the symptoms")
    assert a.kind == alerts.ESCALATION and "escalation" in a.subject
    assert "escalation: unexplained (INV-03, INV-04)" in a.body() and "no cause fits" in a.body()


def test_live_run_alerts_once_when_a_gated_action_waits(tmp_path):
    post = FakePost()
    run = source.start_live("scn_0007", Mode.RULES, db_path=tmp_path / "run.db",
                            alerter=Alerter(CONFIGURED, post=post, background=False))
    run.run_to_end()  # stops at the first gated action
    waiting = run.waiting
    assert waiting
    approval_alerts = [r for r in run.alerts if r.alert.kind == alerts.APPROVAL]
    assert [r.alert.ref for r in approval_alerts] == [a.request.id for a in waiting]
    assert all(r.status == alerts.SENT for r in approval_alerts) and len(post.calls) == len(waiting)
    body = post.calls[0][2]["text"]
    assert run.incident.id in body and waiting[0].request.rationale in body
    assert "started 2025-" in body and "Diagnosis so far: " in body and "@" in body.split("Diagnosis so far: ")[1]
    audit = run.store.audit(step="alert")
    assert list(audit["payload"].map(lambda p: p["status"])) == ["sent"] * len(waiting)
    run.decide(waiting[0].request.id, approve=True)
    run.run_to_end(approve=True)
    assert len(post.calls) == len({r.alert.ref for r in run.alerts})  # one email per request, no repeats
    run.close()


def test_console_banner_when_email_is_not_configured(tmp_path):
    at = AppTest.from_file(str(APP), default_timeout=120)
    run = source.start_live("scn_0007", Mode.RULES, db_path=tmp_path / "run.db",
                            alerter=Alerter(AlertConfig(enabled=True), post=FakePost(), background=False))
    run.run_to_end()
    at.session_state.run = run
    at.session_state.mode_choice = live.MODE_LABEL[Mode.RULES]
    at.run()
    infos = " ".join(str(i.value) for i in at.info)
    assert alerts.BANNER_NOT_CONFIGURED in infos and "RESEND_API_KEY" in infos
    assert not at.exception
    run.close()
