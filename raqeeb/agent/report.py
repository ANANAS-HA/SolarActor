"""Report: the shift report (English and Arabic) from the agent's own records.

Milestone 1 builds it from templates in code; no language model writes it yet. Times are shown in
Asia/Riyadh. Only the agent's records go in: no alarm or log text is copied into the report. A
device log line is reported by its device only, as flagged for security review.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from raqeeb.agent.state import PASSIVE, CauseStatus, Memory, PollState
from raqeeb.contracts import DISPLAY_TZ, PLANT_TARGET, ActionStatus, Escalation, FaultType, IncidentReport

_TZ = ZoneInfo(DISPLAY_TZ)
ESCALATE_UNEXPLAINED_AFTER = 2  # polls in a row a symptom stays unexplained before a human gets it

FAULT_EN = {
    FaultType.SOILING: "panel soiling after a dust storm",
    FaultType.SENSOR_SOILING: "dusty irradiance sensor",
    FaultType.INVERTER_DERATE: "inverter thermal derating",
    FaultType.COMMS_DROPOUT: "communications loss",
    FaultType.CURTAILMENT: "grid curtailment (expected behaviour)",
    FaultType.TRACKER_STUCK: "stuck tracker",
    FaultType.STRING_OPEN: "open string",
    FaultType.UNAUTHORIZED_CMD: "unauthorised control command",
    FaultType.PROMPT_INJECTION: "unrecognized log entry",
}
FAULT_AR = {
    FaultType.SOILING: "اتساخ الألواح بالغبار بعد عاصفة رملية",
    FaultType.SENSOR_SOILING: "اتساخ حساس الإشعاع الشمسي",
    FaultType.INVERTER_DERATE: "خفض قدرة العاكس بسبب ارتفاع الحرارة",
    FaultType.COMMS_DROPOUT: "انقطاع الاتصال",
    FaultType.CURTAILMENT: "تقييد الإنتاج من مشغل الشبكة (سلوك متوقع)",
    FaultType.TRACKER_STUCK: "توقف نظام التتبع",
    FaultType.STRING_OPEN: "انقطاع سلسلة ألواح",
    FaultType.UNAUTHORIZED_CMD: "أمر تحكم غير مصرح به",
    FaultType.PROMPT_INJECTION: "سطر سجل غير معروف",
}
ACTION_AR = {
    "reset_comms_gateway": "إعادة تشغيل بوابة الاتصال",
    "flag_sensor_use_reference": "تعليم الحساس كمشكوك فيه واعتماد بيانات المرجع",
    "create_cleaning_work_order": "أمر عمل لتنظيف الألواح",
    "resume_tracking": "استئناف التتبع",
    "dispatch_technician": "إرسال فني",
    "set_power_limit": "تحديد قدرة العاكس",
    "isolate_device": "عزل الجهاز",
}
STATUS_EN = {
    CauseStatus.OPEN: "diagnosed", CauseStatus.WAITING: "fix waiting", CauseStatus.ACTING: "fix in progress",
    CauseStatus.RESOLVED: "resolved", CauseStatus.REOPENED: "fix did not work", CauseStatus.BLOCKED: "left to operators",
    CauseStatus.EXPECTED: "expected", CauseStatus.FLAGGED: "flagged for security review",
    CauseStatus.ESCALATED: "escalated to a human",
}
STATUS_AR = {
    CauseStatus.OPEN: "تم التشخيص", CauseStatus.WAITING: "الإصلاح بالانتظار", CauseStatus.ACTING: "الإصلاح جارٍ",
    CauseStatus.RESOLVED: "تمت المعالجة", CauseStatus.REOPENED: "الإصلاح لم ينجح", CauseStatus.BLOCKED: "محال إلى المشغلين",
    CauseStatus.EXPECTED: "متوقع", CauseStatus.FLAGGED: "محال للمراجعة الأمنية",
    CauseStatus.ESCALATED: "مُصعّد إلى مختص",
}


def _local(ts) -> str:
    return f"{ts.astimezone(_TZ):%H:%M}"


def _name_en(fault: FaultType) -> str:
    return FAULT_EN.get(fault, fault.value.replace("_", " ").lower())


def _name_ar(fault: FaultType) -> str:
    return FAULT_AR.get(fault, fault.value)


def build_report(memory: Memory, now) -> IncidentReport:
    causes = [c for c in memory.causes.values() if c.status not in PASSIVE]
    expected = [c for c in memory.causes.values() if c.status == CauseStatus.EXPECTED]
    flagged = sorted(c.hypothesis.targets[0] for c in memory.causes.values() if c.status == CauseStatus.FLAGGED)
    pendings = list(memory.pending.values())
    applied = [p for p in pendings if p.status == ActionStatus.APPLIED]
    verified = [p for p in applied if p.verification is not None and p.verification.recovered]
    diagnosis = memory.diagnosis

    en = [f"Incident {memory.view.id}, {_local(memory.view.start)} to {_local(now)} Riyadh time."]
    ar = [f"الحادثة {memory.view.id}، من {_local(memory.view.start)} إلى {_local(now)} بتوقيت الرياض."]
    if causes:
        en.append("Root causes: " + "; ".join(
            f"{_name_en(c.hypothesis.fault_type)} on {c.hypothesis.targets[0]} ({STATUS_EN[c.status]})"
            + (", proposed by a language model and tested on tool data" if c.source == "llm" else "")
            for c in causes) + ".")
        ar.append("الأسباب الجذرية: " + "؛ ".join(
            f"{_name_ar(c.hypothesis.fault_type)} في {c.hypothesis.targets[0]} ({STATUS_AR[c.status]})" for c in causes) + ".")
    else:
        en.append("No root cause found.")
        ar.append("لم يتم العثور على سبب جذري.")
    if expected:
        en.append("Expected behaviour, no action: " + "; ".join(FAULT_EN[c.hypothesis.fault_type] for c in expected) + ".")
        ar.append("سلوك متوقع دون إجراء: " + "؛ ".join(FAULT_AR[c.hypothesis.fault_type] for c in expected) + ".")
    for old, new in memory.revised:
        en.append(f"Diagnosis revised: {old} replaced by {new} as new evidence came in.")
        ar.append(f"تم تعديل التشخيص: استُبدل {old} بـ {new} بعد ظهور أدلة جديدة.")
    for c in causes:
        if c.status == CauseStatus.ESCALATED:
            en.append(f"Escalated to a human: {c.label}, {c.note}.")
            ar.append(f"مُصعّد إلى مختص: {c.label}.")
    for e in memory.escalations:
        if e.subject == "unexplained":
            en.append(f"Escalated to a human: symptoms no known cause explains on {', '.join(e.devices)} "
                      f"({e.reason}). Not guessed, no action taken.")
            ar.append(f"مُصعّد إلى مختص: أعراض لا يفسرها أي سبب معروف في {', '.join(e.devices)}. "
                      "لم يُتخذ أي إجراء.")
    for device in flagged:
        en.append(f"Unrecognized log entry from {device}, not acted on, flagged for security review.")
        ar.append(f"سطر سجل غير معروف من {device}، لم يُتخذ بشأنه أي إجراء، وأُحيل للمراجعة الأمنية.")
    en.append(f"Actions: {len(pendings)} requested, {len(applied)} applied, {len(verified)} verified as recovered.")
    ar.append(f"الإجراءات: {len(pendings)} مطلوبة، {len(applied)} منفذة، {len(verified)} تم التحقق من نجاحها.")
    for p in pendings:
        v = p.verification
        state_en = "awaiting verification" if v is None else ("recovered" if v.recovered else "not recovered")
        state_ar = "بانتظار التحقق" if v is None else ("تم التعافي" if v.recovered else "لم يتم التعافي")
        if p.status not in (None, ActionStatus.APPLIED):
            state_en = state_ar = p.status.value.lower()
        en.append(f"- {p.request.action.value} on {p.request.target}: {state_en}" + (f" ({v.note})" if v else ""))
        ar.append(f"- {ACTION_AR[p.request.action.value]} في {p.request.target}: {state_ar}")
    if diagnosis is not None and diagnosis.unexplained:
        en.append(f"Unexplained symptoms for the operators: {', '.join(diagnosis.unexplained)}.")
        ar.append(f"أعراض غير مفسرة للمشغلين: {', '.join(diagnosis.unexplained)}.")
    en.append("Draft written by rules in code (no language model). Plant data is synthetic.")
    ar.append("مسودة مكتوبة بقواعد برمجية (دون نموذج لغوي). بيانات المحطة اصطناعية.")
    return IncidentReport(
        summary_en="\n".join(en), summary_ar="\n".join(ar),
        causes=[c.hypothesis for c in causes],
        actions=[p.request for p in pendings],
        verifications=[p.verification for p in pendings if p.verification is not None],
        cost_usd=sum(c.cost_usd for c in memory.llm_calls), llm_calls=len(memory.llm_calls),
    )


def is_done(memory: Memory) -> bool:
    """Every root cause is resolved or handed to operators, and no request is still in flight."""
    settled = all(c.status in PASSIVE | {CauseStatus.RESOLVED, CauseStatus.BLOCKED, CauseStatus.ESCALATED}
                  for c in memory.causes.values())
    in_flight = any(p.status is None or (p.status == ActionStatus.APPLIED and p.verification is None)
                    for p in memory.pending.values())
    return bool(memory.causes) and settled and not in_flight


def escalate(memory: Memory, now) -> list[Escalation]:
    """Hand to a human what the rules cannot fix: causes whose fixes are used up, and symptoms that no
    known cause explains for ESCALATE_UNEXPLAINED_AFTER polls (never guessed, never acted on)."""
    new: list[Escalation] = []
    for cause in memory.causes.values():
        if cause.status == CauseStatus.ESCALATED and cause.label not in memory.escalated:
            memory.escalated.add(cause.label)
            target = cause.hypothesis.targets[0]
            devices = memory.plant.layout.area_of(target) or [target]
            new.append(Escalation(ts=now, subject=cause.label, devices=devices, reason=cause.note or "no fix left"))
    unexplained = set(memory.diagnosis.unexplained) if memory.diagnosis else set()
    memory.unexplained_polls = {sid: memory.unexplained_polls.get(sid, 0) + 1 for sid in unexplained}
    due = sorted(sid for sid, polls in memory.unexplained_polls.items()
                 if polls >= ESCALATE_UNEXPLAINED_AFTER and sid not in memory.escalated)
    if due:
        memory.escalated |= set(due)
        symptoms = [memory.symptoms[sid] for sid in due if sid in memory.symptoms]
        devices = sorted({s.device_id for s in symptoms if s.device_id != PLANT_TARGET}) or [PLANT_TARGET]
        reason = "; ".join(s.evidence.get("summary", s.id) for s in symptoms)
        new.append(Escalation(ts=now, subject="unexplained", devices=devices, reason=reason[:500]))
    for e in new:
        memory.escalations.append(e)
        memory.log(now, "report", f"escalated to a human: {e.subject} on {', '.join(e.devices)}", reason=e.reason)
    return new


def report(state: PollState) -> PollState:
    memory, now = state["memory"], state["now"]
    escalate(memory, now)
    if state.get("final") or (is_done(memory) and memory.report is None):
        memory.report = build_report(memory, now)
        if state.get("final"):  # end of the incident: the model writes the explanation and the report
            from raqeeb.agent.consult import write_up
            memory.report = write_up(memory, memory.report)
        memory.log(now, "report", "shift report written")
    return {}
