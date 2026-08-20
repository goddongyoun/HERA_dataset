"""Baseline Fair: Immediate emergency recovery without preempting mission planning.

Design intent:
- Same fast fault detection as HERA (CPG discrepancy, 1-step latency)
- Emergency LLM request submitted IMMEDIATELY on fault (no cooldown)
- Mission planning continues running, NOT cancelled (unlike HERA)
- Both emergency and mission planning LLM requests run concurrently

This isolates the key question:
  Is HERA's advantage from fast detection, or from *preempting* the LLM?

Expected outcome:
  If Ollama serializes requests (default), emergency waits for mission planning
  to complete (~36s) → recovery still fails within 30s window.
  This shows: fast detection alone does not solve the problem;
  preemption (cancellation) is the critical mechanism.
"""
import argparse
import csv
import itertools
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import imageio
import numpy as np
import ollama
from dm_control import suite
from dispatcher import PresetDispatcher, MODEL, OLLAMA_WORKER, SYSTEM_PROMPT
from monitor_thread import CerebellarMonitor

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
VIDEOS_DIR  = Path(__file__).parent / "videos"
HEIGHT, WIDTH, CAMERA_ID, FPS, EVERY_N = 480, 640, 1, 30, 2
STAND_ACTION = np.array([0.0, 0.0, -0.4,  0.0, 0.0, -0.4,  0.0, 0.0, -0.4,  0.0, 0.0, -0.4])

LEG_NAMES      = {0: "FL", 1: "FR", 2: "BL", 3: "BR"}
LEG_FULL_NAMES = {0: "front-left", 1: "front-right", 2: "back-left", 3: "back-right"}
LEG_ACTION_IDX = {0: "0,1,2", 1: "3,4,5", 2: "6,7,8", 3: "9,10,11"}
LEG_EGO_SLICE  = {0: (0, 3), 1: (4, 7), 2: (8, 11), 3: (12, 15)}


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


def get_situation(time_step, step_idx: int, cycles_remaining: int | None = None) -> str:
    upright = float(np.array(time_step.observation["torso_upright"]))
    if step_idx > 20 and upright < 0.3:
        base = ("The robot has fallen. Generate a LOOP preset to attempt recovery: "
                "alternate leg extensions to right the torso.")
    elif step_idx < 600:
        base = ("Walk forward on flat ground. Generate a LOOP preset with multiple "
                "alternating leg phases to produce a continuous walking gait.")
    elif step_idx < 1400:
        base = ("Turn left while walking. Generate a LOOP preset where the left legs "
                "push less than the right legs to curve the path.")
    else:
        base = "Stop and hold a stable standing posture. Generate a one-shot preset."
    if cycles_remaining is not None:
        return f"{base} (Current preset has {cycles_remaining} cycles remaining. Prepare the next preset now.)"
    return base


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


def mission_worker(stop_event: threading.Event, results: list, lock: threading.Lock,
                   model_name: str, extra_latency: float):
    """Mission planning: NOT interruptible (key difference from HERA)."""
    for question in MISSION_QUESTIONS:
        if stop_event.is_set():
            break
        print("[Mission] New planning task started (non-interruptible)...")
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


def emergency_worker(situation: str, preset_queue, result_holder: list, lock: threading.Lock,
                     model_name: str, extra_latency: float, max_attempts: int = 5,
                     attempt_counter: list | None = None):
    """Emergency LLM call — submitted to the same shared OLLAMA_WORKER queue as
    mission planning (not preempting it), so it genuinely waits its turn behind
    an in-flight mission call rather than racing it client-side.

    This preset is pushed directly onto dispatcher's queue via force_preset()
    rather than going through dispatcher.request_preset(), so it bypasses
    _load_next_preset()'s schema validation until the next dispatcher.step()
    call -- and, more importantly, there is no other code path in this file
    that retries the emergency request if it never arrives at all (unlike
    HERA's reconnect, or baseline_c's cooldown-driven polling). A single JSON
    parse failure or schema violation would otherwise fail the trial outright
    with no chance of recovery for the rest of the episode. Weaker models
    (llama3.2:3b) have been observed to violate this exact schema often
    enough that a non-retrying single attempt is not reliable, so this
    validates and retries internally, all within the same background thread
    and the same overall "one continuous emergency phase" -- retries still
    queue behind mission-planning calls exactly like the first attempt,
    preserving the no-preemption comparison this baseline exists to make."""
    for attempt in range(1, max_attempts + 1):
        t0 = time.time()
        label = "" if attempt == 1 else f" (retry {attempt}/{max_attempts})"
        print(f"[Emergency] Submitting emergency request{label} (concurrent with mission planning)...")
        if attempt_counter is not None:
            with lock:
                attempt_counter[0] += 1
        try:
            content = OLLAMA_WORKER.submit(
                messages=[
                    # The full schema-specifying prompt, identical to what
                    # dispatcher.generate_preset() uses for every other
                    # preset request (HERA's reconnect included) -- this used
                    # to be a bare one-line prompt with no schema at all,
                    # silently disadvantaging baseline_fair's single most
                    # important request relative to every other method in
                    # the comparison. Without the schema, the model has to
                    # guess the JSON shape and was observed to consistently
                    # invent a plausible-looking but entirely wrong one
                    # (fields like command_type/priority/target_legs instead
                    # of skill_name/type/actions), failing validation on
                    # every attempt regardless of how many retries.
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": situation},
                ],
                model=model_name, use_json_format=True, temperature=0.2,
                extra_latency=extra_latency, cancel_event=None,
            )
            if content is None:
                raise RuntimeError("emergency request failed")
            m = re.search(r'\{.*\}', content, re.DOTALL)
            preset = json.loads(m.group() if m else content)

            # Same schema dispatcher._load_next_preset() enforces -- checked
            # here too so a malformed response triggers a retry instead of
            # being force-loaded and only discovered as a crash/silent drop
            # once dispatcher.step() next processes the queue.
            if not all(k in preset for k in ("skill_name", "type", "actions")):
                raise ValueError(f"missing required keys: {list(preset.keys())}")
            actions = preset["actions"]
            if not isinstance(actions, list) or len(actions) == 0:
                raise ValueError("'actions' is empty or not a list")
            for i, phase in enumerate(actions):
                targets = phase.get("joint_targets", [])
                if len(targets) != 12 or not all(isinstance(v, (int, float)) for v in targets):
                    raise ValueError(f"action[{i}] has invalid joint_targets: {targets!r}")
                duration = phase.get("duration_steps")
                if not isinstance(duration, (int, float)) or duration <= 0:
                    raise ValueError(f"action[{i}] has invalid duration_steps: {duration!r}")

            elapsed = time.time() - t0
            print(f"[Emergency] Response received in {elapsed:.1f}s | skill: {preset.get('skill_name')}")
            with lock:
                result_holder.append(preset)
            preset_queue.put(preset)
            return
        except Exception as e:
            elapsed = time.time() - t0
            print(f"[Emergency] Failed after {elapsed:.1f}s: {e}")
    print(f"[Emergency] Giving up after {max_attempts} failed attempts")


def main(save_video: bool = True, fault_leg_idx: int = 0,
         model_name: str | None = None, extra_latency: float = 0.0):

    _model = model_name or MODEL

    env = suite.load("quadruped", "walk")
    dispatcher = PresetDispatcher(env)

    cancel_event    = threading.Event()
    interrupt_event = threading.Event()
    mission_stop    = threading.Event()
    mission_lock    = threading.Lock()
    mission_results = []
    emergency_lock  = threading.Lock()
    emergency_result = []

    monitor = CerebellarMonitor(dispatcher, interrupt_event)
    monitor.start()

    time_step = _reset_standing(env)

    print("[Main] Requesting initial preset from LLM...")
    dispatcher.request_preset(get_situation(time_step, 0), cancel_event,
                              model=_model, extra_latency=extra_latency)
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
            cancel_event = threading.Event()
            dispatcher.request_preset(get_situation(time_step, 0), cancel_event,
                                      model=_model, extra_latency=extra_latency)
            _wait_t0 = time.time()
        time.sleep(0.1)

    threading.Thread(
        target=mission_worker,
        args=(mission_stop, mission_results, mission_lock, _model, extra_latency),
        daemon=True,
    ).start()

    timing = {
        "fault_inject_step": None,
        "interrupt_fired_step": None,
        "emergency_submitted_step": None,
        "emergency_loaded_step": None,
        "recovery_complete_step": None,
        "fault_inject_time": None,
        "interrupt_fired_time": None,
        "emergency_submitted_time": None,
        "emergency_loaded_time": None,
        "recovery_complete_time": None,
    }

    frames = []
    last_situation = ""
    last_llm_request_step = -COOLDOWN_STEPS
    t_wall_start = time.time()
    llm_calls = 1

    fault_injected        = False
    # Separate from fault_injected (briefly False again after recovery):
    # latches permanently True the first time injection actually happens so
    # the >= FAULT_START_STEP condition below cannot re-fire and re-inject
    # every time recovery completes and a new preset loads.
    fault_injection_attempted = False
    emergency_submitted   = False
    # Whatever situation was last requested before the fault (almost always
    # "walk forward", since FAULT_START_STEP=500 falls within that phase) --
    # re-issued verbatim once the emergency reset preset finishes, so the
    # robot resumes its last pre-fault command instead of falling through to
    # get_situation()'s step-indexed schedule, which by the time recovery
    # happens has long since advanced past the walking phase and would
    # otherwise request standing still.
    pre_fault_situation = None
    resume_pending = False

    step_log = []
    cumulative_reward = 0.0
    steps_upright = 0

    import queue as q_mod
    emergency_preset_queue = q_mod.Queue()
    # Incremented by emergency_worker (under emergency_lock) once per attempt,
    # so llm_calls accounts for internal retries instead of always counting
    # the emergency phase as exactly one call.
    emergency_attempt_count = [0]

    t_end = t_wall_start + TOTAL_DURATION_SEC
    step = 0
    while time.time() < t_end:

        # ── 1. Inject fault ───────────────────────────────────────────────
        # Gated on a preset actually being active (see poc_interrupt.py for
        # full rationale): with none loaded, intended == sent == zero, so
        # the fault has no observable effect and the interrupt can never
        # fire. `>=` (not `==`) so injection still happens as soon as a
        # preset becomes active, whenever that is. Gated on
        # fault_injection_attempted (not fault_injected), which never goes
        # back to False, so this can only ever fire once per trial.
        if step >= FAULT_START_STEP and not fault_injection_attempted and dispatcher._current_preset is not None:
            dispatcher.fault_legs.add(fault_leg_idx)
            fault_injected = True
            fault_injection_attempted = True
            timing["fault_inject_step"] = step
            timing["fault_inject_time"] = time.time()
            pre_fault_situation = last_situation
            print(f"[Main] FAULT INJECTED at step {step} - {LEG_NAMES[fault_leg_idx]} leg zeroed")

        # ── 2. Step physics ───────────────────────────────────────────────
        time_step, action = dispatcher.step()
        if time_step.last():
            time_step = _reset_standing(env)

        reward  = time_step.reward or 0.0
        upright = float(np.array(time_step.observation["torso_upright"]))
        speed   = float(np.linalg.norm(np.array(time_step.observation["torso_velocity"])))
        skill   = dispatcher._current_preset["skill_name"] if dispatcher._current_preset else "none"
        ego     = np.array(time_step.observation["egocentric_state"])

        cumulative_reward += reward
        if upright > 0.5:
            steps_upright += 1

        # ── 3. Submit emergency immediately on interrupt (NO mission cancel) ──
        if interrupt_event.is_set() and not emergency_submitted:
            timing["interrupt_fired_step"] = step
            timing["interrupt_fired_time"] = time.time()
            print(f"[Main] INTERRUPT at step {step} - submitting emergency (mission NOT cancelled)")

            # Abort any stale, pre-fault normal preset request still in flight
            # (queued behind mission calls in OLLAMA_WORKER) so it cannot
            # silently land in dispatcher._preset_queue during the fault
            # window and be executed as if it were the fault-aware recovery.
            # The mission-planning stream itself is deliberately NOT cancelled
            # here -- that is the entire point of this baseline.
            cancel_event.set()
            OLLAMA_WORKER.clear_pending()
            dispatcher.clear_queue()
            dispatcher._current_preset = None
            cancel_event = threading.Event()

            s0, s1 = LEG_EGO_SLICE[fault_leg_idx]
            leg_pos = ego[s0:s1].round(3).tolist()
            reconnect_situation = build_reconnect_situation(fault_leg_idx, leg_pos)

            threading.Thread(
                target=emergency_worker,
                args=(reconnect_situation, emergency_preset_queue,
                      emergency_result, emergency_lock, _model, extra_latency,
                      5, emergency_attempt_count),
                daemon=True,
            ).start()
            # llm_calls is incremented at the end from emergency_attempt_count
            # instead of here, since emergency_worker may retry internally.
            emergency_submitted = True
            timing["emergency_submitted_step"] = step
            timing["emergency_submitted_time"] = time.time()
            monitor.suppress = True
            interrupt_event.clear()

        # ── 4. Check if emergency preset has arrived ──────────────────────
        if emergency_submitted and timing["recovery_complete_step"] is None:
            try:
                preset = emergency_preset_queue.get_nowait()
                timing["emergency_loaded_step"] = step
                timing["emergency_loaded_time"] = time.time()
                dispatcher.force_preset(preset)
                dispatcher.fault_legs.discard(fault_leg_idx)
                fault_injected = False
                monitor.suppress = False
                timing["recovery_complete_step"] = step
                timing["recovery_complete_time"] = time.time()
                resume_pending = True
                print(f"[Main] RECOVERY COMPLETE at step {step} - {LEG_NAMES[fault_leg_idx]} re-enabled")
            except q_mod.Empty:
                pass

        # ── 5. Log ────────────────────────────────────────────────────────
        step_log.append({
            "step": step,
            "upright": round(upright, 4),
            "speed": round(speed, 4),
            "reward": round(reward, 4),
            "skill_name": skill,
            "fault_active": fault_injected,
            "emergency_pending": emergency_submitted and timing["recovery_complete_step"] is None,
        })

        if save_video and step % EVERY_N == 0:
            frames.append(env.physics.render(height=HEIGHT, width=WIDTH, camera_id=CAMERA_ID))

        if step % 100 == 0:
            fault_tag = " [FAULT]" if fault_injected else ""
            emrg_tag  = " [EMERGENCY_PENDING]" if (emergency_submitted and timing["recovery_complete_step"] is None) else ""
            print(f"  step={step:4d}{fault_tag}{emrg_tag} | skill={skill:30s} | "
                  f"upright={upright:.2f} | speed={speed:.3f}")

        # ── 6. Normal LLM request (only when not in emergency) ───────────
        if not emergency_submitted or timing["recovery_complete_step"] is not None:
            cycles_left = dispatcher.cycles_remaining
            near_end = cycles_left is not None and cycles_left <= PREEMPT_CYCLES
            if resume_pending:
                situation = pre_fault_situation
            else:
                situation = get_situation(time_step, step, cycles_left if near_end else None)
            if ((dispatcher._current_preset is None or situation != last_situation or near_end)
                    and dispatcher._preset_queue.empty()
                    and (step - last_llm_request_step) >= COOLDOWN_STEPS):
                # Cancel the previous periodic request before submitting a
                # new one, so unanswered retries (e.g. while queued behind a
                # long mission call) do not pile up in OLLAMA_WORKER faster
                # than they can ever be served.
                cancel_event.set()
                OLLAMA_WORKER.clear_pending()
                cancel_event = threading.Event()
                dispatcher.request_preset(situation, cancel_event,
                                          model=_model, extra_latency=extra_latency)
                last_situation = situation
                last_llm_request_step = step
                llm_calls += 1
                resume_pending = False

        step += 1

    total_steps = step
    mission_stop.set()
    monitor.stop()
    llm_calls += emergency_attempt_count[0]
    wall_time = time.time() - t_wall_start
    upright_rate = steps_upright / total_steps if total_steps > 0 else 0.0

    fault_step    = timing["fault_inject_step"]
    emrg_sub_step = timing["emergency_submitted_step"]
    emrg_load_step = timing["emergency_loaded_step"]
    recovery_step  = timing["recovery_complete_step"]

    detect_latency     = (timing["interrupt_fired_step"] - fault_step) if timing["interrupt_fired_step"] and fault_step else None
    emrg_wait_steps    = (emrg_load_step - emrg_sub_step) if emrg_load_step and emrg_sub_step else None
    total_recovery     = (recovery_step - fault_step) if recovery_step and fault_step else None

    # Wall-clock deltas (directly measured via time.time(), seconds)
    fault_t, interrupt_t, emrg_sub_t, emrg_load_t, recovery_t = (
        timing["fault_inject_time"], timing["interrupt_fired_time"],
        timing["emergency_submitted_time"], timing["emergency_loaded_time"],
        timing["recovery_complete_time"],
    )
    detect_latency_sec = round(interrupt_t - fault_t, 3) if (interrupt_t and fault_t) else None
    emrg_wait_sec = round(emrg_load_t - emrg_sub_t, 3) if (emrg_load_t and emrg_sub_t) else None
    total_recovery_sec = round(recovery_t - fault_t, 3) if (recovery_t and fault_t) else None

    print("\n=== Baseline Fair (Immediate Emergency, No Preemption) Results ===")
    print(f"  Fault leg              : {LEG_NAMES[fault_leg_idx]}")
    print(f"  Fault injected         : step {fault_step}")
    print(f"  Emergency submitted    : step {emrg_sub_step}  (detect latency: {detect_latency} steps / {detect_latency_sec}s)")
    print(f"  Emergency preset loaded: step {emrg_load_step}  (LLM wait: {emrg_wait_steps} steps / {emrg_wait_sec}s)")
    print(f"  Recovery complete      : step {recovery_step}  (total: {total_recovery} steps / {total_recovery_sec}s)")
    print(f"  Mission plans complete : {sum(1 for r in mission_results if r['status']=='complete')}")
    print(f"  LLM model              : {_model}")
    print(f"  LLM extra latency      : {extra_latency}s")
    print(f"  Total wall time        : {wall_time:.1f}s")
    print(f"  Upright rate           : {upright_rate * 100:.1f}%")

    RESULTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    leg_tag = LEG_NAMES[fault_leg_idx]
    stem = f"baseline_fair_{leg_tag}_{ts}"

    summary = {
        "method": "baseline_fair",
        "fault_leg": leg_tag,
        "timestamp": ts,
        "llm_model": _model,
        "llm_latency_sec": extra_latency,
        "total_steps": total_steps,
        "fault_start_step": fault_step,
        "interrupt_fired_step": timing["interrupt_fired_step"],
        "emergency_submitted_step": emrg_sub_step,
        "emergency_loaded_step": emrg_load_step,
        "recovery_complete_step": recovery_step,
        "detect_latency_steps": detect_latency,
        "emergency_wait_steps": emrg_wait_steps,
        "total_recovery_steps": total_recovery,
        "fault_inject_time": fault_t,
        "interrupt_fired_time": interrupt_t,
        "emergency_submitted_time": emrg_sub_t,
        "emergency_loaded_time": emrg_load_t,
        "recovery_complete_time": recovery_t,
        "detect_latency_sec": detect_latency_sec,
        "emergency_wait_sec": emrg_wait_sec,
        "total_recovery_sec": total_recovery_sec,
        "llm_calls": llm_calls,
        "mission_complete": sum(1 for r in mission_results if r["status"] == "complete"),
        "mission_results": mission_results,
        "wall_time_sec": round(wall_time, 2),
        "cumulative_reward": round(cumulative_reward, 4),
        "steps_upright": steps_upright,
        "upright_rate": round(upright_rate, 4),
    }
    with open(RESULTS_DIR / f"{stem}.json", "w") as f:
        json.dump({"summary": summary, "steps": step_log}, f, indent=2)

    with open(RESULTS_DIR / f"{stem}.csv", "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["step", "upright", "speed", "reward", "skill_name",
                           "fault_active", "emergency_pending"]
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
    parser.add_argument("--no-video", action="store_true")
    parser.add_argument("--fault-leg", type=int, default=0, choices=[0, 1, 2, 3],
                        help="0=FL 1=FR 2=BL 3=BR")
    parser.add_argument("--model", type=str, default=None,
                        help="Ollama model name (default: HERA_MODEL env or qwen3.5:9b)")
    parser.add_argument("--latency", type=float, default=0.0,
                        help="Artificial LLM latency in seconds (default: 0)")
    args = parser.parse_args()
    main(save_video=not args.no_video, fault_leg_idx=args.fault_leg,
         model_name=args.model, extra_latency=args.latency)
