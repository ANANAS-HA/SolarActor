"""A live run driven by a background thread, paced in agent turns per second.

    runner = Runner(live_run)          # kept in st.session_state: reruns and clicks never restart it
    runner.resume(turns_per_s=2)       # starts the thread on first use
    runner.pause(); runner.jump_to_end()
    runner.decide(action_id, approve, note)    # the operator's decision, through LiveRun.decide

The thread only calls LiveRun.poll() (one agent turn) and stops by itself while an action waits for
approval or when the run has ended. Every poll and every decision happens under `lock` (re-entrant); the
console takes the same lock while it reads the run, so a page never sees half a turn. No Streamlit call
is made from the thread.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from raqeeb.ui.live import LiveRun

PACES = (0.5, 1.0, 2.0, 4.0, 8.0)  # agent turns per second
DEFAULT_PACE = 2.0
IDLE_S = 0.1  # how often a paused or waiting runner checks again
FAST_GAP_S = 0.02  # pause between turns when jumping to the end (lets the page take the lock)


@dataclass
class RunnerState:
    running: bool  # the thread is advancing the run (not paused, not waiting, not finished)
    paused: bool
    waiting: bool  # an action waits for an operator decision
    finished: bool
    fast: bool  # jumping to the end (no pause between turns)
    turns: int
    error: str


class Runner:
    def __init__(self, run: LiveRun, turns_per_s: float = DEFAULT_PACE):
        self.run = run
        self.lock = threading.RLock()
        self.turns_per_s = turns_per_s
        self._paused = True
        self._fast = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error = ""

    # -- controls (called from the page) ------------------------------------------------------

    def resume(self, turns_per_s: float | None = None) -> None:
        if turns_per_s is not None:
            self.turns_per_s = turns_per_s
        self._paused = False
        self._start()

    def pause(self) -> None:
        self._paused, self._fast = True, False

    def jump_to_end(self) -> None:
        """Run without pausing between turns; still stops at every action that needs approval."""
        self._fast = True
        self.resume()

    def set_pace(self, turns_per_s: float) -> None:
        self.turns_per_s = turns_per_s

    def decide(self, action_id: str, approve: bool, note: str = "") -> None:
        with self.lock:
            self.run.decide(action_id, approve, note)

    def stop(self, timeout: float = 5.0) -> None:
        """End the thread (a new run replaces this one). Waits for the current turn to finish."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    # -- state ---------------------------------------------------------------------------------

    @property
    def active(self) -> bool:
        """The page should keep refreshing: the thread is (or is about to be) advancing the run."""
        s = self.state()
        return s.running

    def state(self) -> RunnerState:
        with self.lock:
            waiting, finished, turns = bool(self.run.waiting), self.run.finished, len(self.run.log.polls)
        alive = self._thread is not None and self._thread.is_alive()
        return RunnerState(running=alive and not self._paused and not waiting and not finished, paused=self._paused,
                           waiting=waiting, finished=finished, fast=self._fast, turns=turns, error=self.error)

    # -- the thread ----------------------------------------------------------------------------

    def _start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name=f"raqeeb-runner-{self.run.incident.id}",
                                            daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self.lock:
                done = self.run.finished
                hold = self._paused or bool(self.run.waiting) or self.run.polled_out
                if not done and not hold:
                    try:
                        self.run.poll()
                    except Exception as exc:  # noqa: BLE001 - shown on the page; the thread ends
                        self.error = f"{type(exc).__name__}: {exc}"
                        self._paused = True
                        return
            if done:
                return
            # Between turns, without the lock: the page can read the run. Fast mode still yields briefly.
            time.sleep(IDLE_S if hold else FAST_GAP_S if self._fast else 1.0 / max(self.turns_per_s, 0.01))
