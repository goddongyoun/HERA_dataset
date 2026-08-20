"""Ours (Interrupt): event-driven fault recovery with cerebellar monitor thread.

Biological analogy (3-layer hierarchy):
  LLM (cortex/brainstem) - high-level preset generation, reconnect planning
  CerebellarMonitor      - efference copy vs afference error monitoring
  PresetDispatcher (CPG) - autonomous gait rhythm execution

Experiment flow:
  1. Normal walk (step 0 ~ FAULT_START_STEP)
  2. FL leg fault injected at FAULT_START_STEP
  3. CerebellarMonitor detects error → sets interrupt_event
  4. Main loop: cancel ongoing LLM call, request reconnect preset
  5. Reconnect preset loads → FL re-enabled → normal walk resumes

Ablation flags:
  --no-cancel   : skip mission_future.cancel() (HERA-NoCancel)
  --no-suppress : skip monitor.suppress during reconnect (HERA-NoSuppress)

Contrast with baseline_c.py: identical fault injection but NO interrupt/monitor.
"""
import argparse
import asyncio
import csv
import itertools
import json
import threading
import time
from datetime import datetime
from pathlib import Path

import imageio
import numpy as np
import ollama
from dm_control import suite
from dispatcher import PresetDispatcher, MODEL, OLLAMA_WORKER
from monitor_thread import CerebellarMonitor

# Dedicated asyncio event loop for mission planning (background thread)
_mission_loop = asyncio.new_event_loop()
threading.Thread(target=_mission_loop.run_forever, daemon=True, name="MissionLoop").start()

MISSION_SYSTEM_PROMPT = """You are a quadruped robot on an autonomous search-and-rescue mission.
You have high-level cognitive capabilities independent of your motor control system.
Think step by step and provide detailed reasoning for every decision."""

MISSION_QUESTIONS = itertools.cycle([
    (
        "You are operating in a disaster zone. Analyze your current operational status, "
        "describe the terrain and obstacles ahead, and plan your next 10 actions in detail "
        "with full reasoning for each step. Consider joint load, energy efficiency, "
        "stability risks, and mission objectives."
    ),
    (
        "A survivor has been located 200 meters ahead. Assess the obstacles between your "
        "current position and the target. Plan the optimal path considering terrain type, "
        "energy budget, and time constraints. Describe each decision point with reasoning."
    ),
    (
        "Perform a comprehensive self-assessment: evaluate each leg joint's current load, "
        "estimate remaining operational time, identify any early warning signs of mechanical "
        "stress, and recommend specific gait adjustments with detailed justification."
    ),
    (
        "Mission control has requested a full situational report. Describe your current "
        "position, surrounding environment, detected hazards, progress toward objectives, "
        "and your recommended next course of action with step-by-step reasoning."
    ),
])

TOTAL_DURATION_SEC = 30.0
COOLDOWN_STEPS = 300
PREEMPT_CYCLES = 5
FAULT_START_STEP = 500
RESULTS_DIR = Path(__file__).parent / "results"

# Leg parameterization
LEG_NAMES      = {0: "FL", 1: "FR", 2: "BL", 3: "BR"}
LEG_FULL_NAMES = {0: "front-left", 1: "front-right", 2: "back-left", 3: "back-right"}
LEG_ACTION_IDX = {0: "0,1,2", 1: "3,4,5", 2: "6,7,8", 3: "9,10,11"}
LEG_EGO_SLICE  = {0: (0, 3), 1: (4, 7), 2: (8, 11), 3: (12, 15)}  # egocentric_state joint pos
VIDEOS_DIR = Path(__file__).parent / "videos"
HEIGHT, WIDTH, CAMERA_ID, FPS, EVERY_N = 480, 640, 1, 30, 2
STAND_ACTION = np.array([0.0, 0.0, -0.4,  0.0, 0.0, -0.4,  0.0, 0.0, -0.4,  0.0, 0.0, -0.4])


def _reset_standing(env):
    env.reset()
    env.physics.data.qpos[2] = 0.7
    env.physics.data.qpos[3:7] = [1.0, 0.0, 0.0, 0.0]
    env.physics.data.qpos[7:19] = [0.0, 0.0, -0.4,  0.0, 0.0, -0.4,
                                    0.0, 0.0, -0.4,  0.0, 0.0, -0.4]
    env.physics.data.qvel[:] = 0.0
    env.physics.forward()
    time_step = None
    for _ in range(300):
        time_step = env.step(STAND_ACTION)
    return time_step


def get_situation(time_step, step_idx: int, cycles_remaining: int | None = None,
                  walking_resumed: bool = False) -> str:
    upright = float(np.array(time_step.observation["torso_upright"]))

    if step_idx > 20 and upright < 0.3:
        base = (
            "The robot has fallen. Generate a LOOP preset to attempt recovery: "
            "alternate leg extensions to right the torso."
        )
    elif step_idx < 600 or walking_resumed:
        base = (
            "Walk forward on flat ground. Generate a LOOP preset with multiple "
            "alternating leg phases to produce a continuous walking gait."
        )
    elif step_idx < 1400:
        base = (
            "Turn left while walking. Generate a LOOP preset where the left legs "
            "push less than the right legs to curve the path."
        )
    else:
        base = "Stop and hold a stable standing posture. Generate a one-shot preset."

    if cycles_remaining is not None:
        return f"{base} (Current preset has {cycles_remaining} cycles remaining. Prepare the next preset now.)"
    return base


async def _mission_coroutine(stop_event: threading.Event, results: list, lock: threading.Lock,
                             model_name: str, extra_latency: float):
    """Async mission worker.

    Requests are routed through the shared OLLAMA_WORKER (dispatcher.py) so
    they are processed in strict submission order relative to preset /
    emergency generation, deterministically emulating a single-slot backend
    instead of relying on client-side thread/GIL scheduling (which was found
    to silently starve this coroutine for seconds at a time).

    task.cancel() raises CancelledError at the current await point; the
    handler below sets a dedicated per-call cancel_event so the worker aborts
    an in-progress stream promptly instead of finishing it unattended (which
    would otherwise keep occupying the queue).
    """
    for question in MISSION_QUESTIONS:
        if stop_event.is_set():
            break
        print("[Mission] New planning task started...")
        t0 = time.time()
        mission_cancel_event = threading.Event()
        text = ""
        try:
            content = await OLLAMA_WORKER.submit_async(
                messages=[
                    {"role": "system", "content": MISSION_SYSTEM_PROMPT},
                    {"role": "user",   "content": question},
                ],
                model=model_name, use_json_format=False, temperature=0.7,
                extra_latency=extra_latency, cancel_event=mission_cancel_event,
            )
            text = content or ""
            elapsed = time.time() - t0
            print(f"[Mission] Complete ({elapsed:.1f}s, {len(text)} chars)")
            with lock:
                results.append({"status": "complete", "elapsed": round(elapsed, 2), "chars": len(text)})
        except asyncio.CancelledError:
            mission_cancel_event.set()  # tell the worker to abort the in-flight/queued stream
            elapsed = time.time() - t0
            print(f"[Mission] INTERRUPTED after {elapsed:.1f}s ({len(text)} chars)")
            with lock:
                results.append({"status": "interrupted", "elapsed": round(elapsed, 2), "chars": len(text)})
            return  # task done; do not restart after cancellation
        except Exception as e:
            print(f"[Mission] Error: {e}")
            break


def build_reconnect_situation(fault_leg_idx: int, joint_pos: list) -> str:
    name = LEG_FULL_NAMES[fault_leg_idx]
    short = LEG_NAMES[fault_leg_idx]
    idx = LEG_ACTION_IDX[fault_leg_idx]
    return (
        f"EMERGENCY: The {name} ({short}) leg is unresponsive. "
        "Joint commands are being sent but the leg position is not changing. "
        f"Current {short} joint positions (hip_abduct, hip_flex, knee): {joint_pos}. "
        f"Generate a one-shot preset that sends a strong reset command to return the {short} leg "
        f"(joints {idx}) to its neutral standing position [0.0, 0.0, -0.4], "
        "while keeping the other three legs in a stable standing posture."
    )


def main(save_video: bool = True, fault_leg_idx: int = 0,
         model_name: str | None = None, extra_latency: float = 0.0,
         no_cancel: bool = False, no_suppress: bool = False):

    _model = model_name or MODEL

    # Derive method name for result files based on ablation flags
    if no_cancel:
        method_name = "hera_nocancel"
    elif no_suppress:
        method_name = "hera_nosuppress"
    else:
        method_name = "ours_interrupt"

    env = suite.load("quadruped", "walk")
    dispatcher = PresetDispatcher(env)

    # Shared events
    cancel_event = threading.Event()
    interrupt_event = threading.Event()
    mission_stop_event = threading.Event()
    mission_lock = threading.Lock()
    mission_results = []

    # Cerebellar monitor (separate thread)
    monitor = CerebellarMonitor(dispatcher, interrupt_event)
    monitor.start()

    time_step = _reset_standing(env)

    # request_pending: True from the moment a preset request is dispatched
    # until its response is observed to have landed (dispatcher._current_preset
    # changes away from last_dispatch_snapshot, the value it held at dispatch
    # time). While True, no further request may be dispatched -- this is what
    # actually enforces "wait for the one outstanding request" rather than
    # firing a new duplicate every cooldown period regardless of whether the
    # previous one has been answered yet.
    request_pending = False
    last_dispatch_snapshot = None

    print("[Main] Requesting initial preset from LLM...")
    last_dispatch_snapshot = dispatcher._current_preset
    dispatcher.request_preset(get_situation(time_step, 0), cancel_event,
                              model=_model, extra_latency=extra_latency)
    request_pending = True

    print("[Main] Waiting for first preset...")
    _wait_t0 = time.time()
    _retry_count = 0
    _retry_timeout = max(8.0, extra_latency + 8.0)  # must exceed the guaranteed extra_latency floor
    while dispatcher._preset_queue.empty():
        if time.time() - _wait_t0 > _retry_timeout:
            _retry_count += 1
            if _retry_count > 5:
                print("[Main] WARNING: giving up on initial preset after 5 retries; proceeding anyway")
                break
            print(f"[Main] No initial preset after {_retry_timeout:.0f}s (retry {_retry_count}/5) -- re-requesting")
            cancel_event.set()
            OLLAMA_WORKER.clear_pending()
            # A fresh Event for the new dispatch, rather than clearing and
            # reusing the same object: if the abandoned attempt's worker
            # thread is between chunk-boundary checks right as this happens,
            # clearing the shared event would make it see is_set()==False on
            # its next check and mistake itself for not-cancelled, letting a
            # stale response land in the queue unflagged. The old object
            # stays permanently set, so a late-finishing stale attempt is
            # correctly recognized as cancelled no matter when it checks.
            cancel_event = threading.Event()
            last_dispatch_snapshot = dispatcher._current_preset
            dispatcher.request_preset(get_situation(time_step, 0), cancel_event,
                                      model=_model, extra_latency=extra_latency)
            request_pending = True
            _wait_t0 = time.time()
        time.sleep(0.1)

    # Start mission planning AFTER walking begins (robot walks while reasoning)
    mission_future = asyncio.run_coroutine_threadsafe(
        _mission_coroutine(mission_stop_event, mission_results, mission_lock,
                           _model, extra_latency),
        _mission_loop,
    )

    # Timing log for this experiment (step index + wall-clock time.time() per event)
    timing = {
        "fault_inject_step": None,
        "interrupt_fired_step": None,
        "reconnect_requested_step": None,
        "reconnect_loaded_step": None,
        "recovery_complete_step": None,
        "fault_inject_time": None,
        "interrupt_fired_time": None,
        "reconnect_requested_time": None,
        "reconnect_loaded_time": None,
        "recovery_complete_time": None,
    }

    frames = []
    last_situation = ""
    last_llm_request_step = -COOLDOWN_STEPS
    t_wall_start = time.time()
    llm_calls = 1

    fault_injected = False
    # Separate from fault_injected (which is briefly False again after
    # recovery): this must latch permanently True the first time injection
    # actually happens, so the >= FAULT_START_STEP condition below (needed
    # to wait for a slow-to-load initial preset, see below) cannot re-fire
    # and re-inject the fault every time recovery completes and a new
    # preset loads for the rest of the trial.
    fault_injection_attempted = False
    reconnect_requested = False
    walking_resumed = False

    step_log = []
    cumulative_reward = 0.0
    steps_upright = 0

    t_end = t_wall_start + TOTAL_DURATION_SEC
    step = 0
    while time.time() < t_end:

        # ── 1. Inject fault ──────────────────────────────────────────────────
        # Gated on a preset actually being active, not just step >=
        # FAULT_START_STEP: fault detection works by comparing intended vs.
        # sent actuator commands, and with no preset loaded both are zero,
        # so zeroing fault_legs has no observable effect and the interrupt
        # can never fire. Simulation steps do not wait on LLM responses, so
        # if the initial preset is unusually slow to arrive (observed under
        # heavy concurrent GPU load), step 500 can be reached before any
        # preset has ever loaded -- previously this silently failed the
        # trial for the rest of its duration with no interrupt, no recovery,
        # and no diagnostic. `>=` (not `==`) so injection still happens on
        # the first step a preset becomes active, whenever that is. Gated on
        # fault_injection_attempted (not fault_injected) so this can only
        # ever fire once per trial -- fault_injected itself goes back to
        # False on recovery, and step >= FAULT_START_STEP stays trivially
        # true for the rest of the trial, so guarding on fault_injected
        # alone would re-inject the fault every time recovery completes.
        if step >= FAULT_START_STEP and not fault_injection_attempted and dispatcher._current_preset is not None:
            dispatcher.fault_legs.add(fault_leg_idx)
            fault_injected = True
            fault_injection_attempted = True
            timing["fault_inject_step"] = step
            timing["fault_inject_time"] = time.time()
            print(f"[Main] FAULT INJECTED at step {step} - {LEG_NAMES[fault_leg_idx]} leg zeroed")

        # ── 2. Step physics ──────────────────────────────────────────────────
        time_step, action = dispatcher.step()

        if time_step.last():
            time_step = _reset_standing(env)

        reward = time_step.reward or 0.0
        upright = float(np.array(time_step.observation["torso_upright"]))
        speed = float(np.linalg.norm(np.array(time_step.observation["torso_velocity"])))
        skill = dispatcher._current_preset["skill_name"] if dispatcher._current_preset else "none"

        # A pending request has been answered once the dispatcher's active
        # preset differs from whatever it was at dispatch time (covers both
        # "was None, now loaded" and "was preset A, now preset B"). Must also
        # require the new preset to be non-None: _current_preset can become
        # None on its own when a one-shot preset (e.g. the emergency reset
        # loaded at recovery) naturally exhausts its cycle count, which is
        # unrelated to whether the *dispatched* request has been answered.
        # Without this guard that natural exhaustion is misread as "answered",
        # clearing request_pending early and letting block 7 re-dispatch a
        # duplicate of the still-in-flight request.
        #
        # A dispatched request can also fail outright (JSON parse error, or a
        # response that fails schema validation in _load_next_preset) rather
        # than being answered or naturally superseded. Nothing then ever
        # changes _current_preset, so the check above alone would leave
        # request_pending stuck True for the rest of the trial with no way to
        # retry. dispatcher.consume_request_failed() is drained every step
        # (regardless of request_pending) so a failure is never left stale
        # for a later, unrelated dispatch to consume.
        failed = dispatcher.consume_request_failed()
        if request_pending and (
            (dispatcher._current_preset is not None and dispatcher._current_preset is not last_dispatch_snapshot)
            or failed
        ):
            request_pending = False

        cumulative_reward += reward
        if upright > 0.5:
            steps_upright += 1

        # ── 3. Read egocentric state ─────────────────────────────────────────
        ego = np.array(time_step.observation["egocentric_state"])

        # ── 4. Handle interrupt (fired by CPG → cerebellum pathway) ─────────
        if interrupt_event.is_set() and not reconnect_requested:
            timing["interrupt_fired_step"] = step
            timing["interrupt_fired_time"] = time.time()
            print(f"[Main] INTERRUPT at step {step} - cancelling LLM, requesting reconnect")

            # Cancel any in-flight LLM calls
            cancel_event.set()
            if not no_cancel:
                mission_future.cancel()  # CancelledError → httpx TCP close → Ollama stops
                OLLAMA_WORKER.clear_pending()  # also drop mission's request if it was still queued
            time.sleep(0.1)
            # Fresh Event for the reconnect dispatch below, rather than
            # clearing and reusing the same object -- see the initial-retry
            # loop above for why reuse is unsafe (a stale in-flight request
            # could momentarily observe is_set()==False and land unflagged).
            cancel_event = threading.Event()

            # Flush queued presets and force immediate reload on next step
            dispatcher.clear_queue()
            dispatcher._current_preset = None

            # Build reconnect situation with current fault-leg joint positions
            s0, s1 = LEG_EGO_SLICE[fault_leg_idx]
            leg_pos = ego[s0:s1].round(3).tolist()
            reconnect_situation = build_reconnect_situation(fault_leg_idx, leg_pos)

            last_dispatch_snapshot = dispatcher._current_preset
            dispatcher.request_preset(reconnect_situation, cancel_event,
                                      model=_model, extra_latency=extra_latency)
            request_pending = True
            llm_calls += 1

            reconnect_requested = True
            timing["reconnect_requested_step"] = step
            timing["reconnect_requested_time"] = time.time()
            if not no_suppress:
                monitor.suppress = True
            interrupt_event.clear()

        # ── 5. Detect reconnect preset load → re-enable fault leg ────────────
        if reconnect_requested:
            current = dispatcher._current_preset
            if current is not None:
                timing["reconnect_loaded_step"] = step
                timing["reconnect_loaded_time"] = time.time()
                print(f"[Main] Reconnect preset loaded at step {step}: '{current['skill_name']}'")

                dispatcher.fault_legs.discard(fault_leg_idx)
                fault_injected = False
                reconnect_requested = False
                if not no_suppress:
                    monitor.suppress = False
                timing["recovery_complete_step"] = step
                timing["recovery_complete_time"] = time.time()
                print(f"[Main] RECOVERY COMPLETE at step {step} - {LEG_NAMES[fault_leg_idx]} re-enabled")
                request_pending = False  # the reconnect request has now been answered

                walking_resumed = True
                last_situation = ""
                last_dispatch_snapshot = dispatcher._current_preset
                dispatcher.request_preset(
                    get_situation(time_step, step, walking_resumed=True),
                    cancel_event,
                    model=_model, extra_latency=extra_latency,
                )
                request_pending = True
                last_llm_request_step = step
                llm_calls += 1
            elif not request_pending:
                # The reconnect request itself failed (JSON parse error or a
                # response that failed schema validation) and nothing else
                # retries it: block 4 above only fires once per fault
                # (guarded by reconnect_requested), and block 7's routine
                # dispatch is deliberately disabled for the whole reconnect
                # window. Without an explicit retry here, any single
                # malformed emergency response would fail the trial outright
                # for the rest of its duration, regardless of request_pending
                # bookkeeping -- this happens occasionally even with capable
                # models and much more often with weaker ones.
                s0, s1 = LEG_EGO_SLICE[fault_leg_idx]
                leg_pos = ego[s0:s1].round(3).tolist()
                retry_situation = build_reconnect_situation(fault_leg_idx, leg_pos)
                last_dispatch_snapshot = dispatcher._current_preset
                dispatcher.request_preset(retry_situation, cancel_event,
                                          model=_model, extra_latency=extra_latency)
                request_pending = True
                llm_calls += 1

        # ── 6. Log ───────────────────────────────────────────────────────────
        step_log.append({
            "step": step,
            "upright": round(upright, 4),
            "speed": round(speed, 4),
            "reward": round(reward, 4),
            "skill_name": skill,
            "fault_active": fault_injected,
            "reconnect_active": reconnect_requested,
        })

        if save_video and step % EVERY_N == 0:
            frames.append(env.physics.render(height=HEIGHT, width=WIDTH, camera_id=CAMERA_ID))

        if step % 100 == 0:
            fault_tag = " [FAULT]" if fault_injected else ""
            reconnect_tag = " [RECONNECT]" if reconnect_requested else ""
            print(
                f"  step={step:4d}{fault_tag}{reconnect_tag} | skill={skill:30s} | "
                f"upright={upright:.2f} | speed={speed:.3f} | reward={reward:.3f}"
            )

        # ── 7. Normal LLM request (only when not in interrupt/reconnect mode) ─
        if not reconnect_requested and not interrupt_event.is_set():
            cycles_left = dispatcher.cycles_remaining
            near_end = cycles_left is not None and cycles_left <= PREEMPT_CYCLES
            situation = get_situation(time_step, step, cycles_left if near_end else None,
                                     walking_resumed=walking_resumed)
            preset_done = dispatcher._current_preset is None
            situation_changed = situation != last_situation
            queue_empty = dispatcher._preset_queue.empty()
            cooldown_ok = (step - last_llm_request_step) >= COOLDOWN_STEPS

            if (preset_done or situation_changed or near_end) and queue_empty and cooldown_ok and not request_pending:
                last_dispatch_snapshot = dispatcher._current_preset
                dispatcher.request_preset(situation, cancel_event,
                                          model=_model, extra_latency=extra_latency)
                request_pending = True
                last_situation = situation
                last_llm_request_step = step
                llm_calls += 1

        step += 1

    total_steps = step
    mission_stop_event.set()
    mission_future.cancel()
    monitor.stop()
    wall_time = time.time() - t_wall_start
    upright_rate = steps_upright / total_steps if total_steps > 0 else 0.0

    fault_step = timing["fault_inject_step"]
    interrupt_step = timing["interrupt_fired_step"]
    reconnect_step = timing["reconnect_requested_step"]
    loaded_step = timing["reconnect_loaded_step"]
    recovery_step = timing["recovery_complete_step"]

    detect_latency = (interrupt_step - fault_step) if (interrupt_step and fault_step) else None
    llm_response_steps = (loaded_step - reconnect_step) if (loaded_step and reconnect_step) else None
    total_recovery_steps = (recovery_step - fault_step) if (recovery_step and fault_step) else None

    # Wall-clock deltas (directly measured via time.time(), seconds)
    fault_t, interrupt_t, reconnect_t, loaded_t, recovery_t = (
        timing["fault_inject_time"], timing["interrupt_fired_time"],
        timing["reconnect_requested_time"], timing["reconnect_loaded_time"],
        timing["recovery_complete_time"],
    )
    detect_latency_sec = round(interrupt_t - fault_t, 3) if (interrupt_t and fault_t) else None
    llm_response_sec = round(loaded_t - reconnect_t, 3) if (loaded_t and reconnect_t) else None
    total_recovery_sec = round(recovery_t - fault_t, 3) if (recovery_t and fault_t) else None

    print(f"\n=== {method_name} Results ===")
    print(f"  Fault injected at step    : {fault_step}")
    print(f"  Interrupt fired at step   : {interrupt_step}  (detect latency: {detect_latency} steps / {detect_latency_sec}s)")
    print(f"  Reconnect requested step  : {reconnect_step}")
    print(f"  Reconnect preset loaded   : {loaded_step}  (LLM response: {llm_response_steps} steps / {llm_response_sec}s)")
    print(f"  Recovery complete step    : {recovery_step}  (total: {total_recovery_steps} steps / {total_recovery_sec}s)")
    mission_complete = sum(1 for r in mission_results if r["status"] == "complete")
    mission_interrupted = sum(1 for r in mission_results if r["status"] == "interrupted")
    print(f"  LLM calls                 : {llm_calls}")
    print(f"  Mission plans complete    : {mission_complete}")
    print(f"  Mission plans interrupted : {mission_interrupted}")
    print(f"  LLM model                 : {_model}")
    print(f"  LLM extra latency         : {extra_latency}s")
    print(f"  Total wall time           : {wall_time:.1f}s")
    print(f"  Cumulative reward         : {cumulative_reward:.2f}")
    print(f"  Steps upright (>0.5)      : {steps_upright} / {total_steps}")
    print(f"  Upright rate              : {upright_rate * 100:.1f}%")

    RESULTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    leg_tag = LEG_NAMES[fault_leg_idx]

    fault_duration = (recovery_step - fault_step) if (fault_step is not None and recovery_step is not None) else None

    summary = {
        "method": method_name,
        "fault_leg": leg_tag,
        "timestamp": ts,
        "llm_model": _model,
        "llm_latency_sec": extra_latency,
        "ablation_no_cancel": no_cancel,
        "ablation_no_suppress": no_suppress,
        "total_steps": total_steps,
        "fault_start_step": fault_step,
        "interrupt_fired_step": interrupt_step,
        "reconnect_requested_step": reconnect_step,
        "reconnect_loaded_step": loaded_step,
        "recovery_complete_step": recovery_step,
        "fault_duration_steps": fault_duration,
        "detect_latency_steps": detect_latency,
        "llm_response_steps": llm_response_steps,
        "total_recovery_steps": total_recovery_steps,
        "fault_inject_time": fault_t,
        "interrupt_fired_time": interrupt_t,
        "reconnect_requested_time": reconnect_t,
        "reconnect_loaded_time": loaded_t,
        "recovery_complete_time": recovery_t,
        "detect_latency_sec": detect_latency_sec,
        "llm_response_sec": llm_response_sec,
        "total_recovery_sec": total_recovery_sec,
        "llm_calls": llm_calls,
        "mission_complete": mission_complete,
        "mission_interrupted": mission_interrupted,
        "mission_results": mission_results,
        "wall_time_sec": round(wall_time, 2),
        "cumulative_reward": round(cumulative_reward, 4),
        "steps_upright": steps_upright,
        "upright_rate": round(upright_rate, 4),
    }
    stem = f"{method_name}_{leg_tag}_{ts}"
    with open(RESULTS_DIR / f"{stem}.json", "w") as f:
        json.dump({"summary": summary, "steps": step_log}, f, indent=2)

    with open(RESULTS_DIR / f"{stem}.csv", "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["step", "upright", "speed", "reward", "skill_name",
                        "fault_active", "reconnect_active"],
        )
        writer.writeheader()
        writer.writerows(step_log)

    print(f"\n[Main] Results saved to results/{stem}.{{json,csv}}")

    if save_video and frames:
        VIDEOS_DIR.mkdir(exist_ok=True)
        out = VIDEOS_DIR / f"{stem}.mp4"
        print(f"[Main] Saving {len(frames)} frames → {out}")
        imageio.mimwrite(str(out), frames, fps=FPS, quality=8)
        print(f"[Main] Video saved to {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-video", action="store_true", help="Skip video recording")
    parser.add_argument("--fault-leg", type=int, default=0, choices=[0, 1, 2, 3],
                        help="Fault leg index: 0=FL 1=FR 2=BL 3=BR (default: 0)")
    parser.add_argument("--model", type=str, default=None,
                        help="Ollama model name (default: HERA_MODEL env or qwen3.5:9b)")
    parser.add_argument("--latency", type=float, default=0.0,
                        help="Artificial LLM latency in seconds (default: 0)")
    parser.add_argument("--no-cancel", action="store_true",
                        help="Ablation: skip mission LLM cancellation on interrupt")
    parser.add_argument("--no-suppress", action="store_true",
                        help="Ablation: skip interrupt suppression during reconnect")
    args = parser.parse_args()
    main(
        save_video=not args.no_video,
        fault_leg_idx=args.fault_leg,
        model_name=args.model,
        extra_latency=args.latency,
        no_cancel=args.no_cancel,
        no_suppress=args.no_suppress,
    )
