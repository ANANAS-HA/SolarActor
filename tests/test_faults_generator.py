"""Scenario generator: seeded, answer keys, traps and decoys, YAML round-trip, committed files."""

import re
from collections import Counter

import pytest

from raqeeb.config import SCENARIO_DIR, load_plant
from raqeeb.contracts import CORE_FAULTS, DECOY_FAULTS, FULL_SCOPE_FAULTS, FaultType
from raqeeb.faults.generator import (
    COOL_MONTHS,
    generate,
    has_decoy,
    is_trap,
    load_scenarios,
    storm_scenario,
    write_scenarios,
)
from raqeeb.faults.injector import FaultInjector

SYMPTOM_CODES = {"LOW_PR", "LOW_STRING_CURRENT", "DC_INPUT_LOW"}


@pytest.fixture(scope="module")
def core():
    return generate()


def test_same_seed_same_scenarios(core):
    assert generate() == core
    assert generate(seed=7) != core


def test_core_set_shape(core):
    assert len(core) == 20 and len({s.id for s in core}) == 20
    for s in core:
        assert 1 <= len(s.faults) <= 4
        assert {f.type for f in s.faults} <= CORE_FAULTS
        assert len({f.type for f in s.faults}) == len(s.faults)
        targets = [f.target for f in s.faults if f.target.startswith("INV-")]
        assert len(targets) == len(set(targets))


def test_answer_keys(core):
    for s in core:
        assert s.answer_key == s.expected_answer_key()
        assert s.answer_key.root_causes  # at least one real fault
        assert not any(label.startswith("CURTAILMENT") for label in s.answer_key.root_causes)


def test_traps_and_decoys(core):
    traps = [s for s in core if is_trap(s)]
    decoys = [s for s in core if has_decoy(s)]
    assert len(traps) >= 3 and len(decoys) >= 3
    for s in traps:
        assert s.start.month in COOL_MONTHS
        severity = {f.type: f.severity for f in s.faults}
        assert abs(severity[FaultType.SOILING] - severity[FaultType.SENSOR_SOILING]) <= 0.03 + 1e-9
    for s in decoys:
        assert any(f.type not in DECOY_FAULTS for f in s.faults)


def test_severities_and_times(core):
    for s in core:
        for f in s.faults:
            assert re.fullmatch(r"\d\d:\d\d", f.start) and (f.end is None or re.fullmatch(r"\d\d:\d\d", f.end))
            if f.type == FaultType.SENSOR_SOILING:
                assert f.severity >= 0.10  # the reference itself is only good to a few percent
            if f.type in (FaultType.SOILING, FaultType.CURTAILMENT):
                assert f.end is not None


def test_yaml_round_trip(core, tmp_path):
    paths = write_scenarios(core, tmp_path)
    assert [p.name for p in paths] == [f"{s.id}.yaml" for s in core]
    assert load_scenarios(tmp_path) == core
    text = paths[0].read_text(encoding="utf-8")
    assert re.search(r"start: '\d\d:\d\d'", text)  # times stay quoted, never sexagesimal ints


def test_committed_scenarios_match_generator(core):
    assert load_scenarios(SCENARIO_DIR) == core


def test_full_scope_generation_runs():
    scenarios = generate(n=30, seed=11, full_scope=True)
    used = Counter(f.type for s in scenarios for f in s.faults)
    assert FULL_SCOPE_FAULTS & set(used)
    plant = load_plant()
    for s in scenarios:
        if {f.type for f in s.faults} & FULL_SCOPE_FAULTS:
            FaultInjector.for_scenario(s, plant).run_to_end()


def test_core_scenarios_run_with_expected_effects(core):
    plant = load_plant()
    for s in core:
        batch = FaultInjector.for_scenario(s, plant).run_to_end()
        raised = Counter(e.code for e in batch.events)
        types = {f.type for f in s.faults}
        if FaultType.SOILING in types:
            assert raised["TRACKER_STOW_WIND"] == 16, s.id
            if is_trap(s):
                assert raised["LOW_STRING_CURRENT"] == 0, s.id  # the dusty sensor hides the loss
            else:
                assert raised["LOW_STRING_CURRENT"] >= 12, s.id
                assert 30 <= sum(raised[c] for c in SYMPTOM_CODES | {"HIGH_WIND", "TRACKER_STOW_WIND"}) <= 50, s.id
        if FaultType.CURTAILMENT in types:
            limit = batch.grid_setpoint[0].limit_kw
            window = {g.ts for g in batch.grid_setpoint}
            assert max(m.plant_ac_kw for m in batch.meter if m.ts in window) > 0.95 * limit, s.id  # binds
        if FaultType.INVERTER_DERATE in types:
            assert raised["DERATE_TEMP"] >= 1, s.id
        if FaultType.COMMS_DROPOUT in types:
            assert raised["COMM_LOSS"] == 1, s.id


@pytest.mark.parametrize("seed", range(12))
def test_storm_scenario(seed):
    s = storm_scenario(seed)
    assert storm_scenario(seed) == s
    types = [f.type for f in s.faults]
    assert FaultType.SOILING in types and len(types) == 3 and len(set(types)) == 3
    assert s.start.month in COOL_MONTHS
    assert s.answer_key == s.expected_answer_key()
    if FaultType.SENSOR_SOILING in types:
        assert is_trap(s)
