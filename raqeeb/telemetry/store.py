"""SQLite telemetry store (tables from contracts.TABLE_DDL).

    store = TelemetryStore()              # in memory; TelemetryStore(path) or from_settings() for a file
    store.write_batch(twin.advance(60))
    store.write_audit("execute", "agent", {"action": "reset_comms_gateway"}, ts)
    df = store.inverter_readings(start, end)

Timestamps are written with to_db_ts and read back with from_db_ts. Read helpers return a
DataFrame indexed by `ts` (tz-aware UTC) for the half-open window [start, end); either bound
may be None. Missing rows stay missing: nothing here fills gaps.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel

from raqeeb.config import load_settings
from raqeeb.contracts import TABLE_DDL, TABLE_MODELS, AuditEntry, TelemetryBatch, from_db_ts, to_db_ts

MEMORY = ":memory:"
# Tables keyed by ts (+ device): rewriting a step replaces it, so writing a batch twice is harmless.
_KEYED = {"inverter_readings", "weather_station", "weather_reference", "meter", "grid_setpoint"}
# Model field -> column holding it as JSON text
_JSON_COLUMNS = {"commands": {"params": "params"}, "audit": {"payload": "payload_json"}}
_FILTERABLE = {"inverter_id", "device_id", "code", "severity", "source", "action", "step", "actor", "status"}


class TelemetryStore:
    def __init__(self, path: Path | str = MEMORY):
        if path != MEMORY:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        with self._conn:
            for ddl in TABLE_DDL.values():
                self._conn.execute(ddl)

    @classmethod
    def from_settings(cls) -> TelemetryStore:
        """The file store at RAQEEB_DB (.env)."""
        return cls(load_settings().db_path)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> TelemetryStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- writing ---------------------------------------------------------------

    @staticmethod
    def _row(table: str, record: BaseModel) -> dict[str, Any]:
        row = record.model_dump(mode="json")
        row["ts"] = to_db_ts(getattr(record, "ts"))
        for field, column in _JSON_COLUMNS.get(table, {}).items():
            row[column] = json.dumps(row.pop(field), sort_keys=True)
        return row

    def write_rows(self, table: str, records: list[BaseModel]) -> int:
        if table not in TABLE_MODELS:
            raise ValueError(f"unknown table {table!r}")
        if not records:
            return 0
        rows = [self._row(table, r) for r in records]
        columns = list(rows[0])
        verb = "INSERT OR REPLACE" if table in _KEYED else "INSERT"
        sql = f"{verb} INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"
        with self._conn:
            self._conn.executemany(sql, [tuple(row[c] for c in columns) for row in rows])
        return len(rows)

    def write_batch(self, batch: TelemetryBatch) -> dict[str, int]:
        """Write every table of a batch; returns rows written per table."""
        return {table: self.write_rows(table, records) for table, records in batch.tables().items()}

    def write_audit(self, step: str, actor: str, payload: dict[str, Any], ts: datetime) -> AuditEntry:
        entry = AuditEntry(ts=ts, step=step, actor=actor, payload=payload)
        self.write_rows("audit", [entry])
        return entry

    # -- reading ---------------------------------------------------------------

    def read(self, table: str, start: datetime | None = None, end: datetime | None = None,
             **filters: str | None) -> pd.DataFrame:
        """Rows of `table` with start <= ts < end, optionally filtered on device-like columns."""
        if table not in TABLE_MODELS:
            raise ValueError(f"unknown table {table!r}")
        json_cols = _JSON_COLUMNS.get(table, {})
        fields = list(TABLE_MODELS[table].model_fields)
        columns = [json_cols.get(f, f) for f in fields]
        if "id INTEGER PRIMARY KEY" in TABLE_DDL[table]:
            columns.insert(0, "id")
        where, args = [], []
        if start is not None:
            where.append("ts >= ?")
            args.append(to_db_ts(start))
        if end is not None:
            where.append("ts < ?")
            args.append(to_db_ts(end))
        for column, value in filters.items():
            if column not in _FILTERABLE or column not in columns:
                raise ValueError(f"cannot filter {table} on {column!r}")
            if value is not None:
                where.append(f"{column} = ?")
                args.append(value)
        sql = f"SELECT {', '.join(columns)} FROM {table}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts" + (", id" if "id" in columns else "")
        rows = self._conn.execute(sql, args).fetchall()

        df = pd.DataFrame(rows, columns=columns)
        for field, column in json_cols.items():
            df[field] = [json.loads(v) for v in df.pop(column)]
        index = pd.DatetimeIndex([from_db_ts(v) for v in df.pop("ts")], name="ts")
        if index.tz is None:  # empty result
            index = index.tz_localize("UTC")
        df.index = index
        return df

    def inverter_readings(self, start=None, end=None, inverter_id: str | None = None) -> pd.DataFrame:
        return self.read("inverter_readings", start, end, inverter_id=inverter_id)

    def inverter_wide(self, quantity: str, start=None, end=None) -> pd.DataFrame:
        """One column per inverter for `quantity` (ac_kw, dc_kw, temp_c, status, tracker_angle_deg).

        A step a device did not report is NaN in its column (the row was missing).
        """
        df = self.inverter_readings(start, end)
        wide = df.pivot(columns="inverter_id", values=quantity)
        wide.columns.name = "inverter_id"
        return wide

    def weather_station(self, start=None, end=None) -> pd.DataFrame:
        return self.read("weather_station", start, end)

    def weather_reference(self, start=None, end=None) -> pd.DataFrame:
        return self.read("weather_reference", start, end)

    def meter(self, start=None, end=None) -> pd.DataFrame:
        return self.read("meter", start, end)

    def grid_setpoint(self, start=None, end=None) -> pd.DataFrame:
        return self.read("grid_setpoint", start, end)

    def events(self, start=None, end=None, device_id: str | None = None, code: str | None = None) -> pd.DataFrame:
        return self.read("events", start, end, device_id=device_id, code=code)

    def commands(self, start=None, end=None, device_id: str | None = None, action: str | None = None) -> pd.DataFrame:
        return self.read("commands", start, end, device_id=device_id, action=action)

    def audit(self, start=None, end=None, step: str | None = None) -> pd.DataFrame:
        return self.read("audit", start, end, step=step)

    def span(self, table: str = "meter") -> tuple[datetime, datetime] | None:
        """First and last timestamp stored in `table`, or None if it is empty."""
        if table not in TABLE_MODELS:
            raise ValueError(f"unknown table {table!r}")
        first, last = self._conn.execute(f"SELECT MIN(ts), MAX(ts) FROM {table}").fetchone()
        return None if first is None else (from_db_ts(first), from_db_ts(last))
