"""Record a console run as a replay file (format: ui/runlog.py).

    uv run python -m raqeeb.ui.record --scenario scn_0007 --mode local     # on the PC with the model server
    uv run python -m raqeeb.ui.record --scenario scn_0007 --mode test      # scripted test model, no server
    uv run python -m raqeeb.ui.record --scenario scn_0007 --mode rules [--reject] [--out path.json]

Scenarios come from the practice sets (or "storm_<seed>"). Gated actions are decided by the
recording operator: approved by default, rejected with --reject; the note says it was a recording.
Files go to raqeeb/ui/replays/ by default, which the console lists in Replay mode. A run can also
be saved from the console itself ("Save recording" after a live run ends).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from raqeeb.ui import source
from raqeeb.ui.live import local_model_status
from raqeeb.ui.runlog import Mode

NOTE = "decided by the recording script (recorded run)"


def record(scenario_id: str, mode: Mode, out: Path | None = None, approve: bool = True,
           label: str | None = None) -> Path:
    run = source.start_live(scenario_id, mode)
    try:
        run.run_to_end(approve=approve, note=NOTE)
        path = out or source.REPLAY_DIR / f"{scenario_id}_{mode.value}.json"
        return run.recording(label).save(path)
    finally:
        run.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", required=True, help="practice scenario id, e.g. scn_0007, or storm_<seed>")
    parser.add_argument("--mode", choices=[m.value for m in Mode], default=Mode.RULES.value)
    parser.add_argument("--reject", action="store_true", help="reject every gated action (default: approve)")
    parser.add_argument("--label", help="label shown in the console (default from the mode)")
    parser.add_argument("--out", type=Path, help="output file (default raqeeb/ui/replays/<scenario>_<mode>.json)")
    args = parser.parse_args()
    mode = Mode(args.mode)
    if mode == Mode.LOCAL:
        available, reason = local_model_status()
        if not available:
            parser.error(f"local model unavailable: {reason}")
    path = record(args.scenario, mode, args.out, approve=not args.reject, label=args.label)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
