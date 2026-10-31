"""Model selection (sidebar): the local server's model list (faked, no network), the router built with the
chosen model name only, the model in the run's header label and recording label, and the selector showing
"Rules only" plus a disabled API tier when the model is switched off."""

from __future__ import annotations

import io
import json

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from raqeeb.config import REPO_ROOT, Settings
from raqeeb.ui import live, source
from raqeeb.ui.runlog import RECORDED_LABEL, Mode
from raqeeb.ui.testmodel import scripted_router

APP = REPO_ROOT / "raqeeb" / "ui" / "app.py"


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_server(monkeypatch, models: list[str], base="http://127.0.0.1:9/v1", default="qwen-a"):
    monkeypatch.delenv(live.NO_LLM_ENV, raising=False)
    monkeypatch.setattr(live, "load_settings",
                        lambda: Settings(local_llm_base_url=base, local_llm_model=default))
    seen = []

    def fake_urlopen(url, timeout=None):
        seen.append(url)
        return _Resp(json.dumps({"object": "list", "data": [{"id": m, "object": "model"} for m in models]}).encode())

    monkeypatch.setattr(live.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_list_local_models_reads_the_servers_list(monkeypatch):
    seen = _fake_server(monkeypatch, ["other-b", "qwen-a", "zeta"])
    assert live.list_local_models() == ["qwen-a", "other-b", "zeta"]  # the .env model first, then by name
    assert seen == ["http://127.0.0.1:9/v1/models"]


def test_list_local_models_is_empty_when_off_or_unreachable(monkeypatch):
    monkeypatch.setenv(live.NO_LLM_ENV, "1")
    assert live.list_local_models() == []
    monkeypatch.delenv(live.NO_LLM_ENV)
    monkeypatch.setattr(live, "load_settings", lambda: Settings())  # no server in .env
    assert live.list_local_models() == []

    def down(url, timeout=None):
        raise OSError("refused")

    monkeypatch.setattr(live, "load_settings", lambda: Settings(local_llm_base_url="http://127.0.0.1:9/v1",
                                                                local_llm_model="m"))
    monkeypatch.setattr(live.urllib.request, "urlopen", down)
    assert live.list_local_models() == []


def test_router_gets_only_the_chosen_model_name(monkeypatch):
    monkeypatch.setattr(live, "load_settings",
                        lambda: Settings(local_llm_base_url="http://127.0.0.1:9/v1", local_llm_model="qwen-a"))
    router = live.make_router(Mode.LOCAL, "other-b")
    assert router.gateway.settings.local_llm_model == "other-b"
    assert router.gateway.model_name("local") == "openai/other-b" and router.api is False
    assert router.gateway.settings.local_llm_base_url == "http://127.0.0.1:9/v1"  # the server is unchanged
    assert live.make_router(Mode.LOCAL).gateway.settings.local_llm_model == "qwen-a"
    assert live.make_router(Mode.RULES, "other-b") is None
    assert live.model_name(Mode.LOCAL, "other-b") == "other-b" and live.model_name(Mode.LOCAL) == "qwen-a"
    assert live.model_name(Mode.RULES, "other-b") == ""


def test_live_run_labels_carry_the_chosen_model(tmp_path):
    run = source.start_live("scn_0002", Mode.LOCAL, router=scripted_router(), model="qwen-b",
                            db_path=tmp_path / "run.db")
    assert run.log.model == "qwen-b" and source.mode_label(run) == "Live: rules + local model (qwen-b)"
    run.run_to_end(approve=True)
    assert run.recording().label == f"{RECORDED_LABEL[Mode.LOCAL]} (qwen-b)"
    assert run.recording("custom").label == "custom"
    run.close()


@pytest.fixture
def _fresh_cache():
    st.cache_data.clear()  # local_models() is cached process-wide for a minute
    yield
    st.cache_data.clear()


def test_sidebar_selector_offline_shows_rules_only_and_a_disabled_api_tier(_fresh_cache):
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    at.radio(key="mode_choice").set_value(live.MODE_LABEL[Mode.RULES]).run()
    box = at.selectbox(key="model_choice")
    assert box.options == ["Rules only"] and box.value == "Rules only"
    api = at.button(key="api_tier")
    assert api.label == "API tier (not configured)" and api.disabled
    assert "not configured" in (api.help or "")
    at.radio(key="mode_choice").set_value("Replay").run()
    assert not [b for b in at.selectbox if b.key == "model_choice"]  # not shown for replays
    assert not at.exception


def test_sidebar_selector_lists_the_servers_models(monkeypatch, _fresh_cache):
    _fake_server(monkeypatch, ["qwen-a", "other-b"])
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    at.radio(key="mode_choice").set_value(live.MODE_LABEL[Mode.LOCAL]).run()
    box = at.selectbox(key="model_choice")
    assert box.options == ["Rules only", "qwen-a", "other-b"] and box.value == "qwen-a"
    box.set_value("other-b").run()
    assert at.session_state.model_choice == "other-b" and at.session_state.mode_choice == live.MODE_LABEL[Mode.LOCAL]
    at.selectbox(key="model_choice").set_value("Rules only").run()
    assert at.session_state.mode_choice == live.MODE_LABEL[Mode.RULES]  # the two controls agree
    assert not at.exception
