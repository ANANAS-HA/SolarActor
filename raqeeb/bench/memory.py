"""In-memory telemetry for the bench until Lane C's TelemetryStore is merged.

    telemetry = MemoryTelemetry()
    telemetry.write_batch(inj.advance(15))
    df = telemetry.read("events", start, end, device_id="INV-07")

Same read shape as telemetry/store.py: a DataFrame indexed by `ts` (UTC) for [start, end),
model fields as columns (plus `id` for events and commands), rows in time order, optional
equality filters on device-like columns. Missing rows stay missing.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from raqeeb.contracts import TABLE_MODELS, TelemetryBatch

_WITH_ID = {"events", "commands", "audit"}
_FILTERABLE = {"inverter_id", "device_id", "code", "severity", "source", "action", "step", "actor", "status"}


class MemoryTelemetry:
    def __init__(self) -> None:
        self._rows: dict[str, list[dict]] = {table: [] for table in TABLE_MODELS}

    def write_batch(self, batch: TelemetryBatch) -> dict[str, int]:
        written = {}
        for table, records in batch.tables().items():
            rows = self._rows[table]
            for record in records:
                row = {**record.model_dump(mode="json"), "ts": record.ts}  # enums as plain strings
                if table in _WITH_ID:
                    row = {"id": len(rows) + 1, **row}
                rows.append(row)
            written[table] = len(records)
        return written

    def read(self, table: str, start: datetime | None = None, end: datetime | None = None,
             **filters: str | None) -> pd.DataFrame:
        if table not in TABLE_MODELS:
            raise ValueError(f"unknown table {table!r}")
        fields = list(TABLE_MODELS[table].model_fields)
        columns = (["id"] if table in _WITH_ID else []) + fields
        for column in filters:
            if column not in _FILTERABLE or column not in fields:
                raise ValueError(f"cannot filter {table} on {column!r}")
        rows = [
            row for row in self._rows[table]
            if (start is None or row["ts"] >= start) and (end is None or row["ts"] < end)
            and all(value is None or row[column] == value for column, value in filters.items())
        ]
        df = pd.DataFrame(rows, columns=columns)
        df = df.iloc[pd.Index(df["ts"]).argsort(kind="stable")] if len(df) else df
        index = pd.DatetimeIndex(df.pop("ts"), name="ts")
        df.index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
        return df
