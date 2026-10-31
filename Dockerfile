# Raqeeb console for a public demo (e.g. a Hugging Face Docker Space): rules-only live runs and replays.
# No secrets, no .env, no model server: the local-model mode is switched off (RAQEEB_UI_NO_LLM) and so are
# email alerts (RAQEEB_ALERTS=0).
# Network is used only while building (base image, uv, Python packages); the running app makes no calls.
#   docker build -t raqeeb-console .
#   docker run --rm -p 7860:7860 raqeeb-console        -> http://localhost:7860
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    HOME=/home/user \
    RAQEEB_UI_NO_LLM=1     RAQEEB_ALERTS=0 \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# Hugging Face runs Docker Spaces as uid 1000
RUN useradd -m -u 1000 user
WORKDIR /home/user/app

# Dependencies first (cached layer), then the project itself (editable, so paths resolve to this folder)
COPY --chown=user pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY --chown=user . .
RUN uv sync --frozen --no-dev && chown -R user:user /home/user/app

USER user
EXPOSE 7860
# XSRF protection off: Spaces serve the app inside an iframe, where it blocks replay uploads.
# browser.serverAddress set: otherwise a headless Streamlit looks up the machine's external IP at start.
CMD [".venv/bin/streamlit", "run", "raqeeb/ui/app.py", \
     "--server.port=7860", "--server.address=0.0.0.0", "--server.headless=true", \
     "--server.enableXsrfProtection=false", "--server.fileWatcherType=none", \
     "--browser.gatherUsageStats=false", "--browser.serverAddress=localhost"]
