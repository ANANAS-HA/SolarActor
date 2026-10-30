"""Scenario generator: seeded, answer keys, traps and decoys, YAML round-trip, committed files."""

import re
from collections import Counter

import pytest

from raqeeb.config import PRACTICE2_SCENARIO_DIR, SAFETY_SCENARIO_DIR, SCENARIO_DIR, TEST_SCENARIO_DIR, load_plant
from raqeeb.contracts import CORE_FAULTS, DECOY_FAULTS, FULL_SCOPE_FAULTS, FaultType
from raqeeb.faults.generator import (
    COOL_MONTHS,
    PRACTICE2,
    SAFETY,
    TEST,
    generate,
    generate_set,
    has_decoy,
    has_injection,
    is_trap,
    load_scenarios,
    storm_scenario,
    write_scenarios,
)
from raqeeb.faults.catalogue import INJECTION_TEMPLATES
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


def test_committed_test_set_is_held_out_and_full_scope(core):
    """The test set is frozen in scenarios/test/: regenerating or editing it fails here."""
    test_set = load_scenarios(TEST_SCENARIO_DIR, TEST["prefix"])
    assert test_set == generate(TEST["n"], TEST["seed"], TEST["full_scope"], TEST["prefix"])
    assert len(test_set) == 50 and all(s.id.startswith("tst_") for s in test_set)
    assert not {s.seed for s in test_set} & {s.seed for s in core}  # new seeds
    assert not {s.id for s in test_set} & {s.id for s in core}
    used = {f.type for s in test_set for f in s.faults}
    assert FULL_SCOPE_FAULTS - DECOY_FAULTS <= used and CORE_FAULTS <= used
    assert FaultType.PROMPT_INJECTION not in used  # injected instructions live in the safety set
    assert sum(is_trap(s) for s in test_set) >= 5 and sum(has_decoy(s) for s in test_set) >= 5
    assert load_scenarios(SCENARIO_DIR) == core  # practice files are not picked up from test/ or vice versa


def test_committed_practice2_set_is_frozen_and_full_scope(core):
    """scenarios/practice2/: the second practice set (tuned on). Regenerating or editing it fails here."""
    practice2 = load_scenarios(PRACTICE2_SCENARIO_DIR, PRACTICE2["prefix"])
    assert practice2 == generate_set("practice2")
    assert len(practice2) == 30 and all(s.id.startswith("pr2_") for s in practice2)
    others = load_scenarios(TEST_SCENARIO_DIR, TEST["prefix"]) + load_scenarios(SAFETY_SCENARIO_DIR, SAFETY["prefix"])
    assert not {s.seed for s in practice2} & {s.seed for s in core + others}
    used = Counter(f.type for s in practice2 for f in s.faults)
    for fault in (FaultType.TRACKER_STUCK, FaultType.STRING_OPEN, FaultType.UNAUTHORIZED_CMD):
        assert used[fault] >= 4, fault
    assert FaultType.PROMPT_INJECTION not in used
    assert sum(is_trap(s) for s in practice2) == 6 and sum(has_decoy(s) for s in practice2) == 6
    assert all(s.answer_key == s.expected_answer_key() for s in practice2)


def test_old_sets_unchanged_by_the_new_options(core):
    assert generate_set("practice") == core
    assert generate_set("test") == load_scenarios(TEST_SCENARIO_DIR, TEST["prefix"])


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


def test_committed_safety_set_has_one_injection_each():
    """scenarios/safety/: full-scope scenarios plus one injected instruction, never in the answer key."""
    safety = load_scenarios(SAFETY_SCENARIO_DIR, SAFETY["prefix"])
    assert safety == generate(SAFETY["n"], SAFETY["seed"], SAFETY["full_scope"], SAFETY["prefix"], SAFETY["injections"])
    assert len(safety) == SAFETY["n"] and all(s.id.startswith("saf_") for s in safety)
    test_seeds = {s.seed for s in load_scenarios(TEST_SCENARIO_DIR, TEST["prefix"])}
    assert not {s.seed for s in safety} & (test_seeds | {s.seed for s in generate()})
    plant, templates = load_plant(), set()
    for s in safety:
        (line,) = [f for f in s.faults if f.type == FaultType.PROMPT_INJECTION]
        assert has_injection(s) and s.answer_key == s.expected_answer_key() and s.answer_key.root_causes
        assert not any(label.startswith("PROMPT_INJECTION") for label in s.answer_key.root_causes)
        others = [f.target for f in s.faults if f is not line]
        assert line.target not in others or line.target == plant.weather_station_id
        assert "PROMPT_INJECTION" in (SAFETY_SCENARIO_DIR / f"{s.id}.yaml").read_text(encoding="utf-8").split("\n", 3)[2]
        inj = FaultInjector.for_scenario(s, plant)
        inj.run_to_end()
        (posted,) = inj.injections
        templates.add(next(k for k, t in enumerate(INJECTION_TEMPLATES) if posted.text.startswith(t.text[:20])))
    assert len(templates) == len(INJECTION_TEMPLATES)  # every attack text is in the set


def test_injections_only_on_request():
    assert not any(has_injection(s) for s in generate(n=30, seed=11, full_scope=True))
    assert all(FaultType.PROMPT_INJECTION not in {f.type for f in storm_scenario(k, full_scope=True).faults}
               for k in range(12))


def test_committed_practice_unknown_set_is_frozen():
    """scenarios/practice_unknown/: 10 scenarios, one unknown fault each (all three types) plus 0-2 known
    faults. The router and the unknown-fault handling are tuned on it."""
    from raqeeb.config import PRACTICE_UNKNOWN_SCENARIO_DIR
    from raqeeb.contracts import UNKNOWN_FAULTS
    from raqeeb.faults.generator import PRACTICE_UNKNOWN

    scenarios = load_scenarios(PRACTICE_UNKNOWN_SCENARIO_DIR, PRACTICE_UNKNOWN["prefix"])
    assert scenarios == generate_set("practice_unknown")
    assert len(scenarios) == 10 and all(s.id.startswith("pru_") for s in scenarios)
    others = load_scenarios(PRACTICE2_SCENARIO_DIR, "pr2") + load_scenarios(TEST_SCENARIO_DIR, TEST["prefix"])
    assert not {s.seed for s in scenarios} & {s.seed for s in others}
    unknown = Counter(f.type for s in scenarios for f in s.faults if f.type in UNKNOWN_FAULTS)
    assert set(unknown) == UNKNOWN_FAULTS and sum(unknown.values()) == 10
    for s in scenarios:
        known = [f for f in s.faults if f.type not in UNKNOWN_FAULTS]
        assert len(known) <= 2 and not any(f.type in DECOY_FAULTS for f in known)
        assert s.answer_key == s.expected_answer_key()
