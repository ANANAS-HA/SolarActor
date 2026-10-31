"""The held-out slice of the final set: `heldout_fault` placement and `generate_heldout`."""

from __future__ import annotations

from collections import Counter

import numpy as np
import pytest

from raqeeb.config import FINAL_SCENARIO_DIR, load_plant
from raqeeb.contracts import DECOY_FAULTS, HELDOUT_FAULTS, UNKNOWN_FAULTS, FaultSpec, FaultType, Scenario
from raqeeb.faults.catalogue import CATALOGUE
from raqeeb.faults.generator import (
    DRIFT_SEVERITY,
    DRIFT_START_MIN,
    FINAL,
    HELDOUT_SLICES,
    HUNG_START_MIN,
    LOAD_DROPOUT_SEVERITY,
    LOAD_DROPOUT_START_MIN,
    SILENT_TRACKER_START_MIN,
    generate,
    generate_heldout,
    generate_set,
    heldout_fault,
    load_scenarios,
)
from raqeeb.faults.injector import FaultInjector
from raqeeb.telemetry.store import TelemetryStore
from tests.leakcheck import assert_no_answer_leak, environment_rows

RANGES = {  # (start window in minutes after midnight UTC, severity range or None)
    FaultType.HUNG_INVERTER: (HUNG_START_MIN, None),
    FaultType.COMMS_DROPOUT_LOAD: (LOAD_DROPOUT_START_MIN, LOAD_DROPOUT_SEVERITY),
    FaultType.SENSOR_DRIFT: (DRIFT_START_MIN, DRIFT_SEVERITY),
    FaultType.TRACKER_STUCK_SILENT: (SILENT_TRACKER_START_MIN, None),
}


def minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


@pytest.mark.parametrize("fault_type", sorted(HELDOUT_FAULTS))
def test_each_type_gives_a_valid_spec_in_its_ranges(fault_type):
    plant = load_plant()
    spec = heldout_fault(fault_type, [], np.random.default_rng(5))
    assert isinstance(spec, FaultSpec) and spec.type == fault_type
    assert spec.target in CATALOGUE[fault_type].valid_targets(plant)
    (low, high), severity = RANGES[fault_type]
    assert low <= minutes(spec.start) <= high and minutes(spec.start) % 5 == 0
    if severity is None:
        assert spec.severity is None
    else:
        assert severity[0] <= spec.severity <= severity[1]
    assert spec == heldout_fault(fault_type, [], np.random.default_rng(5))  # deterministic for a given rng
    assert spec != heldout_fault(fault_type, [], np.random.default_rng(6)) or fault_type == FaultType.SENSOR_DRIFT


def test_placement_avoids_devices_other_faults_use():
    plant = load_plant()
    station = plant.weather_station_id
    busy = [FaultSpec(type=FaultType.SENSOR_SOILING, target=station, start="05:00", severity=0.2),
            FaultSpec(type=FaultType.COMMS_DROPOUT, target="INV-03", start="05:00")]
    assert heldout_fault(FaultType.SENSOR_DRIFT, busy, np.random.default_rng(1)) is None
    for fault_type in HELDOUT_FAULTS - {FaultType.SENSOR_DRIFT}:
        for seed in range(20):
            spec = heldout_fault(fault_type, busy, np.random.default_rng(seed))
            assert spec is not None and spec.target != "INV-03" and spec.target in plant.inverters.ids
    all_busy = [FaultSpec(type=FaultType.INVERTER_DERATE, target=i, start="05:00", severity=0.3)
                for i in plant.inverters.ids]
    assert heldout_fault(FaultType.HUNG_INVERTER, all_busy, np.random.default_rng(1)) is None
    with pytest.raises(ValueError):
        heldout_fault(FaultType.SOILING, [], np.random.default_rng(1))


def test_generate_heldout_slice():
    slice_ = generate_heldout(8, 4242, "fx", start_index=51)
    assert [s.id for s in slice_] == [f"fx_{i:04d}" for i in range(51, 59)]
    assert slice_ == generate_heldout(8, 4242, "fx", start_index=51)  # same seed, same scenarios
    assert slice_ != generate_heldout(8, 4243, "fx", start_index=51)
    assert Counter(f.type for s in slice_ for f in s.faults if f.type in HELDOUT_FAULTS) == {t: 2 for t in HELDOUT_FAULTS}
    for s in slice_:
        targets = [f.target for f in s.faults]
        assert len(targets) == len(set(targets))  # no overlapping targets
        known = [f for f in s.faults if f.type not in HELDOUT_FAULTS]
        assert len(known) <= 2 and not any(f.type in DECOY_FAULTS | UNKNOWN_FAULTS for f in known)
        assert s.answer_key == s.expected_answer_key()
        assert all(f.label in s.answer_key.root_causes for f in s.faults if f.type in HELDOUT_FAULTS)


def test_injected_heldout_scenario_runs_and_leaks_nothing():
    plant = load_plant()
    for s in generate_heldout(4, 4242, "fx")[:2]:
        inj = FaultInjector.for_scenario(s, plant)
        store = TelemetryStore()
        store.write_batch(inj.advance(4 * 60))  # past the earliest start window
        for table in ("inverter_readings", "weather_station", "events", "audit"):
            assert_no_answer_leak(environment_rows(store, table), s)
        assert_no_answer_leak(s.agent_view(), s, strict=True)


def test_final_set_is_the_regular_set_plus_the_slice():
    regular = generate(**FINAL)
    final = generate_set("final")
    assert final[:len(regular)] == regular  # the slice changes nothing before it
    assert len(final) == len(regular) + HELDOUT_SLICES["final"]
    assert not {s.seed for s in final[len(regular):]} & {s.seed for s in regular}
    assert all(f.type not in HELDOUT_FAULTS for s in regular for f in s.faults)


def test_committed_final_set_is_frozen():
    """scenarios/final/: generated once from the final-candidate tag, never edited or regenerated."""
    committed = load_scenarios(FINAL_SCENARIO_DIR, FINAL["prefix"])
    if not committed:
        pytest.skip("scenarios/final not generated yet")
    assert committed == generate_set("final")
    assert all(isinstance(s, Scenario) and s.id.startswith("fin_") for s in committed)
