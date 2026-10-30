"""Held-out faults (HUNG_INVERTER, COMMS_DROPOUT_LOAD, SENSOR_DRIFT): telemetry signature and recovery.

These faults have no detection rules yet: they are kept out of the causal graph, the planner and every
committed scenario set, so they can serve as unknowns. "Detection" tests here check the signature in
the telemetry the agent would read, not an agent rule.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache

import numpy as np
import pytest

from raqeeb.agent.plan import LADDERS
from raqeeb.agent.rank import CAUSAL_GRAPH
from raqeeb.bench.harness import MITIGATED
from raqeeb.config import SCENARIO_DIR, load_plant
from raqeeb.contracts import (
    CORE_FAULTS,
    FULL_SCOPE_FAULTS,
    HELDOUT_FAULTS,
    UNKNOWN_FAULTS,
    ActionType,
    FaultSpec,
    FaultType,
    InverterStatus,
    Scenario,
)
from raqeeb.faults.catalogue import CATALOGUE
from raqeeb.faults.heldout import HeldoutFault
from raqeeb.faults.injector import FaultInjector
from raqeeb.telemetry.store import TelemetryStore
from raqeeb.tools import AGENT_TOOLS
from raqeeb.twin.controls import apply_action
from tests.conftest import call

DAY = datetime(2025, 1, 15, 3, tzinfo=timezone.utc)  # clear, cool day, 06:00-16:00 Riyadh (no clipping)
HOURS = 10
SEED = 21
STATION = "WS-01"
HUNG = {"type": "HUNG_INVERTER", "target": "INV-05", "start": "06:00"}
LOAD = {"type": "COMMS_DROPOUT_LOAD", "target": "INV-12", "start": "05:00", "severity": 0.5}  # 500 W/m2
DRIFT = {"type": "SENSOR_DRIFT", "target": STATION, "start": "05:00", "severity": 0.04}
TRACKER = {"type": "TRACKER_STUCK_SILENT", "target": "INV-08", "start": "04:30"}
CASES = {"hung": HUNG, "load": LOAD, "drift": DRIFT, "tracker": TRACKER}


def at(hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return DAY.replace(hour=h, minute=m)


def scenario(*faults: dict, seed: int = SEED) -> Scenario:
    s = Scenario(id="fx_heldout", seed=seed, start=DAY, duration_hours=HOURS,
                 faults=[FaultSpec(**f) for f in faults])
    return s.model_copy(update={"answer_key": s.expected_answer_key()})


def run(faults: tuple[dict, ...], actions: tuple[tuple[str, ActionType, str], ...] = (), seed: int = SEED):
    """Drive the scenario through the injector, applying (HH:MM, action, target) on the way."""
    s = scenario(*faults, seed=seed)
    inj = FaultInjector.for_scenario(s)
    store = TelemetryStore()
    for hhmm, action, target in sorted(actions):
        store.write_batch(inj.advance((at(hhmm) - inj.twin.clock).total_seconds() / 60))
        apply_action(inj.twin, action, target)
    store.write_batch(inj.run_to_end())
    return s, inj, store


@lru_cache
def planted(name: str, actions: tuple = ()):
    return run((CASES[name],), actions)


@lru_cache
def healthy():
    return run(())


def readings(store: TelemetryStore, inverter: str):
    frame = store.read("inverter_readings")
    return frame[frame["inverter_id"] == inverter]


def truth_ac(inj: FaultInjector, inverter: str) -> dict[datetime, float]:
    return {t.ts: t.inverter_ac_kw[inverter] for t in inj.twin.truth}


def events(store: TelemetryStore, device: str) -> list[str]:
    frame = store.read("events")
    return list(frame[frame["device_id"] == device]["code"])


# -- contract: held out from rules and generated sets ---------------------------------------------------


def test_heldout_faults_have_no_rules_and_no_scenarios():
    assert HELDOUT_FAULTS == {FaultType.HUNG_INVERTER, FaultType.COMMS_DROPOUT_LOAD, FaultType.SENSOR_DRIFT,
                              FaultType.TRACKER_STUCK_SILENT}
    assert not HELDOUT_FAULTS & (CORE_FAULTS | FULL_SCOPE_FAULTS | UNKNOWN_FAULTS)
    assert not HELDOUT_FAULTS & set(CAUSAL_GRAPH.nodes)
    assert not HELDOUT_FAULTS & set(LADDERS)
    for fault in HELDOUT_FAULTS:
        assert issubclass(CATALOGUE[fault], HeldoutFault) and fault in MITIGATED
    committed = [Scenario.from_yaml(p) for p in sorted(SCENARIO_DIR.rglob("*.yaml")) if "final" not in p.parts]
    assert committed and not any(f.type in HELDOUT_FAULTS for s in committed for f in s.faults)


@pytest.mark.parametrize("name", sorted(CASES))
def test_answer_key_names_the_cause_and_the_agent_view_hides_it(name):
    s, inj, store = planted(name)
    label = f"{CASES[name]['type']}@{CASES[name]['target']}"
    assert s.answer_key.root_causes == [label]
    assert label not in s.agent_view().model_dump_json()
    (fault,) = inj.faults
    assert fault.record["start"] == at(CASES[name]["start"])  # hidden record: bench and tests only
    for tool in AGENT_TOOLS.values():
        call(tool, store, s)  # every tool output passes assert_no_answer_leak


def test_bad_targets_are_rejected():
    plant = load_plant()
    with pytest.raises(ValueError):
        CATALOGUE[FaultType.HUNG_INVERTER](FaultSpec(type="HUNG_INVERTER", target=STATION, start="05:00"),
                                           scenario(), plant)
    with pytest.raises(ValueError):
        CATALOGUE[FaultType.SENSOR_DRIFT](FaultSpec(type="SENSOR_DRIFT", target="INV-01", start="05:00"),
                                          scenario(), plant)


# -- 1. HUNG_INVERTER: "a workflow that hangs forever" ----------------------------------------------------


def test_hung_inverter_reports_frozen_online_readings_while_producing_nothing():
    s, inj, store = planted("hung")
    rows = readings(store, "INV-05")
    hang = at(HUNG["start"])
    before, after = rows[rows.index < hang], rows[rows.index >= hang]
    assert len(rows) == HOURS * 12  # no missing rows: it looks online
    last = before.iloc[-1]
    assert after["status"].eq(InverterStatus.RUNNING.value).all() and last["status"] == InverterStatus.RUNNING.value
    for column in ("ac_kw", "dc_kw", "temp_c", "tracker_angle_deg"):
        assert after[column].eq(last[column]).all(), column  # values stop updating
    live = before[before["ac_kw"] > 0]
    assert len(live) > 6 and live["ac_kw"].nunique() == len(live)  # a live inverter never repeats exactly
    produced = truth_ac(inj, "INV-05")
    assert all(kw == 0.0 for ts, kw in produced.items() if ts >= hang)
    assert all(produced[ts] > 0 for ts in before.index if before.loc[ts, "ac_kw"] > 0)
    # the meter sees the loss even though the inverter's own rows do not
    meter, meter_ok = store.read("meter")["plant_ac_kw"], healthy()[2].read("meter")["plant_ac_kw"]
    lost = (meter_ok - meter)[meter.index >= hang]
    assert (lost > 0.5 * after["ac_kw"].iloc[0]).all()
    assert not {"COMM_LOSS", "REMOTE_STOP"} & set(events(store, "INV-05"))


def test_hung_inverter_recovers_after_a_controlled_restart():
    s, inj, store = planted("hung", (("07:00", ActionType.RESET_COMMS_GATEWAY, "INV-05"),))
    back = at("07:00") + timedelta(minutes=inj.twin.plant.controls.comms_reset_delay_min)
    produced, healthy_ac = truth_ac(inj, "INV-05"), truth_ac(healthy()[1], "INV-05")
    assert all(produced[ts] == 0.0 for ts in produced if at(HUNG["start"]) <= ts < back)
    assert all(produced[ts] == pytest.approx(healthy_ac[ts]) for ts in produced if ts >= back)
    rows = readings(store, "INV-05")
    assert rows[rows.index >= back]["ac_kw"].nunique() == len(rows[rows.index >= back])  # updating again
    assert MITIGATED[FaultType.HUNG_INVERTER](inj.twin.state, "INV-05")


def test_hung_inverter_without_a_restart_never_recovers():
    s, inj, store = planted("hung")
    assert not MITIGATED[FaultType.HUNG_INVERTER](inj.twin.state, "INV-05")
    assert inj.twin.state.inverters["INV-05"].hung


# -- 2. COMMS_DROPOUT_LOAD: "loses data under load" --------------------------------------------------------


def _high(inj: FaultInjector) -> dict[datetime, bool]:
    (fault,) = [f for f in inj.faults if f.fault_type == FaultType.COMMS_DROPOUT_LOAD]
    return {t.ts: t.ghi_wm2 > fault.record["ghi_threshold_wm2"] for t in inj.twin.truth}


def test_comms_dropout_load_drops_rows_only_while_irradiance_is_high():
    s, inj, store = planted("load")
    (fault,) = inj.faults
    assert fault.record["ghi_threshold_wm2"] == pytest.approx(500.0)
    high, sent = _high(inj), set(readings(store, "INV-12").index)
    start = at(LOAD["start"])
    assert any(high[ts] for ts in high if ts >= start) and any(not high[ts] for ts in high if ts >= start)
    for ts, is_high in high.items():
        assert (ts in sent) == (ts < start or not is_high), ts  # missing rows, never zeros
    assert truth_ac(inj, "INV-12") == truth_ac(healthy()[1], "INV-12")  # it keeps producing
    assert "COMM_LOSS" in events(store, "INV-12")
    other = readings(store, "INV-11")
    assert len(other) == HOURS * 12  # only the faulty inverter loses data


def test_comms_dropout_load_survives_a_gateway_reset_and_a_technician_fixes_it():
    reset = planted("load", (("07:00", ActionType.RESET_COMMS_GATEWAY, "INV-12"),))
    high, sent = _high(reset[1]), set(readings(reset[2], "INV-12").index)
    assert any(high[ts] and ts not in sent for ts in high if ts >= at("07:30"))  # the reset does not hold
    assert not MITIGATED[FaultType.COMMS_DROPOUT_LOAD](reset[1].twin.state, "INV-12")

    s, inj, store = planted("load", (("06:00", ActionType.DISPATCH_TECHNICIAN, "INV-12"),))
    fixed = at("06:00") + timedelta(minutes=inj.twin.plant.controls.technician_delay_min)
    high, sent = _high(inj), set(readings(store, "INV-12").index)
    assert any(high[ts] for ts in high if ts >= fixed)
    assert all(ts in sent for ts in high if ts >= fixed)
    assert MITIGATED[FaultType.COMMS_DROPOUT_LOAD](inj.twin.state, "INV-12")


# -- 3. SENSOR_DRIFT: "slowly corrupts data" ----------------------------------------------------------------


def _sensor_ratio(inj: FaultInjector, store: TelemetryStore):
    sensor = store.read("weather_station")["ghi_sensor_wm2"]
    true = {t.ts: t.ghi_wm2 for t in inj.twin.truth}
    keep = [ts for ts in sensor.index if true[ts] > 200]
    return keep, np.array([sensor[ts] / true[ts] for ts in keep])


def test_sensor_drift_bias_grows_linearly_over_hours():
    s, inj, store = planted("drift")
    (fault,) = inj.faults
    rate, start = fault.record["drift_per_h"], at(DRIFT["start"])
    assert abs(rate) == pytest.approx(0.04)
    keep, ratio = _sensor_ratio(inj, store)
    hours = np.array([max((ts - start).total_seconds() / 3600, 0.0) for ts in keep])
    after = hours > 0
    slope, intercept = np.polyfit(hours[after], ratio[after], 1)
    assert slope == pytest.approx(rate, abs=0.004) and intercept == pytest.approx(1.0, abs=0.02)
    assert np.abs(ratio[~after] - 1.0).max() < 0.05  # fine before the drift starts
    assert abs(ratio[-1] - 1.0) > 0.15  # ~0.25 off after six hours above 200 W/m2
    end_hours = (DAY + timedelta(hours=HOURS) - start).total_seconds() / 3600
    assert inj.twin.state.station.sensor_bias == pytest.approx(rate * end_hours)
    assert truth_ac(inj, "INV-01") == truth_ac(healthy()[1], "INV-01")  # the plant itself is fine


def test_sensor_drift_direction_varies_between_runs():
    signs = set()
    for seed in range(8):
        s = scenario(DRIFT, seed=seed)
        inj = FaultInjector.for_scenario(s)
        signs.add(np.sign(inj.faults[0].record["drift_per_h"]))
    assert signs == {-1.0, 1.0}


def test_sensor_drift_is_cleared_by_recalibration_not_by_flagging():
    flagged = planted("drift", (("06:00", ActionType.FLAG_SENSOR_USE_REFERENCE, STATION),))
    assert abs(flagged[1].twin.state.station.sensor_bias) > 0.2  # the tools stop trusting it; it still drifts
    assert MITIGATED[FaultType.SENSOR_DRIFT](flagged[1].twin.state, STATION)

    s, inj, store = planted("drift", (("06:00", ActionType.DISPATCH_TECHNICIAN, STATION),))
    fixed = at("06:00") + timedelta(minutes=inj.twin.plant.controls.technician_delay_min)
    keep, ratio = _sensor_ratio(inj, store)
    later = np.array([ts > fixed for ts in keep])
    assert later.any() and np.abs(ratio[later] - 1.0).max() < 0.05
    station = inj.twin.state.station
    assert station.sensor_bias == 0.0 and station.sensor_drift_per_h == 0.0
    assert MITIGATED[FaultType.SENSOR_DRIFT](inj.twin.state, STATION)


# -- 4. TRACKER_STUCK_SILENT: stuck tracker -----------------------------------------------------------------


def test_silent_stuck_tracker_holds_its_angle_with_no_alarm():
    s, inj, store = planted("tracker")
    (fault,) = inj.faults
    start, angle = at(TRACKER["start"]), fault.record["angle_deg"]
    assert abs(angle) > 20  # frozen mid-morning, not at stow
    rows, ok = readings(store, "INV-08"), readings(healthy()[2], "INV-08")
    assert rows[rows.index >= start]["tracker_angle_deg"].eq(angle).all()
    assert ok[ok.index >= start]["tracker_angle_deg"].nunique() > 20  # a healthy tracker keeps moving
    assert np.array_equal(readings(store, "INV-07")["tracker_angle_deg"].to_numpy(),
                          readings(healthy()[2], "INV-07")["tracker_angle_deg"].to_numpy())  # peers unchanged
    assert not {"TRACKER_FAULT", "TRACKER_STOW_WIND"} & set(events(store, "INV-08"))
    produced, healthy_ac = truth_ac(inj, "INV-08"), truth_ac(healthy()[1], "INV-08")
    afternoon = [ts for ts in produced if ts >= at("10:30") and healthy_ac[ts] > 0]  # near noon it barely shows
    assert afternoon and all(produced[ts] < 0.9 * healthy_ac[ts] for ts in afternoon)  # facing east after noon


def test_silent_stuck_tracker_recovers_after_resume_tracking():
    s, inj, store = planted("tracker", (("08:00", ActionType.RESUME_TRACKING, "INV-08"),))
    rows, ok = readings(store, "INV-08"), readings(healthy()[2], "INV-08")
    later = rows.index >= at("08:00")
    assert np.allclose(rows[later]["tracker_angle_deg"], ok[later]["tracker_angle_deg"])
    produced, healthy_ac = truth_ac(inj, "INV-08"), truth_ac(healthy()[1], "INV-08")
    assert all(produced[ts] == pytest.approx(healthy_ac[ts]) for ts in produced if ts >= at("08:00"))
    assert MITIGATED[FaultType.TRACKER_STUCK_SILENT](inj.twin.state, "INV-08")
    assert not MITIGATED[FaultType.TRACKER_STUCK_SILENT](planted("tracker")[1].twin.state, "INV-08")
