"""SAMPLE content for the console until the agent (Phase 2) and the bench (Lane E) exist.

Everything here is fake and labelled SAMPLE in the UI. The "agent" is a script that reads the
scenario's planted faults, which the real agent never sees. Its automatic actions do go through
twin.controls.apply_action (see ui/source.py), so the recovery chart shows the twin's real
response. Numbers in the timeline and report are computed from the run's telemetry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from raqeeb.config import load_actions
from raqeeb.contracts import (
    DISPLAY_TZ,
    STEP_MINUTES,
    ActionRequest,
    ActionType,
    AuditEntry,
    FaultSpec,
    FaultType,
    Hypothesis,
    IncidentReport,
    PlantConfig,
    Scenario,
    Verification,
)
from raqeeb.faults.catalogue import STORM_DEFAULT_MIN

SAMPLE_ACTOR = "sample-agent"
ACT_AFTER_MIN = 20  # the script acts this long after a fault starts (or the storm ends)
INVESTIGATE_BEFORE_MIN = 10
VERIFY_WINDOW_MIN = 30
TRIAGE_WINDOW_MIN = 30
RECOVERED_FRACTION = 0.95  # actual / expected-from-reference after an action
DERATE_LIMIT_FRACTION = 0.6  # proposed set_power_limit, fraction of the inverter rating

ACTION_FOR = {
    FaultType.SOILING: ActionType.CREATE_CLEANING_WORK_ORDER,
    FaultType.SENSOR_SOILING: ActionType.FLAG_SENSOR_USE_REFERENCE,
    FaultType.COMMS_DROPOUT: ActionType.RESET_COMMS_GATEWAY,
    FaultType.INVERTER_DERATE: ActionType.SET_POWER_LIMIT,
    FaultType.TRACKER_STUCK: ActionType.RESUME_TRACKING,
    FaultType.STRING_OPEN: ActionType.DISPATCH_TECHNICIAN,
    FaultType.UNAUTHORIZED_CMD: ActionType.ISOLATE_DEVICE,
}

RATIONALE = {
    FaultType.SOILING: "Plant-wide loss against the trusted reference that stayed after the wind dropped.",
    FaultType.SENSOR_SOILING: "On-site sensor reads below the reference on a clear period; it hides real losses.",
    FaultType.COMMS_DROPOUT: "Rows missing from one device while the meter total does not drop.",
    FaultType.INVERTER_DERATE: "One inverter flat-capped with high internal temperature; peers are normal.",
    FaultType.TRACKER_STUCK: "Tracker angle flat after the wind dropped below the stow limit.",
    FaultType.STRING_OPEN: "One inverter's DC down by a fixed fraction with no weather link.",
    FaultType.UNAUTHORIZED_CMD: "Inverter stopped after a command from an unexpected source.",
}

EFFECT = {
    ActionType.CREATE_CLEANING_WORK_ORDER: "Plant output returns to the line expected from the reference.",
    ActionType.FLAG_SENSOR_USE_REFERENCE: "Expected power is computed from the reference, so the soiling loss shows.",
    ActionType.RESET_COMMS_GATEWAY: "The device's data feed returns.",
    ActionType.SET_POWER_LIMIT: "Stabilises the unit's temperature until inspection; output stays limited.",
    ActionType.RESUME_TRACKING: "Block output returns to its peers in the morning and evening.",
    ActionType.DISPATCH_TECHNICIAN: "Technician repairs the open string; DC returns to its peers.",
    ActionType.ISOLATE_DEVICE: "Device disconnected until a site visit.",
}

CAUSE_AR = {
    FaultType.SOILING: "اتساخ الألواح بالغبار بعد العاصفة الرملية ({target})",
    FaultType.SENSOR_SOILING: "اتساخ حساس الإشعاع الشمسي {target}",
    FaultType.INVERTER_DERATE: "خفض حراري للقدرة في العاكس {target}",
    FaultType.COMMS_DROPOUT: "انقطاع الاتصال بالعاكس {target}",
    FaultType.TRACKER_STUCK: "توقف المتتبع الشمسي في الكتلة {target}",
    FaultType.STRING_OPEN: "سلسلة ألواح مفتوحة في العاكس {target}",
    FaultType.UNAUTHORIZED_CMD: "أمر تحكم غير مصرح به على {target}",
}

ACTION_AR = {
    ActionType.CREATE_CLEANING_WORK_ORDER: "فتح أمر عمل لتنظيف الألواح",
    ActionType.FLAG_SENSOR_USE_REFERENCE: "تعليم الحساس كمشكوك فيه واعتماد الإشعاع المرجعي",
    ActionType.RESET_COMMS_GATEWAY: "إعادة تشغيل بوابة الاتصال",
    ActionType.SET_POWER_LIMIT: "تحديد قدرة العاكس",
    ActionType.RESUME_TRACKING: "استئناف التتبع",
    ActionType.DISPATCH_TECHNICIAN: "إرسال فني",
    ActionType.ISOLATE_DEVICE: "عزل الجهاز",
}


@dataclass(frozen=True)
class PlannedAction:
    at: datetime  # when the scripted agent issues it (on the 5-minute grid)
    fault: FaultSpec
    request: ActionRequest


@dataclass
class SampleAgent:
    timeline: list[AuditEntry]
    approvals: list[PlannedAction]  # gated actions waiting for a human
    executed: list[PlannedAction]
    verifications: list[Verification]
    report: IncidentReport


# -- the scripted plan --------------------------------------------------------


def _grid(ts: datetime) -> datetime:
    step = STEP_MINUTES * 60
    return datetime.fromtimestamp(math.ceil(ts.timestamp() / step) * step, tz=ts.tzinfo)


def _storm_end(scenario: Scenario) -> datetime | None:
    for f in scenario.faults:
        if f.type == FaultType.SOILING:
            start = scenario.resolve_time(f.start)
            return scenario.resolve_time(f.end) if f.end else start + timedelta(minutes=STORM_DEFAULT_MIN)
    return None


def _act_time(scenario: Scenario, fault: FaultSpec) -> datetime:
    start = scenario.resolve_time(fault.start)
    storm_end = _storm_end(scenario)
    if fault.type == FaultType.SOILING:  # after the sensor is flagged: the loss is clear only then
        return _grid(storm_end + timedelta(minutes=ACT_AFTER_MIN + 2 * STEP_MINUTES))
    if fault.type == FaultType.SENSOR_SOILING and storm_end is not None:
        start = max(start, storm_end)
    return _grid(start + timedelta(minutes=ACT_AFTER_MIN))


def effect_delay_min(action: ActionType, plant: PlantConfig) -> int:
    c = plant.controls
    return {ActionType.RESET_COMMS_GATEWAY: c.comms_reset_delay_min,
            ActionType.CREATE_CLEANING_WORK_ORDER: c.cleaning_delay_min,
            ActionType.DISPATCH_TECHNICIAN: c.technician_delay_min}.get(action, 0)


def scripted_plan(scenario: Scenario, plant: PlantConfig) -> list[PlannedAction]:
    """One allowlisted action per planted root cause, issued ACT_AFTER_MIN after it shows."""
    specs = load_actions()
    latest = scenario.end - timedelta(minutes=VERIFY_WINDOW_MIN)
    plan = []
    for fault in scenario.faults:
        action = ACTION_FOR.get(fault.type)
        at = _act_time(scenario, fault)
        if action is None or at >= latest:
            continue
        params = {"kw": DERATE_LIMIT_FRACTION * plant.inverters.ac_kw} if action == ActionType.SET_POWER_LIMIT else {}
        spec = specs[action]
        plan.append(PlannedAction(at=at, fault=fault, request=ActionRequest(
            id=f"act-{len(plan) + 1:02d}", action=action, target=fault.target, params=params,
            reversible=spec.reversible, needs_approval=spec.needs_approval,
            rationale=f"SAMPLE: {RATIONALE[fault.type]}", expected_effect=EFFECT[action],
        )))
    return sorted(plan, key=lambda p: p.at)


# -- timeline, verification, report (numbers from the run's telemetry) --------


def local_hhmm(ts: datetime) -> str:
    return pd.Timestamp(ts).tz_convert(DISPLAY_TZ).strftime("%H:%M")


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _mean(series: pd.Series, t0: datetime, t1: datetime) -> float:
    window = series[(series.index >= t0) & (series.index < t1)].dropna()
    return float(window.mean()) if len(window) else float("nan")


def _evidence(scenario: Scenario, fault: FaultSpec, at: datetime, tables: dict[str, pd.DataFrame],
              lines: pd.DataFrame, plant: PlantConfig) -> str:
    start = scenario.resolve_time(fault.start)
    t0 = max(start, at - timedelta(minutes=VERIFY_WINDOW_MIN))
    readings = tables["inverter_readings"]
    device = readings[(readings["inverter_id"] == fault.target) & (readings["ts"] >= start) & (readings["ts"] < at)]
    if fault.type == FaultType.SOILING:
        ratio = _mean(lines["actual_kw"], t0, at) / _mean(lines["expected_ref_kw"], t0, at)
        return f"Plant output {ratio:.0%} of expected from the reference since the wind dropped; all inverters."
    if fault.type == FaultType.SENSOR_SOILING:
        ratio = _mean(lines["sensor_ghi_wm2"], t0, at) / _mean(lines["reference_ghi_wm2"], t0, at)
        return f"{fault.target} reads {ratio:.0%} of the reference GHI over the last {VERIFY_WINDOW_MIN} min."
    if fault.type == FaultType.COMMS_DROPOUT:
        steps = int((at - start) / timedelta(minutes=STEP_MINUTES))
        return (f"{steps - len(device)} of {steps} 5-min rows missing from {fault.target} since "
                f"{local_hhmm(start)}; the meter total does not drop.")
    if fault.type == FaultType.INVERTER_DERATE and len(device):
        return (f"{fault.target} internal temperature up to {device['temp_c'].max():.0f} °C "
                f"(derate limit {plant.inverters.derate_temp_c:.0f} °C), output held near "
                f"{device['ac_kw'].tail(6).median():.0f} kW.")
    return RATIONALE.get(fault.type, "")


def _verify(planned: PlannedAction, tables: dict[str, pd.DataFrame], lines: pd.DataFrame,
            plant: PlantConfig) -> tuple[datetime, Verification]:
    req = planned.request
    effect = planned.at + timedelta(minutes=effect_delay_min(req.action, plant))
    t1 = effect + timedelta(minutes=VERIFY_WINDOW_MIN)
    before = _mean(lines["actual_kw"], planned.at - timedelta(minutes=VERIFY_WINDOW_MIN), planned.at)
    after, expected = _mean(lines["actual_kw"], effect, t1), _mean(lines["expected_ref_kw"], effect, t1)
    if req.action == ActionType.RESET_COMMS_GATEWAY:
        r = tables["inverter_readings"]
        rows = int(((r["inverter_id"] == req.target) & (r["ts"] >= effect) & (r["ts"] < t1)).sum())
        recovered, note = rows > 0, f"SAMPLE: {rows} rows received from {req.target} after the reset."
    elif req.action == ActionType.FLAG_SENSOR_USE_REFERENCE:
        before = _mean(lines["expected_sensor_kw"], effect, t1)
        recovered, note = True, "SAMPLE: expected power now uses the reference instead of the sensor."
    else:
        recovered = after >= RECOVERED_FRACTION * expected
        note = f"SAMPLE: actual {after / expected:.0%} of expected from the reference."
    return t1, Verification(action_id=req.id, recovered=recovered, before_kw=round(before, 1),
                            after_kw=round(after, 1), expected_kw=round(expected, 1), note=note)


def build_agent(scenario: Scenario, plan: list[PlannedAction], tables: dict[str, pd.DataFrame],
                lines: pd.DataFrame, plant: PlantConfig) -> SampleAgent:
    timeline: list[AuditEntry] = []
    events = tables["events"]
    if len(events):
        first = events["ts"].min()
        burst = events[events["ts"] < first + timedelta(minutes=TRIAGE_WINDOW_MIN)]
        timeline.append(AuditEntry(ts=first, step="triage", actor=SAMPLE_ACTOR, payload={
            "evidence": f"{_count(len(burst), 'alarm')} in {TRIAGE_WINDOW_MIN} min: "
                        f"{_count(burst['code'].nunique(), 'code')} on {_count(burst['device_id'].nunique(), 'device')}.",
            "decision": "Group alarms by time and device; open an investigation.",
        }))
    for fault in scenario.faults:
        if fault.type == FaultType.CURTAILMENT:
            at = _grid(scenario.resolve_time(fault.start) + timedelta(minutes=STEP_MINUTES))
            limit = tables["grid_setpoint"]["limit_kw"]
            timeline.append(AuditEntry(ts=at, step="triage", actor=SAMPLE_ACTOR, payload={
                "evidence": f"Grid setpoint {limit.iloc[0]:.0f} kW active; all inverters capped together."
                            if len(limit) else "Grid setpoint active; all inverters capped together.",
                "decision": "Curtailment by the grid operator: expected behaviour, no action.",
            }))
    executed, approvals, verifications = [], [], []
    for p in plan:
        req = p.request
        timeline.append(AuditEntry(ts=p.at - timedelta(minutes=INVESTIGATE_BEFORE_MIN), step="investigate",
                                   actor=SAMPLE_ACTOR, payload={
                                       "evidence": _evidence(scenario, p.fault, p.at, tables, lines, plant),
                                       "decision": f"Root cause: {p.fault.label}.",
                                   }))
        timeline.append(AuditEntry(ts=p.at, step="plan", actor=SAMPLE_ACTOR, payload={
            "evidence": req.rationale, "decision": f"Propose {req.action.value} on {req.target}.",
        }))
        if req.needs_approval:
            approvals.append(p)
            timeline.append(AuditEntry(ts=p.at, step="approval", actor="safety-gate", payload={
                "evidence": "Action needs human approval (configs/actions.yaml: auto: false).",
                "decision": "Waiting for the operator.",
            }))
            continue
        executed.append(p)
        timeline.append(AuditEntry(ts=p.at, step="execute", actor=SAMPLE_ACTOR, payload={
            "evidence": f"Allowlisted, reversible: {req.reversible}.", "decision": f"Sent {req.action.value}.",
        }))
        at, verification = _verify(p, tables, lines, plant)
        if at <= scenario.end:
            verifications.append(verification)
            timeline.append(AuditEntry(ts=at, step="verify", actor=SAMPLE_ACTOR, payload={
                "evidence": verification.note,
                "decision": "Recovered." if verification.recovered else "Not recovered: reopen the investigation.",
            }))
    timeline.sort(key=lambda e: e.ts)
    report = _report(scenario, plan, executed, approvals, verifications)
    return SampleAgent(timeline=timeline, approvals=approvals, executed=executed,
                       verifications=verifications, report=report)


def _report(scenario: Scenario, plan: list[PlannedAction], executed: list[PlannedAction],
            approvals: list[PlannedAction], verifications: list[Verification]) -> IncidentReport:
    causes = [Hypothesis(id=f"h-{i:02d}", fault_type=f.type, targets=[f.target], evidence=[RATIONALE[f.type]], score=1.0)
              for i, f in enumerate((f for f in scenario.faults if f.type in RATIONALE), start=1)]
    day = pd.Timestamp(scenario.start).tz_convert(DISPLAY_TZ).strftime("%Y-%m-%d")
    recovered = sum(v.recovered for v in verifications)
    decoy = any(f.type == FaultType.CURTAILMENT for f in scenario.faults)

    en = [f"SAMPLE report (synthetic plant), incident {scenario.id} on {day}.",
          f"{len(causes)} root causes: " + "; ".join(h.labels[0] for h in causes) + "."]
    en += [f"{local_hhmm(p.at)} {p.request.action.value} on {p.request.target}." for p in executed]
    en += [f"{local_hhmm(p.at)} {p.request.action.value} on {p.request.target} is waiting for approval."
           for p in approvals]
    en.append(f"{recovered} of {len(verifications)} verified actions recovered.")
    if decoy:
        en.append("Grid curtailment was expected behaviour; no action taken.")

    ar = [f"تقرير نموذجي (محطة اصطناعية)، الحادثة {scenario.id} بتاريخ {day}.",
          f"الأسباب الجذرية ({len(causes)}): " + "؛ ".join(
              CAUSE_AR[h.fault_type].format(target=h.targets[0]) for h in causes) + "."]
    ar += [f"{local_hhmm(p.at)} {ACTION_AR[p.request.action]} ({p.request.target})." for p in executed]
    ar += [f"{local_hhmm(p.at)} {ACTION_AR[p.request.action]} ({p.request.target}) بانتظار موافقة المشغّل."
           for p in approvals]
    ar.append(f"تعافى {recovered} من {len(verifications)} إجراءات تم التحقق منها.")
    if decoy:
        ar.append("تقييد الشبكة سلوك متوقع، ولم يُتخذ أي إجراء.")

    return IncidentReport(summary_en=" ".join(en), summary_ar=" ".join(ar), causes=causes,
                          actions=[p.request for p in plan], verifications=verifications)


# -- benchmark ------------------------------------------------------------------


def benchmark_table() -> pd.DataFrame:
    """SAMPLE numbers in the shape bench/score.py will report. Not measured."""
    rows = [
        ("Root-cause precision", 0.86, 0.31, "higher"),
        ("Root-cause recall", 0.91, 0.78, "higher"),
        ("All causes found (share of scenarios)", 0.70, 0.15, "higher"),
        ("False-action rate (symptoms or decoys)", 0.05, 0.62, "lower"),
        ("Time to first action (sim min, median)", 25, 10, "lower"),
        ("Time to verified recovery (sim min, median)", 95, 140, "lower"),
        ("MWh recovered vs no action (mean)", 3.1, 1.4, "higher"),
        ("Cost per incident (USD)", 0.012, 0.0, "lower"),
        ("Share of LLM calls handled locally", 0.85, None, "higher"),
    ]
    frame = pd.DataFrame(rows, columns=["Metric", "Raqeeb", "Threshold baseline", "Better is"])
    better = []
    for _, r in frame.iterrows():
        if r["Threshold baseline"] is None or pd.isna(r["Threshold baseline"]):
            better.append("n/a")
        elif r["Raqeeb"] == r["Threshold baseline"]:
            better.append("tie")
        else:
            raqeeb_wins = (r["Raqeeb"] > r["Threshold baseline"]) == (r["Better is"] == "higher")
            better.append("Raqeeb" if raqeeb_wins else "Baseline")
    frame["Winner"] = better
    return frame
