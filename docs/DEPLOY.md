# Deploying the Raqeeb console (Hugging Face Docker Space)

The public demo runs the console from the `Dockerfile` in the repository root:

- **Live: rules only** and **Replay** work. The local-model mode is off (`RAQEEB_UI_NO_LLM=1`): the Space has
  no model server and no API keys.
- The image holds no secrets and no `.env`. The running app makes no network calls: Streamlit usage
  statistics are off and LiteLLM uses its bundled price table. Network is used only while the image builds.
- `.dockerignore` keeps `scenarios/test`, `scenarios/final`, `scenarios/safety`, `tests/` and `handoffs/` out
  of the image. The console lists only the practice sets (`scenarios/scn_*`, `scenarios/practice2/pr2_*`).
- Weather: `data/weather/nasa_power_24.71_46.68_2025.csv` (NASA POWER, hourly, community RE). The UI footer
  credits the NASA Langley Research Center POWER Project, as POWER asks users to do.

Everything below is done by you. Claude does not log in anywhere and never handles your token.

## 1. Try the image locally (optional, about 5 minutes)

```powershell
cd <repo root>
docker build -t raqeeb-console .
docker run --rm -p 7860:7860 raqeeb-console
```

Open http://localhost:7860, choose **Live: rules only**, press **Start live run**, then **To end**. Approve the
power limit when the run pauses, then press **To end** again until the run finishes. Stop the container with Ctrl+C.

## 2. Create the Space and a token (on huggingface.co)

1. Sign in, then **New Space**: pick a name (e.g. `raqeeb-console`), **SDK: Docker**, template **Blank**,
   hardware **CPU basic (free)**, visibility **Public**. Create it.
2. **Settings → Access Tokens → Create new token**: type **Fine-grained**, give it **write** access to
   that Space only. Copy it once; do not paste it into any file in the repository.

## 3. Log in and upload (PowerShell, from the repo root on the branch you want to publish)

`hf` is the Hugging Face CLI. `uvx` runs it without installing it into the project.

```powershell
uvx --from huggingface_hub hf auth login          # paste the token when asked; answer "n" to git credential
$space = "<your-user>/<space-name>"

# The Space's README carries its settings (sdk: docker, app_port: 7860)
uvx --from huggingface_hub hf upload $space docs/SPACE_README.md README.md --repo-type space

# The app itself. No git history is uploaded; secrets and held-out sets are excluded.
uvx --from huggingface_hub hf upload $space . . --repo-type space `
  --exclude ".env" ".env.*" ".venv/*" "**/__pycache__/*" ".pytest_cache/*" "data/*.db" "data/*.db-*" `
            "data/plots/*" "scenarios/test/*" "scenarios/final/*" "scenarios/safety/*" "tests/*" "handoffs/*" `
            ".claude/*" ".vscode/*" "README.md" "CLAUDE.md" `
  --commit-message "Raqeeb console"
```

The Space builds the image (a few minutes; the log is on the Space page under **Logs**). When the status
shows **Running**, the public URL is `https://huggingface.co/spaces/<your-user>/<space-name>`.

Before uploading, check what `bench/` holds: every folder there is shown on the Results tab. `bench/test/results.csv`
lists the round 1 test scenarios with their root causes; leave it out (`--exclude "bench/test/*"`) if that set
should stay private.

## 4. Updates

- **Final benchmark results.** The Results tab reads `bench/final/` by default (summary.csv, results.csv,
  summary.md) and shows "results not available yet" until it exists. After the final run:

  ```powershell
  uvx --from huggingface_hub hf upload $space bench/final bench/final --repo-type space --commit-message "Final results"
  ```

  Another folder or file can be set with the environment variable `RAQEEB_RESULTS` (Space **Settings →
  Variables**), or typed into the Results tab.

- **Replays recorded with the local model.** On the PC with the model server running (`.env` set):

  ```powershell
  uv run python -m raqeeb.ui.record --scenario scn_0007 --mode local --label "recorded with the local model (<model name>)"
  ```

  This writes `raqeeb/ui/replays/scn_0007_local.json`. A run made in the console can be saved too: after a live
  run ends, **Save recording (replay JSON)** in the sidebar. Upload the file:

  ```powershell
  uvx --from huggingface_hub hf upload $space raqeeb/ui/replays/scn_0007_local.json raqeeb/ui/replays/scn_0007_local.json --repo-type space
  ```

  The replays now shipped are labelled "recorded with test model" (a scripted stand-in, not a language model).
  Delete them from the Space when the real ones are there, or keep them:
  `uvx --from huggingface_hub hf repo-files delete $space raqeeb/ui/replays/scn_0007_test.json --repo-type space`
  (older CLI: `huggingface-cli repo-files <space> delete <file> --repo-type space`). To make another file the
  default, change `DEFAULT_REPLAY` in `raqeeb/ui/source.py`.

## 5. After the hackathon

```powershell
uvx --from huggingface_hub hf auth logout
```

Then delete the token under **Settings → Access Tokens**.

## Notes

- Port: the container listens on 7860 (what Docker Spaces expect; `app_port` in the Space README).
- Run files: each live run writes its telemetry to its own SQLite file in the system temp folder
  (`RAQEEB_UI_RUN_DIR` to change it), never to `data/raqeeb.db`; the file is deleted when the run is replaced.
- XSRF protection is off in the image because Spaces show the app in an iframe, where it blocks file uploads.
  Uploads are limited to 20 MB and must be valid replay files.
- If the build fails on `uv sync --frozen`, `uv.lock` is out of date: run `uv lock` locally, commit, upload again.
