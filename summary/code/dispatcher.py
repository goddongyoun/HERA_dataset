"""Preset Dispatcher: executes LLM-generated JSON presets against a dm_control env."""
import asyncio
import json
import os
import queue
import re
import threading
import time
from datetime import datetime
from pathlib import Path
import numpy as np
import ollama

LOGS_DIR = Path(__file__).parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)

# Single-slot enforcement, take 2: a lock alone was found insufficient --
# whichever thread happened to be scheduled first still won the race to even
# *attempt* acquisition, and the mission asyncio-loop thread was observed to
# be starved of GIL time for seconds at a stretch by the physics simulation
# thread + a competing synchronous streaming thread (a GIL convoy effect,
# confirmed via direct chunk-timestamp instrumentation; reducing
# sys.setswitchinterval() did not fix it either).
#
# Fix: route every Ollama call (mission planning and preset generation, across
# all experiment scripts) through ONE dedicated worker thread with ONE
# request queue. Submitting a request is a single fast, thread-safe
# queue.put() -- not dependent on the submitting thread winning any further
# scheduling race -- and the worker processes requests strictly in submission
# order, one at a time. This deterministically emulates a single-slot
# inference backend (as an edge device without concurrent LLM sessions would
# actually have), independent of client-side thread/GIL scheduling.
class _OllamaWorker:
    def __init__(self):
        self._queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True, name="OllamaWorker").start()

    def submit(self, messages, model, use_json_format, temperature,
               extra_latency, cancel_event) -> str | None:
        """Enqueue a request and block the calling (worker) thread until it's
        processed. Returns generated content, or None if cancelled/failed."""
        done = threading.Event()
        result: dict = {}
        self._queue.put((messages, model, use_json_format, temperature,
                         extra_latency, cancel_event, result, done))
        done.wait()
        return None if result.get("error") else result.get("content")

    async def submit_async(self, messages, model, use_json_format, temperature,
                           extra_latency, cancel_event) -> str | None:
        """Async-friendly submit: the enqueue itself is a single fast,
        thread-safe call made directly from the coroutine (no separate thread
        needs to be scheduled first); completion is then polled without
        blocking the event loop, so the wait remains cancellable via
        asyncio.Task.cancel()."""
        done = threading.Event()
        result: dict = {}
        self._queue.put((messages, model, use_json_format, temperature,
                         extra_latency, cancel_event, result, done))
        while not done.is_set():
            await asyncio.sleep(0.05)
        return None if result.get("error") else result.get("content")

    def clear_pending(self):
        """Discard any not-yet-started requests currently sitting in the
        queue (e.g. a mission-planning call that was just cancelled). Does
        not affect a request the worker has already begun processing --
        that one is stopped via its own cancel_event instead."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            result, done = item[6], item[7]
            result["error"] = "flushed"
            done.set()

    # Safety net: an individual request should never be allowed to hang the
    # single shared worker indefinitely (observed with some small/fast models
    # where a stream occasionally yields no chunks at all and neither
    # completes nor raises). If exceeded, the worker gives up on this request
    # and moves on to the next queued item; the stuck call itself is left to
    # finish (or not) in a detached thread rather than being force-killed.
    # The budget must scale with the request's own extra_latency (used by the
    # latency-robustness experiments) -- a fixed 25s timeout would misfire as
    # soon as extra_latency approached it, since every such request is
    # guaranteed to take at least extra_latency seconds by design.
    REQUEST_TIMEOUT_BASE_SEC = 25.0

    def _run(self):
        while True:
            (messages, model, use_json_format, temperature,
             extra_latency, cancel_event, result, done) = self._queue.get()
            if cancel_event is not None and cancel_event.is_set():
                result["error"] = "cancelled-before-start"
                done.set()
                continue
            inner_done = threading.Event()
            t = threading.Thread(
                target=self._do_request,
                args=(messages, model, use_json_format, temperature,
                     extra_latency, cancel_event, result, inner_done),
                daemon=True, name="OllamaWorker-request",
            )
            t.start()
            timeout = max(self.REQUEST_TIMEOUT_BASE_SEC, extra_latency + self.REQUEST_TIMEOUT_BASE_SEC)
            t.join(timeout=timeout)
            if not inner_done.is_set():
                print(f"[OllamaWorker] WARNING: request exceeded {timeout:.0f}s, "
                      f"abandoning (stuck call left to finish in background, if ever)")
                result["error"] = "worker-timeout"
            done.set()

    def _do_request(self, messages, model, use_json_format, temperature,
                    extra_latency, cancel_event, result, inner_done):
        try:
            kwargs = dict(model=model, messages=messages,
                          options={"temperature": temperature}, think=False, stream=True)
            if use_json_format:
                kwargs["format"] = "json"
            stream = ollama.chat(**kwargs)
            content = ""
            cancelled = False
            for chunk in stream:
                if cancel_event is not None and cancel_event.is_set():
                    stream.close()
                    cancelled = True
                    break
                content += chunk["message"]["content"]
            if cancelled:
                result["error"] = "cancelled-mid-stream"
            else:
                if extra_latency > 0:
                    time.sleep(extra_latency)
                result["content"] = content
        except Exception as e:
            result["error"] = str(e)
        finally:
            inner_done.set()


OLLAMA_WORKER = _OllamaWorker()


def _log_llm(situation: str, preset: dict | None, elapsed: float, interrupted: bool = False):
    entry = {
        "timestamp": datetime.now().isoformat(),
        "elapsed_sec": round(elapsed, 3),
        "interrupted": interrupted,
        "situation": situation,
        "preset": preset,
    }
    log_path = LOGS_DIR / f"llm_{datetime.now().strftime('%Y%m%d')}.jsonl"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

MODEL = os.environ.get("HERA_MODEL", "qwen3.5:9b")

SYSTEM_PROMPT = """You are a robot motion controller for a quadruped robot (like a dog or cheetah).
Given a situation description, generate a JSON motion preset.

Joint layout (12 joints, values in [-1.0, 1.0]):
  Indices 0,1,2  : front-left  leg (hip_abduct, hip_flex, knee)
  Indices 3,4,5  : front-right leg (hip_abduct, hip_flex, knee)
  Indices 6,7,8  : back-left   leg (hip_abduct, hip_flex, knee)
  Indices 9,10,11: back-right  leg (hip_abduct, hip_flex, knee)

Joint conventions:
  hip_abduct: 0.0 = neutral, positive = outward splay
  hip_flex:   positive = forward swing (leg extends forward)
  knee:       negative = bent (weight-bearing), positive = extended (swing)

Trot gait (diagonal pairs move together):
  Phase A - pair (front-left, back-right) swing forward, pair (front-right, back-left) push:
    [0.0,  0.4, 0.3,   0.0, -0.3, -0.5,   0.0, -0.3, -0.5,   0.0,  0.4, 0.3]
  Phase B - opposite diagonal swings:
    [0.0, -0.3, -0.5,  0.0,  0.4, 0.3,    0.0,  0.4, 0.3,    0.0, -0.3, -0.5]
  Use duration_steps: 10 per phase for fast trot, 20 for slow trot.

Stand still (neutral, all feet on ground):
  [0.0, 0.0, -0.4,  0.0, 0.0, -0.4,  0.0, 0.0, -0.4,  0.0, 0.0, -0.4]

The JSON must follow this schema exactly:
{
  "type": "loop" or "one-shot",
  "skill_name": string,
  "actions": [
    {"joint_targets": [12 floats between -1.0 and 1.0], "duration_steps": int}
  ],
  "stop_condition": string,
  "max_cycles": int (only if type is "loop", else null)
}

- For "loop": actions cycle repeatedly until max_cycles reached (use 500+ for long behaviors)
- For "one-shot": actions execute once then done
"""


def generate_preset(situation: str, cancel_event=None,
                    model: str | None = None, extra_latency: float = 0.0) -> dict | None:
    """Generate a preset from LLM.

    cancel_event=None  → blocking call (Baseline C path)
    cancel_event=Event → streaming path with interrupt support (HERA path)
    model              → override global MODEL (for LLM comparison experiments)
    extra_latency      → artificial sleep after generation (for latency experiments)
    """
    _model = model or MODEL
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": situation},
    ]

    # Every call -- blocking (Baseline C) or interruptible (HERA) -- is routed
    # through the single shared OLLAMA_WORKER so it is genuinely processed in
    # submission order relative to mission-planning calls (see _OllamaWorker).
    content = OLLAMA_WORKER.submit(messages, _model, True, 0.2, extra_latency, cancel_event)
    if content is None:
        if cancel_event is not None and cancel_event.is_set():
            print("[LLM] Interrupted -- discarding partial/pending response")
        else:
            print("[LLM] Request failed")
        return None

    m = re.search(r'\{.*\}', content, re.DOTALL)
    try:
        result = json.loads(m.group() if m else content)
    except json.JSONDecodeError as e:
        print(f"[LLM] JSON parse failed: {e}")
        return None
    return result


# Sentinel placed on preset_queue when a dispatched request definitively
# failed (JSON parse error or malformed response) rather than being
# cancelled. Callers that track "is a request still outstanding" via a
# pending flag (poc_interrupt.py's request_pending) need this signal --
# without it, a genuine failure looks identical to "still generating" and
# the flag never clears, permanently blocking any retry for the rest of
# the trial. A cancelled request does NOT get this sentinel: whoever
# cancelled it is already responsible for dispatching its own replacement
# and managing the pending flag for that new dispatch.
REQUEST_FAILED = object()


def llm_worker(situation: str, preset_queue: queue.Queue, cancel_event=None,
               model: str | None = None, extra_latency: float = 0.0):
    t0 = time.time()
    print(f"[LLM] Generating preset for: '{situation}'")
    preset = generate_preset(situation, cancel_event, model=model, extra_latency=extra_latency)
    elapsed = time.time() - t0
    interrupted = preset is None and cancel_event is not None and cancel_event.is_set()
    _log_llm(situation, preset, elapsed, interrupted=interrupted)
    if preset is None:
        print("[LLM] Request cancelled or failed")
        if not interrupted:
            preset_queue.put(REQUEST_FAILED)
        return
    print(f"[LLM] Done in {elapsed:.1f}s | skill: {preset.get('skill_name')}")
    preset_queue.put(preset)


class PresetDispatcher:
    def __init__(self, env):
        self.env = env
        self.action_spec = env.action_spec()
        self._preset_queue = queue.Queue()
        self._current_preset = None
        self._action_idx = 0
        self._step_count = 0
        self._cycle_count = 0
        # Set by _load_next_preset() when a dequeued item is the
        # REQUEST_FAILED sentinel or fails schema validation; drained via
        # consume_request_failed() so callers tracking a pending-request
        # flag know to unblock a retry instead of waiting forever.
        self._request_failed = False

        # Efference copy: actual action sent to env.step()
        self._last_action = np.zeros(12)
        # Intended action: what the preset wants, before fault zeroing
        self._intended_action = np.zeros(12)
        # Fault injection: set of leg indices (0=FL, 1=FR, 2=BR, 3=BL)
        # whose actuators are forced to 0
        self.fault_legs: set = set()

        # CPG → Cerebellum upward signal pathway
        # Set by CerebellarMonitor; dispatcher fires interrupt when actuator discrepancy detected
        self.interrupt_event = None
        self.suppress_interrupt = False

    @property
    def cycles_remaining(self) -> int | None:
        """Returns cycles left in current preset, or None if no preset / not a loop."""
        if self._current_preset is None:
            return None
        max_cycles = self._current_preset.get("max_cycles")
        if not max_cycles:
            return None
        return max(0, max_cycles - self._cycle_count)

    def request_preset(self, situation: str, cancel_event=None,
                       model: str | None = None, extra_latency: float = 0.0):
        t = threading.Thread(
            target=llm_worker,
            args=(situation, self._preset_queue, cancel_event, model, extra_latency),
            daemon=True,
        )
        t.start()

    def clear_queue(self):
        """Flush all pending presets from the queue."""
        while not self._preset_queue.empty():
            try:
                self._preset_queue.get_nowait()
            except queue.Empty:
                break

    def force_preset(self, preset: dict):
        """Clear queue and push a preset for immediate loading on next step."""
        self.clear_queue()
        self._preset_queue.put(preset)

    def consume_request_failed(self) -> bool:
        """Return whether a request has failed since the last check, clearing the flag."""
        failed = self._request_failed
        self._request_failed = False
        return failed

    def _load_next_preset(self):
        try:
            preset = self._preset_queue.get_nowait()
        except queue.Empty:
            return

        if preset is REQUEST_FAILED:
            self._request_failed = True
            return

        # Validation is intentionally defensive rather than an enumeration of
        # specific known failure shapes: weaker LLMs have been observed to
        # violate the schema in many different ways (wrong joint_targets
        # count, missing keys, empty actions list, non-numeric values, ...),
        # and a shape this code doesn't yet anticipate must fail safely
        # (flag the request as failed so a retry is triggered) rather than
        # raise -- an uncaught exception here would crash the whole trial
        # subprocess (ZeroDivisionError on an empty actions list and
        # KeyError on a missing duration_steps were both reachable before
        # this rewrite; batch runners do survive a crashed subprocess and
        # move on to the next trial, but that trial's data is still lost).
        try:
            if not all(k in preset for k in ("skill_name", "type", "actions")):
                print(f"[Dispatcher] WARNING: Skipping malformed preset (missing required keys): {list(preset.keys())}")
                self._request_failed = True
                return
            actions = preset["actions"]
            if not isinstance(actions, list) or len(actions) == 0:
                print(f"[Dispatcher] WARNING: Skipping preset '{preset.get('skill_name')}' "
                      f"-- 'actions' is empty or not a list")
                self._request_failed = True
                return
            for i, phase in enumerate(actions):
                targets = phase.get("joint_targets", [])
                if len(targets) != 12:
                    print(f"[Dispatcher] WARNING: Skipping preset '{preset['skill_name']}' "
                          f"-- action[{i}] has {len(targets)} joint_targets (expected 12)")
                    self._request_failed = True
                    return
                if not all(isinstance(v, (int, float)) for v in targets):
                    print(f"[Dispatcher] WARNING: Skipping preset '{preset['skill_name']}' "
                          f"-- action[{i}] has non-numeric joint_targets")
                    self._request_failed = True
                    return
                duration = phase.get("duration_steps")
                if not isinstance(duration, (int, float)) or duration <= 0:
                    print(f"[Dispatcher] WARNING: Skipping preset '{preset['skill_name']}' "
                          f"-- action[{i}] has invalid duration_steps: {duration!r}")
                    self._request_failed = True
                    return
        except Exception as e:
            # Catch-all: any other malformed shape (wrong types, unexpected
            # structure) is treated as a failed request rather than allowed
            # to propagate and crash the trial.
            print(f"[Dispatcher] WARNING: Skipping preset -- validation raised {type(e).__name__}: {e}")
            self._request_failed = True
            return

        self._current_preset = preset
        self._action_idx = 0
        self._step_count = 0
        self._cycle_count = 0
        print(f"[Dispatcher] Loaded preset: {preset['skill_name']} ({preset['type']})")

    def _get_action(self) -> np.ndarray:
        if self._current_preset is None:
            self._intended_action = np.zeros(12)
            return np.zeros(self.action_spec.shape)

        actions = self._current_preset["actions"]
        phase = actions[self._action_idx % len(actions)]
        intended = np.array(phase["joint_targets"], dtype=np.float64)

        # Store intended action (before fault zeroing) for monitor thread
        self._intended_action = intended.copy()

        # Apply fault injection: zero out actuators of faulty legs
        action = intended.copy()
        for leg_idx in self.fault_legs:
            action[leg_idx * 3: leg_idx * 3 + 3] = 0.0

        return action

    def _advance(self):
        if self._current_preset is None:
            return

        actions = self._current_preset["actions"]
        phase = actions[self._action_idx % len(actions)]
        self._step_count += 1

        if self._step_count >= phase["duration_steps"]:
            self._step_count = 0
            self._action_idx += 1

            if self._action_idx >= len(actions):
                self._action_idx = 0
                self._cycle_count += 1

                preset_type = self._current_preset["type"]
                max_cycles = self._current_preset.get("max_cycles") or 1

                if preset_type == "one-shot" or self._cycle_count >= max_cycles:
                    print(
                        f"[Dispatcher] Preset '{self._current_preset['skill_name']}' finished "
                        f"({self._cycle_count} cycle(s))"
                    )
                    self._current_preset = None

    def step(self) -> tuple:
        self._load_next_preset()
        action = self._get_action()

        # CPG → Cerebellum: actuator discrepancy detection
        # If intended ≠ sent (fault zeroing applied), signal cerebellum immediately
        if (self.interrupt_event is not None
                and not self.suppress_interrupt
                and not self.interrupt_event.is_set()
                and np.any(np.abs(self._intended_action - action) > 0.01)):
            print("[Dispatcher/CPG] Actuator discrepancy detected -- signaling cerebellum")
            self.interrupt_event.set()

        self._last_action = action.copy()
        time_step = self.env.step(action)
        self._advance()
        return time_step, action
