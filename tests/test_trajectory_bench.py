"""Bench-side trajectories: the harness records a RunLog per run through the console's Recorder, the bench
CLI writes it as a file (gzip over 1 MB), the gateway's transcripts are opt-in and reach the poll record,
and nothing from the answer key is in a poll's agent-side data."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import pytest

from raqeeb.agent import RaqeebAgent
from raqeeb.bench import run as bench_run
from raqeeb.bench.fakes import EmptyAgent
from raqeeb.bench.harness import ReferenceEnergy, reference_energy, run_scenario
from raqeeb.config import Settings
from raqeeb.contracts import TABLE_MODELS, LlmCall, LlmRole, Scenario
from raqeeb.llm.gateway import TRANSCRIPTS_ENV, Gateway, LlmError
from raqeeb.ui.runlog import GateDecision, Mode, RunLog
from raqeeb.ui.testmodel import TEST_BASE_URL, TEST_MODEL, ScriptedModel, scripted_router
from tests.leakcheck import assert_no_answer_leak
from tests.test_trajectory import INPUT_FIELDS

FIXTURE = Path(__file__).parent / "fixtures" / "scn_example.yaml"
FORBIDDEN_KEYS = ("faults", "answer_key", "severity", "seed", "truth", "mitigated")  # mitigated = bench truth


@lru_cache
def scenario() -> Scenario:
    return Scenario.from_yaml(FIXTURE)


@lru_cache
def reference() -> ReferenceEnergy:
    return reference_energy(scenario())


@pytest.fixture(scope="module")
def rules_record():
    return run_scenario(scenario(), RaqeebAgent(name="raqeeb_rules"), reference=reference(), trajectory=True)


def _poll_dump(poll) -> dict:
    return json.loads(poll.model_dump_json())


def _check_agent_side(log: RunLog, scn: Scenario) -> None:
    assert_no_answer_leak(log.incident, scn, strict=True)
    for poll in log.polls:
        dumped = _poll_dump(poll)
        for field in INPUT_FIELDS:
            assert_no_answer_leak(dumped[field], scn)
        dumped.pop("transcripts", None)  # prompt text is built from the fields checked above
        text = json.dumps(dumped)
        for key in FORBIDDEN_KEYS:
            assert f'"{key}":' not in text, (poll.ts, key)


# -- the harness fills a RunLog the way the console does -------------------------------------------


def test_harness_trajectory_matches_the_record(rules_record):
    log = rules_record.trajectory
    assert isinstance(log, RunLog) and log.finished
    assert (log.mode, log.agent, log.model, log.poll_min) == (Mode.BENCH, "raqeeb_rules", "", 15)
    assert log.incident == scenario().agent_view()
    assert len(log.polls) == len(rules_record.turns) == len(log.bench.polls)
    assert [p.ts for p in log.polls] == [t.ts for t in rules_record.turns] == [m.ts for m in log.bench.polls]
    assert [m.mitigated for m in log.bench.polls] == [t.mitigated for t in rules_record.turns]
    assert log.bench.energy_mwh == rules_record.energy_mwh
    assert (log.bench.no_action_mwh, log.bench.decoy_only_mwh) == (reference().no_action_mwh, reference().decoy_only_mwh)
    assert log.bench.storms == rules_record.storms and not log.bench.control
    assert sorted(log.answer_key.root_causes) == sorted(rules_record.root_causes)
    # one entry per request, decided as the harness decided it
    assert [a.request.id for a in log.actions] == [a.request.id for a in rules_record.actions]
    for entry, action in zip(log.actions, rules_record.actions):
        assert (entry.status, entry.gated, entry.note) == (action.status, action.gated, action.note)
        assert (entry.requested_at, entry.decided_at, entry.effective_at) == (action.ts, action.ts, action.effective_at)
        assert entry.decided_by == ("human:simulated" if action.gated else "auto")
        assert entry.cause and entry.cause in entry.request.rationale
    assert any(a.gated for a in log.actions)
    # what the agent returned per poll is what the scorer's record kept
    assert [v for p in log.polls for v in p.verifications] == [v for _, v in rules_record.verifications]
    assert [r.id for p in log.polls for r in p.actions] == [a.request.id for a in rules_record.actions]
    assert [e for p in log.polls for e in p.escalations] == rules_record.escalations
    assert next(p.report for p in reversed(log.polls) if p.report is not None) == rules_record.report
    assert sorted({label for h in log.diagnosis_until(log.polls[-1].ts).root_causes for label in h.labels}) \
        == rules_record.predicted
    assert set(log.telemetry) == set(TABLE_MODELS) and len(log.telemetry["inverter_readings"]) > 1000
    assert len(log.telemetry["audit"]) == len(rules_record.audit)
    # the v3 fields are filled as for a console run
    assert all(p.checks for p in log.polls) and any(p.candidates and p.symptoms for p in log.polls)
    assert {g.decision for p in log.polls for g in p.gate} >= {GateDecision.AUTO, GateDecision.NEEDS_APPROVAL}


def test_no_model_means_empty_calls_and_transcripts(rules_record):
    assert not rules_record.llm_calls
    assert all(not p.llm_calls and not p.transcripts for p in rules_record.trajectory.polls)


def test_agent_side_poll_data_leaks_nothing(rules_record):
    _check_agent_side(rules_record.trajectory, scenario())
    # bench truth and the answer key only in their own sections
    assert rules_record.trajectory.bench is not None and rules_record.trajectory.answer_key is not None


def test_an_agent_without_memory_gets_a_bare_trajectory():
    record = run_scenario(scenario(), EmptyAgent(), reference=reference(), trajectory=True)
    log = record.trajectory
    assert log.finished and log.agent == "empty" and log.bench is not None and not log.actions
    assert len(log.polls) == len(record.turns)
    assert all(not (p.checks or p.symptoms or p.candidates or p.gate or p.timeline or p.llm_calls or p.transcripts)
               for p in log.polls)


def test_without_the_flag_nothing_is_recorded():
    record = run_scenario(scenario(), EmptyAgent(), reference=reference())
    assert record.trajectory is None and record.control_trajectory is None


# -- files -----------------------------------------------------------------------------------------


def test_files_are_written_and_load_back(rules_record, tmp_path):
    paths = bench_run.write_trajectories(rules_record, tmp_path)
    assert len(paths) == 1 and paths[0].name.startswith("scn_0007__raqeeb_rules.json")
    size = len(rules_record.trajectory.to_json().encode("utf-8"))
    assert paths[0].suffix == (".gz" if size > bench_run.TRAJECTORY_GZIP_BYTES else ".json")
    # in memory the tool values hold datetimes; on disk they are strings, so compare after one JSON pass
    assert RunLog.load(paths[0]) == RunLog.from_json(rules_record.trajectory.to_json())
    small = rules_record.trajectory.model_copy(update={"telemetry": {}, "polls": rules_record.trajectory.polls[:2]})
    path = bench_run.write_trajectory(small, tmp_path, "small")
    assert path.suffix == ".json" and RunLog.load(path) == RunLog.from_json(small.to_json())
    assert bench_run.trajectory_stem("scn_0001", "raqeeb_rules", control=True) == "scn_0001__raqeeb_rules__control"
    bare = run_scenario(scenario(), EmptyAgent(), reference=reference())
    assert bench_run.write_trajectories(bare, tmp_path) == []


# -- transcripts -------------------------------------------------------------------------------------


def test_gateway_transcripts_are_opt_in(monkeypatch):
    settings = Settings(local_llm_base_url=TEST_BASE_URL, local_llm_model=TEST_MODEL)
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    monkeypatch.delenv(TRANSCRIPTS_ENV, raising=False)
    off = Gateway(settings, completion=ScriptedModel())
    off.complete(LlmRole.LOCAL, "hypotheses", messages)
    assert off.calls and off.transcripts == [] and not off.keep_transcripts  # default: nothing kept
    on = Gateway(settings, completion=ScriptedModel(), keep_transcripts=True)
    text, _ = on.complete(LlmRole.LOCAL, "hypotheses", messages)
    assert on.transcripts == [{"purpose": "hypotheses", "prompt": "[system]\ns\n\n[user]\nu", "reply": text}]
    assert len(on.transcripts) == len(on.calls) == 1
    monkeypatch.setenv(TRANSCRIPTS_ENV, "1")
    assert Gateway(settings, completion=ScriptedModel()).keep_transcripts
    assert not Gateway(settings, completion=ScriptedModel(), keep_transcripts=False).keep_transcripts
    monkeypatch.setenv(TRANSCRIPTS_ENV, "0")
    assert not Gateway(settings, completion=ScriptedModel()).keep_transcripts

    def failing(**kwargs):
        raise ValueError("bad request")

    broken = Gateway(settings, completion=failing, keep_transcripts=True)
    with pytest.raises(LlmError):
        broken.complete(LlmRole.LOCAL, "explain", messages)
    assert len(broken.calls) == len(broken.transcripts) == 1 and broken.transcripts[0]["reply"] == ""
    assert not {"prompt", "reply", "transcript"} & set(LlmCall.model_fields)  # the contract is untouched


def test_transcripts_reach_the_poll_record():
    router = scripted_router()
    router.gateway.keep_transcripts = True
    record = run_scenario(scenario(), RaqeebAgent(router=router, name="raqeeb_test"), reference=reference(),
                          trajectory=True)
    log = record.trajectory
    assert log.model == "test model"
    assert record.llm_calls and len(record.llm_calls) == len(router.gateway.calls)
    assert sum(len(p.llm_calls) for p in log.polls) == len(record.llm_calls)
    for poll in log.polls:
        assert len(poll.transcripts) == len(poll.llm_calls)
        for transcript, call in zip(poll.transcripts, poll.llm_calls):
            assert transcript.purpose == call.purpose
            assert "<untrusted_data" in transcript.prompt and transcript.reply
    _check_agent_side(log, scenario())
