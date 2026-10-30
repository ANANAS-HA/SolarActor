"""Shared types for every Raqeeb module (HANDOFF section 6).

This file is the single source of truth. Changing a type here means updating
every user of it and its tests in the same commit.

Conventions:
- All timestamps are timezone-aware UTC. SQLite stores them as ISO-8601 TEXT
  ending in "Z" (e.g. "2025-04-12T05:00:00Z"). Display in Asia/Riyadh.
- Telemetry is on a 5-minute grid (STEP_MINUTES).
- A comms dropout means rows are MISSING, never zeros.
- A root cause is labelled "FAULT_TYPE@TARGET", e.g. "INVERTER_DERATE@INV-07".
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Protocol

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    import pandas as pd

STEP_MINUTES = 5
DISPLAY_TZ = "Asia/Riyadh"
PLANT_TARGET = "ALL"  # fault target meaning "every inverter"


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"timestamp must be timezone-aware UTC, got {value!r}")
    return value.astimezone(timezone.utc)


UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]


def to_db_ts(value: datetime) -> str:
    """Format a UTC datetime the way every SQLite table stores it."""
    return _require_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_db_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FaultType(StrEnum):
    SOILING = "SOILING"
    SENSOR_SOILING = "SENSOR_SOILING"
    INVERTER_DERATE = "INVERTER_DERATE"
    COMMS_DROPOUT = "COMMS_DROPOUT"
    CURTAILMENT = "CURTAILMENT"  # decoy: expected behaviour, never a root cause
    TRACKER_STUCK = "TRACKER_STUCK"  # full scope
    STRING_OPEN = "STRING_OPEN"  # full scope
    UNAUTHORIZED_CMD = "UNAUTHORIZED_CMD"  # full scope
    PROMPT_INJECTION = "PROMPT_INJECTION"  # full scope, decoy: a log line whose text tells the agent what to do
    # Unknown faults: deliberately absent from the agent's causal graph and planner. They are described only
    # in alarm manual text (configs/alarm_codes.yaml) and in technician notes.
    TRANSFORMER_TRIP = "TRANSFORMER_TRIP"  # the inverters on one transformer drop to zero together
    NEW_SHADING = "NEW_SHADING"  # new construction shades one edge of the field at low sun
    FIRMWARE_ALARM = "FIRMWARE_ALARM"  # after a firmware update one inverter raises a new code, derates slightly


CORE_FAULTS = frozenset(
    {
        FaultType.SOILING,
        FaultType.SENSOR_SOILING,
        FaultType.INVERTER_DERATE,
        FaultType.COMMS_DROPOUT,
        FaultType.CURTAILMENT,
    }
)
UNKNOWN_FAULTS = frozenset({FaultType.TRANSFORMER_TRIP, FaultType.NEW_SHADING, FaultType.FIRMWARE_ALARM})
FULL_SCOPE_FAULTS = frozenset(FaultType) - CORE_FAULTS - UNKNOWN_FAULTS
# Planted, but never a root cause and never a reason to act. Curtailment is expected behaviour; an
# injected instruction is untrusted text with no physical effect (doing what it says is an unsafe action).
DECOY_FAULTS = frozenset({FaultType.CURTAILMENT, FaultType.PROMPT_INJECTION})


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class ActionType(StrEnum):
    """Action allowlist (HANDOFF section 9). Anything else is rejected.

    Changing the grid setpoint is deliberately absent. Curtailment gets no
    action: the agent marks it as expected.
    """

    RESET_COMMS_GATEWAY = "reset_comms_gateway"
    FLAG_SENSOR_USE_REFERENCE = "flag_sensor_use_reference"
    CREATE_CLEANING_WORK_ORDER = "create_cleaning_work_order"
    RESUME_TRACKING = "resume_tracking"  # full scope
    DISPATCH_TECHNICIAN = "dispatch_technician"  # full scope
    SET_POWER_LIMIT = "set_power_limit"  # needs approval
    ISOLATE_DEVICE = "isolate_device"  # needs approval, not reversible


class InverterStatus(StrEnum):
    RUNNING = "RUNNING"
    DERATED = "DERATED"
    CURTAILED = "CURTAILED"
    STANDBY = "STANDBY"  # night / no irradiance
    STOPPED = "STOPPED"  # stopped by command or isolation
    FAULT = "FAULT"


class SymptomKind(StrEnum):
    """Node types for the causal graph in agent/rank.py."""

    PLANT_UNDERPERFORMANCE = "PLANT_UNDERPERFORMANCE"  # plant below trusted expected
    DEVICE_UNDERPERFORMANCE = "DEVICE_UNDERPERFORMANCE"  # one device below its peers
    FLAT_CAP_DEVICE = "FLAT_CAP_DEVICE"
    FLAT_CAP_PLANT = "FLAT_CAP_PLANT"
    HIGH_TEMPERATURE = "HIGH_TEMPERATURE"
    DATA_GAP = "DATA_GAP"
    SENSOR_DEVIATION = "SENSOR_DEVIATION"  # on-site sensor vs clear-sky / reference
    HIGH_WIND = "HIGH_WIND"
    TRACKER_ANGLE_FLAT = "TRACKER_ANGLE_FLAT"
    DC_STEP_LOSS = "DC_STEP_LOSS"
    UNEXPECTED_COMMAND = "UNEXPECTED_COMMAND"
    DEVICE_LOG = "DEVICE_LOG"  # a free-text log line from a device: recorded by code and device, text never read
    ALARM = "ALARM"  # a raw alarm not yet folded into another symptom


def cause_label(fault_type: FaultType | str, target: str) -> str:
    return f"{FaultType(fault_type).value}@{target}"


_CAUSE_RE = re.compile(r"^(?P<type>[A-Z_]+)@(?P<target>[A-Z0-9-]+)$")


def parse_cause_label(label: str) -> tuple[FaultType, str]:
    match = _CAUSE_RE.match(label)
    if not match:
        raise ValueError(f"root cause must look like 'FAULT_TYPE@TARGET', got {label!r}")
    return FaultType(match["type"]), match["target"]


# ---------------------------------------------------------------------------
# Telemetry rows (one model per SQLite table)
# ---------------------------------------------------------------------------


class InverterReading(_Model):
    ts: UtcDatetime
    inverter_id: str
    ac_kw: float
    dc_kw: float
    temp_c: float  # inverter internal temperature (not module/cell temperature)
    status: InverterStatus
    tracker_angle_deg: float | None = None  # measured rotation of the tracker block; 0 = flat


class WeatherStationReading(_Model):
    """On-site sensor WS-01. It can be dusty, so it is not trusted."""

    ts: UtcDatetime
    ghi_sensor_wm2: float
    ambient_c: float
    wind_ms: float


class WeatherReference(_Model):
    """NASA POWER + pvlib clear-sky. Trusted, but not exact: the twin's true irradiance
    differs from it by a few percent (see ReferenceErrorConfig)."""

    ts: UtcDatetime
    ghi_wm2: float
    dni_wm2: float
    dhi_wm2: float
    ambient_c: float
    wind_ms: float
    clearsky_ghi_wm2: float


class MeterReading(_Model):
    ts: UtcDatetime
    plant_ac_kw: float


class GridSetpoint(_Model):
    """Written only while the grid operator's limit is active (its presence is the evidence)."""

    ts: UtcDatetime
    limit_kw: float


class Event(_Model):
    ts: UtcDatetime
    device_id: str
    code: str  # key in configs/alarm_codes.yaml
    severity: Severity
    message: str


class Command(_Model):
    ts: UtcDatetime
    source: str  # e.g. "raqeeb", "operator", "scada", or an unexpected source
    device_id: str
    action: str  # free text: unauthorized commands may be outside the allowlist
    params: dict[str, Any] = Field(default_factory=dict)


class TechnicianNote(_Model):
    """A free-text note from site staff. Untrusted data: the agent's rules never read the text."""

    ts: UtcDatetime
    area_or_device: str  # what the note is about, as staff write it ("INV-07", "T2", "west fence", ...)
    author: str
    text: str


class AuditEntry(_Model):
    ts: UtcDatetime
    step: str  # e.g. "plan", "approval", "execute", "verify"
    actor: str  # "agent", "human:<name>", "system"
    payload: dict[str, Any] = Field(default_factory=dict)  # stored as payload_json


TABLE_DDL: dict[str, str] = {
    "inverter_readings": """
        CREATE TABLE IF NOT EXISTS inverter_readings (
            ts TEXT NOT NULL,
            inverter_id TEXT NOT NULL,
            ac_kw REAL NOT NULL,
            dc_kw REAL NOT NULL,
            temp_c REAL NOT NULL,
            status TEXT NOT NULL,
            tracker_angle_deg REAL,
            PRIMARY KEY (ts, inverter_id)
        )""",
    "weather_station": """
        CREATE TABLE IF NOT EXISTS weather_station (
            ts TEXT PRIMARY KEY,
            ghi_sensor_wm2 REAL NOT NULL,
            ambient_c REAL NOT NULL,
            wind_ms REAL NOT NULL
        )""",
    "weather_reference": """
        CREATE TABLE IF NOT EXISTS weather_reference (
            ts TEXT PRIMARY KEY,
            ghi_wm2 REAL NOT NULL,
            dni_wm2 REAL NOT NULL,
            dhi_wm2 REAL NOT NULL,
            ambient_c REAL NOT NULL,
            wind_ms REAL NOT NULL,
            clearsky_ghi_wm2 REAL NOT NULL
        )""",
    "meter": """
        CREATE TABLE IF NOT EXISTS meter (
            ts TEXT PRIMARY KEY,
            plant_ac_kw REAL NOT NULL
        )""",
    "grid_setpoint": """
        CREATE TABLE IF NOT EXISTS grid_setpoint (
            ts TEXT PRIMARY KEY,
            limit_kw REAL NOT NULL
        )""",
    "events": """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            device_id TEXT NOT NULL,
            code TEXT NOT NULL,
            severity TEXT NOT NULL,
            message TEXT NOT NULL
        )""",
    "commands": """
        CREATE TABLE IF NOT EXISTS commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            source TEXT NOT NULL,
            device_id TEXT NOT NULL,
            action TEXT NOT NULL,
            params TEXT NOT NULL DEFAULT '{}'
        )""",
    "technician_notes": """
        CREATE TABLE IF NOT EXISTS technician_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            area_or_device TEXT NOT NULL,
            author TEXT NOT NULL,
            text TEXT NOT NULL
        )""",
    "audit": """
        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            step TEXT NOT NULL,
            actor TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )""",
}

TABLE_MODELS: dict[str, type[BaseModel]] = {
    "inverter_readings": InverterReading,
    "weather_station": WeatherStationReading,
    "weather_reference": WeatherReference,
    "meter": MeterReading,
    "grid_setpoint": GridSetpoint,
    "events": Event,
    "commands": Command,
    "technician_notes": TechnicianNote,
    "audit": AuditEntry,
}


class TelemetryBatch(_Model):
    """What the twin emits for one or more steps. Field names = table names."""

    inverter_readings: list[InverterReading] = Field(default_factory=list)
    weather_station: list[WeatherStationReading] = Field(default_factory=list)
    weather_reference: list[WeatherReference] = Field(default_factory=list)
    meter: list[MeterReading] = Field(default_factory=list)
    grid_setpoint: list[GridSetpoint] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    commands: list[Command] = Field(default_factory=list)
    technician_notes: list[TechnicianNote] = Field(default_factory=list)

    def extend(self, other: TelemetryBatch) -> None:
        for name in type(self).model_fields:
            getattr(self, name).extend(getattr(other, name))

    def tables(self) -> dict[str, list[BaseModel]]:
        return {name: getattr(self, name) for name in type(self).model_fields}


# ---------------------------------------------------------------------------
# Twin state (HANDOFF section 7). Faults and controls change ONLY this; the
# simulator computes readings from weather x state.
# ---------------------------------------------------------------------------


class InverterState(_Model):
    soiling_loss: float = Field(default=0.0, ge=0.0, le=1.0)  # fraction of irradiance lost on panels
    string_loss: float = Field(default=0.0, ge=0.0, le=1.0)  # fraction of DC lost (open strings)
    derate_cap_kw: float | None = Field(default=None, ge=0.0)  # thermal derate cap (fault)
    temp_offset_c: float = 0.0  # extra internal temperature (e.g. failed fan)
    power_limit_kw: float | None = Field(default=None, ge=0.0)  # operator/agent limit (control)
    comms_up: bool = True
    tracker_override_deg: float | None = None  # stuck tracker angle; None = tracking normally
    stopped: bool = False  # stopped by remote command
    isolated: bool = False  # disconnected (irreversible in the twin)
    grid_lost: bool = False  # its transformer tripped: no AC grid, output zero, status FAULT, no alarm of its own
    shading_loss: float = Field(default=0.0, ge=0.0, le=1.0)  # fraction of irradiance lost to new shading
    firmware_cap_kw: float | None = Field(default=None, ge=0.0)  # limit from a bad firmware parameter (no heat)


class WeatherStationState(_Model):
    sensor_soiling: float = Field(default=0.0, ge=0.0, le=1.0)  # fraction the GHI sensor under-reads
    flagged: bool = False  # flagged suspect: tools use the reference instead
    comms_up: bool = True


class AtmosphereState(_Model):
    """Synthetic weather modifiers (dust storm). Apply to the true weather and the reference."""

    irradiance_factor: float = Field(default=1.0, ge=0.0, le=1.0)
    wind_add_ms: float = Field(default=0.0, ge=0.0)


class PlantState(_Model):
    inverters: dict[str, InverterState]
    station: WeatherStationState = Field(default_factory=WeatherStationState)
    atmosphere: AtmosphereState = Field(default_factory=AtmosphereState)
    grid_limit_kw: float | None = Field(default=None, ge=0.0)  # curtailment from the grid operator

    @classmethod
    def healthy(cls, inverter_ids: list[str]) -> PlantState:
        return cls(inverters={i: InverterState() for i in inverter_ids})


# ---------------------------------------------------------------------------
# Scenarios (scenarios/<id>.yaml)
# ---------------------------------------------------------------------------

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class FaultSpec(_Model):
    """One planted fault.

    `start` / `end` are either "HH:MM" (UTC clock time on the scenario's start
    date) or a full ISO-8601 UTC timestamp. Quote "HH:MM" in YAML, otherwise
    YAML 1.1 reads 07:00 as the integer 420.
    """

    type: FaultType
    target: str
    start: str
    end: str | None = None
    severity: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("start", "end", mode="before")
    @classmethod
    def _check_time(cls, value: Any) -> Any:
        if value is None:
            return value
        if isinstance(value, int):
            raise ValueError("time was read as a number; quote it in YAML, e.g. start: \"07:00\"")
        if isinstance(value, datetime):
            return to_db_ts(value)
        if not isinstance(value, str):
            raise ValueError(f"time must be a string, got {value!r}")
        if not _HHMM_RE.match(value):
            _require_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        return value

    @property
    def label(self) -> str:
        return cause_label(self.type, self.target)


class AnswerKey(_Model):
    """Hidden from the agent. Only bench/score.py reads it."""

    root_causes: list[str]

    @field_validator("root_causes")
    @classmethod
    def _check_labels(cls, value: list[str]) -> list[str]:
        for label in value:
            parse_cause_label(label)
        return value


class Scenario(_Model):
    id: str
    seed: int
    start: UtcDatetime
    duration_hours: float = Field(gt=0)
    faults: list[FaultSpec]
    answer_key: AnswerKey | None = None

    @model_validator(mode="after")
    def _check(self) -> Scenario:
        for fault in self.faults:
            start = self.resolve_time(fault.start)
            if not self.start <= start <= self.end:
                raise ValueError(f"{fault.label} starts at {start}, outside the scenario window")
            if fault.end is not None and self.resolve_time(fault.end) < start:
                raise ValueError(f"{fault.label} ends before it starts")
        if self.answer_key is not None:
            planted = {f.label for f in self.faults}
            unknown = set(self.answer_key.root_causes) - planted
            if unknown:
                raise ValueError(f"answer key names faults that are not planted: {sorted(unknown)}")
        return self

    @property
    def end(self) -> datetime:
        return self.start + timedelta(hours=self.duration_hours)

    def resolve_time(self, value: str) -> datetime:
        """Turn a FaultSpec time into an absolute UTC datetime."""
        if _HHMM_RE.match(value):
            hh, mm = (int(part) for part in value.split(":"))
            day: date = self.start.date()
            return datetime.combine(day, time(hh, mm), tzinfo=timezone.utc)
        return _require_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))

    def expected_answer_key(self) -> AnswerKey:
        """Every planted fault except decoys (curtailment, injected instructions) is a root cause."""
        return AnswerKey(root_causes=[f.label for f in self.faults if f.type not in DECOY_FAULTS])

    def agent_view(self) -> AgentScenario:
        """The only part of a scenario the agent may see: no faults, no seed, no answer key."""
        return AgentScenario(id=self.id, start=self.start, duration_hours=self.duration_hours)

    @classmethod
    def from_yaml(cls, path: Path) -> Scenario:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    def to_yaml(self, path: Path, header: str = "") -> None:
        """Write the scenario; "HH:MM" times are always quoted. `header` is prepended (e.g. comments)."""
        data = self.model_dump(mode="json", exclude_none=True)
        text = yaml.dump(data, Dumper=_ScenarioDumper, sort_keys=False, allow_unicode=True)
        path.write_text(header + text, encoding="utf-8", newline="\n")


class AgentScenario(_Model):
    """What the agent is told about an incident window. Everything else comes from telemetry."""

    id: str
    start: UtcDatetime
    duration_hours: float = Field(gt=0)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(hours=self.duration_hours)


class _ScenarioDumper(yaml.SafeDumper):
    """Quotes "HH:MM" strings so no YAML 1.1 reader takes them for numbers."""


_ScenarioDumper.add_representer(
    str, lambda dumper, value: dumper.represent_scalar(
        "tag:yaml.org,2002:str", value, style="'" if _HHMM_RE.match(value) else None)
)


# ---------------------------------------------------------------------------
# Agent models
# ---------------------------------------------------------------------------

VERDICT_OK = "ok"
VERDICT_INCONCLUSIVE = "inconclusive"


class ToolResult(_Model):
    """What every agent-facing tool in raqeeb/tools returns: one shape for the agent and the LLM.

    Code computes `values`; `evidence` is a short plain-text summary built from them. `values`
    must stay JSON-serialisable (it goes into Symptom.evidence, the audit log and prompts).
    """

    tool: str  # function name, e.g. "sensor_check"
    verdict: str  # VERDICT_OK, VERDICT_INCONCLUSIVE, or a short finding code, e.g. "sensor_low"
    devices: list[str] = Field(default_factory=list)  # what a finding is about; PLANT_TARGET = plant-wide
    start: UtcDatetime | None = None  # window the tool looked at, [start, end)
    end: UtcDatetime | None = None
    values: dict[str, Any] = Field(default_factory=dict)
    thresholds: dict[str, float] = Field(default_factory=dict)  # the limits the verdict used
    evidence: str = ""

    @property
    def finding(self) -> bool:
        return self.verdict not in (VERDICT_OK, VERDICT_INCONCLUSIVE)


class Symptom(_Model):
    id: str
    kind: SymptomKind
    device_id: str
    start: UtcDatetime
    evidence: dict[str, Any] = Field(default_factory=dict)  # numbers computed by code


class Hypothesis(_Model):
    id: str
    fault_type: FaultType
    targets: list[str]
    explains: list[str] = Field(default_factory=list)  # Symptom ids
    evidence: list[str] = Field(default_factory=list)
    score: float = 0.0

    @property
    def labels(self) -> list[str]:
        return [cause_label(self.fault_type, t) for t in self.targets]


class Diagnosis(_Model):
    root_causes: list[Hypothesis]
    unexplained: list[str] = Field(default_factory=list)  # Symptom ids
    confidence: float = Field(ge=0.0, le=1.0)


class ActionRequest(_Model):
    id: str  # referenced by Verification.action_id
    action: ActionType
    target: str
    params: dict[str, Any] = Field(default_factory=dict)
    reversible: bool
    needs_approval: bool
    rationale: str
    expected_effect: str


class Verification(_Model):
    action_id: str
    recovered: bool
    before_kw: float
    after_kw: float
    expected_kw: float
    note: str = ""


class IncidentReport(_Model):
    summary_en: str
    summary_ar: str
    causes: list[Hypothesis]
    actions: list[ActionRequest]
    verifications: list[Verification]
    cost_usd: float = 0.0
    llm_calls: int = 0


# ---------------------------------------------------------------------------
# Agent interface: how an environment (bench harness or console) drives an agent.
#
#   agent.start(scenario.agent_view(), telemetry)
#   every N sim-minutes: write new telemetry, then turn = agent.step(now, outcomes)
#   the environment gates turn.actions with configs/actions.yaml (not the agent's
#   needs_approval flag), asks for approval, runs them with twin.controls.apply_action,
#   and passes the ActionOutcomes to the next step. The last call has final=True.
# ---------------------------------------------------------------------------


class LlmRole(StrEnum):
    LOCAL = "local"
    API = "api"


class LlmCall(_Model):
    """One LLM call, logged by llm/gateway.py (HANDOFF section 10)."""

    role: LlmRole
    model: str
    purpose: str  # e.g. "hypotheses", "plan_order", "report"
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    latency_s: float = Field(default=0.0, ge=0.0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    ok: bool = True  # output parsed into its Pydantic model
    escalation: str = ""  # why the call went to the api model; "" if routed there by rule


class ActionStatus(StrEnum):
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"  # approval refused
    FAILED = "FAILED"  # not allowlisted, bad target or bad params


class ActionOutcome(_Model):
    """What the environment did with one ActionRequest."""

    action_id: str
    status: ActionStatus
    ts: UtcDatetime  # sim time the environment handled it
    note: str = ""


class Escalation(_Model):
    """Something the agent hands to a human instead of acting: a cause it cannot fix (fixes used up,
    or none fits) or symptoms it cannot explain. `devices` are the plant devices concerned."""

    ts: UtcDatetime
    subject: str  # a cause label, or "unexplained"
    devices: list[str] = Field(default_factory=list)
    reason: str


class AgentTurn(_Model):
    """The agent's answer to one poll."""

    diagnosis: Diagnosis | None = None  # current best diagnosis; None = unchanged
    actions: list[ActionRequest] = Field(default_factory=list)  # new requests this turn
    escalations: list[Escalation] = Field(default_factory=list)  # new hand-overs to a human this turn
    verifications: list[Verification] = Field(default_factory=list)  # new verifications this turn
    llm_calls: list[LlmCall] = Field(default_factory=list)  # calls made during this turn
    report: IncidentReport | None = None
    done: bool = False  # nothing left to do (the environment may still poll)


class TelemetryReader(Protocol):
    """Read access to the telemetry tables (telemetry/store.py TelemetryStore fits).

    Returns the rows of `table` with start <= ts < end as a DataFrame indexed by `ts` (UTC),
    optionally filtered on device-like columns (inverter_id, device_id, code, ...).
    """

    def read(self, table: str, start: datetime | None = None, end: datetime | None = None,
             **filters: str | None) -> pd.DataFrame: ...


class Agent(Protocol):
    name: str

    def start(self, view: AgentScenario, telemetry: TelemetryReader) -> None: ...

    def step(self, now: datetime, outcomes: list[ActionOutcome], final: bool = False) -> AgentTurn: ...


# ---------------------------------------------------------------------------
# Config models (configs/*.yaml)
# ---------------------------------------------------------------------------


class ActionSpec(_Model):
    reversible: bool
    auto: bool
    scope: str = Field(pattern="^(core|full)$")
    description: str

    @property
    def needs_approval(self) -> bool:
        return not self.auto


class AlarmCode(_Model):
    vendor_id: str
    severity: Severity
    title: str
    related_faults: list[FaultType] = Field(default_factory=list)  # empty: normal/protective (INFO) or unknown
    manual: str

    @property
    def protective(self) -> bool:
        """Normal or protective behaviour (wind stow, a log line): no related fault and only INFO.
        A WARNING with no related fault is a code the planner has no fix for, so it stays a symptom."""
        return not self.related_faults and self.severity == Severity.INFO


class SiteConfig(_Model):
    name: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    altitude_m: float
    timezone: str = DISPLAY_TZ


class InverterFleetConfig(_Model):
    count: int = Field(gt=0)
    id_format: str
    ac_kw: float = Field(gt=0)
    dc_ac_ratio: float = Field(gt=0)
    nominal_efficiency: float = Field(gt=0, le=1)
    variation_pct: float = Field(ge=0)
    variation_seed: int
    derate_temp_c: float
    temp_rise_idle_c: float  # internal temp = ambient + idle + full_load * load fraction + offset
    temp_rise_full_load_c: float
    layout_rows: int = Field(gt=0)
    layout_cols: int = Field(gt=0)

    @model_validator(mode="after")
    def _check_layout(self) -> InverterFleetConfig:
        if self.layout_rows * self.layout_cols != self.count:
            raise ValueError("layout_rows * layout_cols must equal count")
        return self

    @property
    def ids(self) -> list[str]:
        return [self.id_format.format(i) for i in range(1, self.count + 1)]

    @property
    def dc_kw(self) -> float:
        return self.ac_kw * self.dc_ac_ratio


class TrackerConfig(_Model):
    axis_tilt_deg: float
    axis_azimuth_deg: float
    max_angle_deg: float
    gcr: float = Field(gt=0, lt=1)
    backtrack: bool
    stow_wind_ms: float
    stow_angle_deg: float


class ModuleConfig(_Model):
    gamma_pdc_per_c: float
    sapm_temp_model: str
    dc_losses_pct: float = Field(ge=0, lt=100)
    albedo: float = Field(ge=0, le=1)


class MeasurementNoiseConfig(_Model):
    """Relative (fraction) standard deviation of measurement noise on readings."""

    inverter_power: float = Field(ge=0)
    ghi_sensor: float = Field(ge=0)
    ambient_c_abs: float = Field(ge=0)  # absolute, degrees C


class ReferenceErrorConfig(_Model):
    """How the twin's true irradiance differs from the reference (satellite vs on-site).

    true = reference * (1 + bias + e(t)), with bias ~ N(0, bias_sigma) per twin and e(t) an
    AR(1) process with standard deviation `sigma` and correlation time `tau_min`.
    """

    bias_sigma: float = Field(ge=0)
    sigma: float = Field(ge=0)
    tau_min: float = Field(gt=0)
    max_abs: float = Field(gt=0, lt=1)  # clip |bias + e(t)|


class ControlsConfig(_Model):
    """Simulated-time delays before a control takes effect."""

    comms_reset_delay_min: int = Field(ge=0)
    cleaning_delay_min: int = Field(ge=0)
    technician_delay_min: int = Field(ge=0)


class LayoutConfig(_Model):
    """Synthetic site layout: which inverters sit behind each transformer and along each field edge."""

    transformers: dict[str, list[str]] = Field(default_factory=dict)  # "T1" -> inverters; site staff call them groups
    groups: dict[str, str] = Field(default_factory=dict)  # site name of a transformer's group, "T1" -> "A"
    edges: dict[str, list[str]] = Field(default_factory=dict)  # "EDGE-W" -> inverters along the west fence

    def area_of(self, target: str) -> list[str]:
        return self.transformers.get(target) or self.edges.get(target) or []


class PlantConfig(_Model):
    site: SiteConfig
    inverters: InverterFleetConfig
    weather_station_id: str
    meter_id: str
    plant_controller_id: str
    high_wind_alarm_ms: float
    tracker: TrackerConfig
    module: ModuleConfig
    noise: MeasurementNoiseConfig
    reference_error: ReferenceErrorConfig
    controls: ControlsConfig
    layout: LayoutConfig = Field(default_factory=lambda: LayoutConfig())

    @property
    def rated_ac_kw(self) -> float:
        return self.inverters.ac_kw * self.inverters.count
