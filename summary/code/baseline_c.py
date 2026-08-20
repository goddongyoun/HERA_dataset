"""Baseline C: No Interrupt — same async loop preset system as Ours, but with
fault injection and NO interrupt mechanism.

At FAULT_START_STEP, FL leg actuators are zeroed (fault injected).
LLM IS informed of the FL fault via situation string (same as Ours).
Recovery is possible via cooldown-based LLM request — but NO interrupt/monitor.
The key difference from Ours: no CerebellarMonitor, no streaming interrupt.
Recovery happens only when cooldown expires and LLM generates a reconnect preset.

Contrast with Ours (poc_interrupt.py):
- Same fault injection
- Same FL fault awareness in LLM situation
- No monitor thread (no cerebellum)
- No interrupt: LLM called only on cooldown expiry
- Recovery possible but slower (no event-driven trigger)
"""
import argparse
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

LEG_NAMES      = {0: "FL", 1: "FR", 2: "BL", 3: "BR"}
LEG_FULL_NAMES = {0: "front-left", 1: "front-right", 2: "back-left", 3: "back-right"}
LEG_ACTION_SLICE = {0: (0, 3), 1: (3, 6), 2: (6, 9), 3: (9, 12)}
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


def mission_worker(stop_event: threading.Event, results: list, lock: threading.Lock,
                   model_name: str, extra_latency: float):
    """Continuously cycles through mission questions. NOT interruptible (Baseline C)."""
    for question in MISSION_QUESTIONS:
        if stop_event.is_set():
            break
        print("[Mission] New planning task started...")
        t0 = time.time()
        try:
            content = OLLAMA_WORKER.submit(
                messages=[
                    {"role": "system", "content": MISSION_SYSTEM_PROMPT},
                    {"role": "user",   "content": question},
                ],
                model=model_name, use_json_format=False, temperature=0.7,
                extra_latency=extra_latency, cancel_event=None,
            )
            text = content or ""
            elapsed = time.time() - t0
            print(f"[Mission] Complete ({elapsed:.1f}s, {len(text)} chars)")
            with lock:
                results.append({"status": "complete", "elapsed": round(elapsed, 2), "chars": len(text)})
        except Exception as e:
            print(f"[Mission] Error: {e}")
            break


def get_situation(time_step, step_idx: int, fault_active: bool = False,
                  cycles_remaining: int | None = None,
                  fault_leg_idx: int = 0) -> str:
    upright = float(np.array(time_step.observation["torso_upright"]))
    short = LEG_NAMES[fault_leg_idx]
    full  = LEG_FULL_NAMES[fault_leg_idx]

    if fault_active:
        base = (
            f"EMERGENCY: The {full} ({short}) leg is unresponsive. "
            "Joint commands are being sent but the leg is not responding. "
            f"Generate a reconnect preset to re-enable the {short} leg and restore walking."
        )
    elif step_idx > 20 and upright < 0.3:
        base = (
            "The robot has fallen. Generate a LOOP preset to attempt recovery: "
            "alternate leg extensions to right the torso."
        )
    elif step_idx < 600:
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


def _fault_detected(dispatcher, fault_leg_idx: int) -> bool:
    """CPG-level fault detection: intended ≠ sent for the faulty leg."""
    s, e = LEG_ACTION_SLICE[fault_leg_idx]
    return bool(np.any(np.abs(dispatcher._intended_action[s:e] - dispatcher._last_action[s:e]) > 0.01))


def main(save_video: bool = True, fault_leg_idx: int = 0,
         model_name: str | None = None, extra_latency: float = 0.0):

    _model = model_name or MODEL

    env = suite.load("quadruped", "walk")
    dispatcher = PresetDispatcher(env)

    mission_stop_event = threading.Event()
    mission_lock = threading.Lock()
    mission_results = []

    time_step = _reset_standing(env)

    # Cancel handle for the periodic "normal walking preset" request stream only
    # (never for the mission-planning stream -- that must remain unpreempted,
    # since testing the effect of NOT preempting mission inference is the whole
    # point of this baseline). Reassigned each time a new normal preset request
    # is dispatched; set() when a fault is detected so a stale, pre-fault
    # request can't silently land in dispatcher._preset_queue after the fault
    # and be mistaken for the fault-aware reconnect response.
    preset_cancel_event = threading.Event()

    print("[Main] Requesting initial preset from LLM...")
    dispatcher.request_preset(get_situation(time_step, 0), preset_cancel_event,
                              model=_model, extra_latency=extra_latency)

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
            preset_cancel_event.set()
            OLLAMA_WORKER.clear_pending()
            preset_cancel_event = threading.Event()
            dispatcher.request_preset(get_situation(time_step, 0), preset_cancel_event,
                                      model=_model, extra_latency=extra_latency)
            _wait_t0 = time.time()
        time.sleep(0.1)

    threading.Thread(
        target=mission_worker,
        args=(mission_stop_event, mission_results, mission_lock, _model, extra_latency),
        daemon=True,
    ).start()

    frames = []
    last_situation = ""
    last_llm_request_step = -COOLDOWN_STEPS
    t_wall_start = time.time()
    llm_calls = 1

    fault_injected = False
    # Separate from fault_injected (briefly False again after recovery):
    # latches permanently True the first time injection actually happens so
    # the >= FAULT_START_STEP condition below cannot re-fire and re-inject
    # every time recovery completes and a new preset loads.
    fault_injection_attempted = False
    fault_inject_step = None
    fault_cleared_step = None
    fault_inject_time = None
    fault_cleared_time = None
    reconnect_requested = False
    reconnect_snapshot = None
    # Whatever situation was last requested before the fault (almost always
    # "walk forward", since FAULT_START_STEP=500 falls within that phase) --
    # re-issued verbatim on recovery so the robot resumes its last pre-fault
    # command instead of falling through to get_situation()'s step-indexed
    # schedule, which by the time recovery happens has long since advanced
    # past the walking phase and would otherwise request standing still.
    pre_fault_situation = None

    step_log = []
    cumulative_reward = 0.0
    steps_upright = 0

    t_end = t_wall_start + TOTAL_DURATION_SEC
    step = 0
    while time.time() < t_end:

        # Gated on a preset actually being active (see poc_interrupt.py for
        # full rationale): with none loaded, intended == sent == zero, so
        # the fault has no observable effect and _fault_detected() below can
        # never trigger. `>=` (not `==`) so injection still happens as soon
        # as a preset becomes active, whenever that is. Gated on
        # fault_injection_attempted (not fault_injected), which never goes
        # back to False, so this can only ever fire once per trial.
        if step >= FAULT_START_STEP and not fault_injection_attempted and dispatcher._current_preset is not None:
            dispatcher.fault_legs.add(fault_leg_idx)
            fault_injected = True
            fault_injection_attempted = True
            fault_inject_step = step
            fault_inject_time = time.time()
            pre_fault_situation = last_situation
            print(f"[Main] FAULT INJECTED at step {step} -- {LEG_NAMES[fault_leg_idx]} leg zeroed (no interrupt)")

        time_step, action = dispatcher.step()

        if time_step.last():
            time_step = _reset_standing(env)

        reward = time_step.reward or 0.0
        upright = float(np.array(time_step.observation["torso_upright"]))
        speed = float(np.linalg.norm(np.array(time_step.observation["torso_velocity"])))
        skill = dispatcher._current_preset["skill_name"] if dispatcher._current_preset else "none"

        cumulative_reward += reward
        if upright > 0.5:
            steps_upright += 1

        if reconnect_requested:
            current = dispatcher._current_preset
            if current is not None and current is not reconnect_snapshot:
                dispatcher.fault_legs.discard(fault_leg_idx)
                fault_injected = False
                fault_cleared_step = step
                fault_cleared_time = time.time()
                reconnect_requested = False
                reconnect_snapshot = None
                print(f"[Main] RECOVERY COMPLETE at step {step} -- {LEG_NAMES[fault_leg_idx]} re-enabled (cooldown-based)")

                # Resume the last pre-fault command (see pre_fault_situation
                # comment above) instead of falling through to
                # get_situation()'s step-indexed schedule. Cancel/flush first
                # in case any earlier periodic request is still pending.
                preset_cancel_event.set()
                OLLAMA_WORKER.clear_pending()
                preset_cancel_event = threading.Event()
                dispatcher.request_preset(pre_fault_situation, preset_cancel_event,
                                          model=_model, extra_latency=extra_latency)
                llm_calls += 1
                last_llm_request_step = step
                last_situation = get_situation(time_step, step, fault_active=False,
                                               fault_leg_idx=fault_leg_idx)

        fl_fault = fault_injected or _fault_detected(dispatcher, fault_leg_idx)

        step_log.append({
            "step": step,
            "upright": round(upright, 4),
            "speed": round(speed, 4),
            "reward": round(reward, 4),
            "skill_name": skill,
            "fault_active": fl_fault,
        })

        if save_video and step % EVERY_N == 0:
            frames.append(env.physics.render(height=HEIGHT, width=WIDTH, camera_id=CAMERA_ID))

        if step % 100 == 0:
            fault_tag = " [FAULT]" if fl_fault else ""
            print(
                f"  step={step:4d}{fault_tag} | skill={skill:30s} | "
                f"upright={upright:.2f} | speed={speed:.3f} | reward={reward:.3f}"
            )

        cycles_left = dispatcher.cycles_remaining
        near_end = cycles_left is not None and cycles_left <= PREEMPT_CYCLES
        situation = get_situation(time_step, step,
                                  fault_active=fl_fault,
                                  cycles_remaining=cycles_left if near_end else None,
                                  fault_leg_idx=fault_leg_idx)
        preset_done = dispatcher._current_preset is None
        situation_changed = situation != last_situation
        queue_empty = dispatcher._preset_queue.empty()
        cooldown_ok = (step - last_llm_request_step) >= COOLDOWN_STEPS

        if (preset_done or situation_changed or near_end) and queue_empty and cooldown_ok:
            if fl_fault and not reconnect_requested:
                # Abort any stale, pre-fault normal preset request still in
                # flight (queued behind mission calls in OLLAMA_WORKER) so it
                # cannot land in dispatcher._preset_queue after the fault and
                # be mistaken for the fault-aware reconnect response. The
                # mission-planning stream itself is deliberately left running.
                preset_cancel_event.set()
                OLLAMA_WORKER.clear_pending()
                dispatcher.clear_queue()
                reconnect_snapshot = dispatcher._current_preset
                # Stop executing the pre-fault preset immediately (matches
                # HERA/Baseline Fair's reflex-level response), so that the
                # only difference between this baseline and HERA is whether
                # the *recovery request* is dispatched via interrupt vs.
                # cooldown-driven polling -- not whether the robot keeps
                # moving on the stale pre-fault gait in the meantime.
                dispatcher._current_preset = None
                reconnect_requested = True
                preset_cancel_event = threading.Event()
                print(f"[Main] EMERGENCY LLM request at step {step} (cooldown-based, no interrupt)")
                dispatcher.request_preset(situation, preset_cancel_event, model=_model, extra_latency=extra_latency)
                last_situation = situation
                last_llm_request_step = step
                llm_calls += 1
            elif not reconnect_requested:
                # Cancel the previous periodic request before submitting a
                # new one, so unanswered retries (e.g. while queued behind a
                # long mission call) do not pile up in OLLAMA_WORKER faster
                # than they can ever be served.
                preset_cancel_event.set()
                OLLAMA_WORKER.clear_pending()
                preset_cancel_event = threading.Event()
                dispatcher.request_preset(situation, preset_cancel_event, model=_model, extra_latency=extra_latency)
                last_situation = situation
                last_llm_request_step = step
                llm_calls += 1

        step += 1

    total_steps = step
    mission_stop_event.set()
    wall_time = time.time() - t_wall_start
    upright_rate = steps_upright / total_steps if total_steps > 0 else 0.0

    fault_duration = (fault_cleared_step - fault_inject_step) if (fault_inject_step is not None and fault_cleared_step is not None) else (total_steps - fault_inject_step) if fault_inject_step is not None else None

    # Wall-clock delta (directly measured via time.time(), seconds)
    fault_duration_sec = (
        round(fault_cleared_time - fault_inject_time, 3)
        if (fault_inject_time is not None and fault_cleared_time is not None) else None
    )

    print("\n=== Baseline C Results (No Interrupt) ===")
    print(f"  Fault injected at step : {fault_inject_step}")
    print(f"  Fault cleared at step  : {fault_cleared_step}")
    print(f"  Fault duration         : {fault_duration} steps / {fault_duration_sec}s")
    mission_complete = sum(1 for r in mission_results if r["status"] == "complete")
    print(f"  LLM calls              : {llm_calls}")
    print(f"  Mission plans complete : {mission_complete}")
    print(f"  LLM model              : {_model}")
    print(f"  LLM extra latency      : {extra_latency}s")
    print(f"  Total wall time        : {wall_time:.1f}s")
    print(f"  Cumulative reward      : {cumulative_reward:.2f}")
    print(f"  Steps upright (>0.5)   : {steps_upright} / {total_steps}")
    print(f"  Upright rate           : {upright_rate * 100:.1f}%")

    RESULTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    leg_tag = LEG_NAMES[fault_leg_idx]

    summary = {
        "method": "baseline_c",
        "fault_leg": leg_tag,
        "timestamp": ts,
        "llm_model": _model,
        "llm_latency_sec": extra_latency,
        "total_steps": total_steps,
        "fault_start_step": fault_inject_step,
        "fault_cleared_step": fault_cleared_step,
        "fault_duration_steps": fault_duration,
        "fault_inject_time": fault_inject_time,
        "fault_cleared_time": fault_cleared_time,
        "fault_duration_sec": fault_duration_sec,
        "llm_calls": llm_calls,
        "mission_complete": mission_complete,
        "mission_results": mission_results,
        "wall_time_sec": round(wall_time, 2),
        "cumulative_reward": round(cumulative_reward, 4),
        "steps_upright": steps_upright,
        "upright_rate": round(upright_rate, 4),
    }
    stem = f"baseline_c_{leg_tag}_{ts}"
    with open(RESULTS_DIR / f"{stem}.json", "w") as f:
        json.dump({"summary": summary, "steps": step_log}, f, indent=2)

    with open(RESULTS_DIR / f"{stem}.csv", "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["step", "upright", "speed", "reward", "skill_name", "fault_active"]
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
    args = parser.parse_args()
    main(save_video=not args.no_video, fault_leg_idx=args.fault_leg,
         model_name=args.model, extra_latency=args.latency)
