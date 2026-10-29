from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from raqeeb.contracts import TABLE_DDL, Command, InverterStatus
from raqeeb.telemetry.store import TelemetryStore
from raqeeb.twin.controls import flag_sensor
from raqeeb.twin.simulator import Twin

START = datetime(2025, 1, 15, 6, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=1)


@pytest.fixture(scope="module")
def batch():
    twin = Twin(START, END, seed=1)
    first = twin.advance(20)
    twin.state.inverters["INV-12"].comms_up = False
    flag_sensor(twin, "WS-01")
    first.extend(twin.advance(20))
    return first


@pytest.fixture
def store(batch):
    with TelemetryStore() as s:
        s.write_batch(batch)
        yield s


def test_all_tables_created(store):
    names = {r[0] for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(TABLE_DDL) <= names


def test_write_counts_match_batch(batch):
    with TelemetryStore() as s:
        counts = s.write_batch(batch)
    assert counts == {name: len(rows) for name, rows in batch.tables().items()}


def test_rewrite_is_idempotent_for_keyed_tables(store, batch):
    store.write_batch(batch.model_copy(update={"events": [], "commands": []}))
    assert len(store.meter()) == len(batch.meter)
    assert len(store.inverter_readings()) == len(batch.inverter_readings)


def test_round_trip_values_and_utc_index(store, batch):
    df = store.inverter_readings()
    assert str(df.index.tz) == "UTC"
    first = batch.inverter_readings[0]
    row = df.iloc[0]
    assert df.index[0].to_pydatetime() == first.ts
    assert row["inverter_id"] == first.inverter_id
    assert row["ac_kw"] == pytest.approx(first.ac_kw)
    assert row["status"] == InverterStatus(first.status).value
    assert row["tracker_angle_deg"] == pytest.approx(first.tracker_angle_deg)


def test_stored_timestamp_format(store):
    (ts,) = store._conn.execute("SELECT ts FROM meter ORDER BY ts LIMIT 1").fetchone()
    assert ts == "2025-01-15T06:00:00Z"


def test_window_is_half_open(store):
    a, b = START + timedelta(minutes=10), START + timedelta(minutes=25)
    df = store.meter(a, b)
    assert list(df.index) == list(pd.date_range(a, b, freq="5min", inclusive="left"))


def test_dropout_rows_are_missing_not_zero(store):
    wide = store.inverter_wide("ac_kw")
    gap = wide.index >= START + timedelta(minutes=20)
    assert wide.loc[gap, "INV-12"].isna().all()
    assert wide.loc[~gap, "INV-12"].notna().all()
    assert (wide.drop(columns="INV-12") > 0).all().all()


def test_events_commands_and_filters(store):
    events = store.events(code="COMM_LOSS")
    assert list(events["device_id"]) == ["INV-12"]
    commands = store.commands(device_id="WS-01")
    assert list(commands["action"]) == ["flag_sensor_use_reference"]
    assert commands["params"].iloc[0] == {"flagged": True}
    assert store.events(device_id="INV-01").empty
    with pytest.raises(ValueError):
        store.read("meter", plant_ac_kw="1")


def test_audit_round_trip(store):
    ts = START + timedelta(minutes=45)
    store.write_audit("execute", "agent", {"action": "reset_comms_gateway", "target": "INV-12"}, ts)
    store.write_audit("verify", "agent", {"recovered": True}, ts)
    df = store.audit()
    assert list(df["step"]) == ["execute", "verify"]
    assert df["payload"].iloc[1] == {"recovered": True}
    assert list(store.audit(step="verify")["actor"]) == ["agent"]


def test_empty_reads_keep_columns(store):
    df = store.grid_setpoint()
    assert df.empty and list(df.columns) == ["limit_kw"] and str(df.index.tz) == "UTC"


def test_span_and_file_store(tmp_path, batch):
    path = tmp_path / "sub" / "raqeeb.db"
    with TelemetryStore(path) as s:
        assert s.span() is None
        s.write_batch(batch)
        s.write_rows("commands", [Command(ts=START, source="operator", device_id="INV-01", action="x")])
    with TelemetryStore(path) as s:
        assert s.span() == (START, END - timedelta(minutes=25))
        assert len(s.commands()) == 2
