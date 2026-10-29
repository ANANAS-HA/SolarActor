import pytest

from raqeeb.config import load_settings
from raqeeb.llm.smoke import ping

settings = load_settings()


@pytest.mark.llm
@pytest.mark.skipif(
    not settings.local_llm_configured,
    reason="local LLM not configured: set LOCAL_LLM_BASE_URL and LOCAL_LLM_MODEL in .env",
)
def test_local_llm_answers():
    result = ping(settings, role="local")
    assert result.reply
