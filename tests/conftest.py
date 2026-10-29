from functools import lru_cache

import pytest

from tests import tool_scenarios


@lru_cache
def _cached(name: str):
    return tool_scenarios.build(name)


@pytest.fixture(scope="session")
def scenario():
    """scenario(name) -> (store, twin), built once per session. Do not write to these stores."""
    return _cached
