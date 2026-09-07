# HERA revision 3: frozen experimental protocol

## Scope and decision rules

This revision supersedes v2 for new paper evidence; v2 runs remain immutable historical development data. The objective is an auditable, manuscript-usable empirical evaluation, not obtaining a positive result or guaranteeing journal acceptance. Pilot runs are implementation checks, never pooled with the main matrix. If code or a scientific condition changes after main execution begins, create a new version/campaign and disclose the deviation; do not selectively rerun completed scientific failures.

All results are descriptive/exploratory. No confirmatory p-values or post-hoc power claims. The fixed sample size is an engineering coverage/precision design: eight complete randomized temporal batches per scheduler workload and integrated study, four legs and all methods per batch; the physics study enumerates all 20 gait phases and four legs per profile. This is not a claim that seed, yaw, legs, or consecutive same-host batches are independent draws from a robotic population. Primary reports include batch/leg summaries and paired effects. Physics finite-grid results describe the tested grid, not a stochastic population.

The main matrix is generated once with master seed 20260908. The pilot uses a separate seed 20261908. Manifest bytes/order, code, environment, model digest, adopted attempts, events and traces are hashed. Main is 1472 trials: scheduler 288; offline physics 960; realtime physics audit 64; integrated 160. Missing/infrastructure errors must be resolved or explicitly reported; scientific failures stay in denominators. No success-dependent stopping or cherry-picked conditions.

## Common platform

Windows, CPython 3.12, dm_control quadruped/walk, MuJoCo, local Ollama llama3.2:3b. The environment lock and per-run provenance are the authoritative versions. Dedicated ports 11435 and 11436 use actual parallel-1 and parallel-2 runners with context 8192 on the same GPU. Every LLM batch reloads/warms/verifies both servers, storing logs. A parallel slot is not an independent GPU or hard real-time priority reservation.

Latency uses perf_counter_ns/QueryPerformanceCounter; nanosecond storage is not nanosecond measurement accuracy. UTC correlates logs only. Physics dt=0.02s. Realtime runs use absolute-deadline pacing; offline grid wall timings are not latency evidence.

## Study A: canonical-command scheduling

Three methods: hera_preempt (cancel mission then same lane), fifo_single (wait for mission), reserved_slot (two client lanes and actual parallel-2). Three separately analyzed mission token budgets: 256, 1024, 2048. Per workload: 8 randomized complete batches x 4 legs x 3 methods = 96 trials. Temperature 0; emergency token cap 512; one attempt; fault barrier is mission first valid frame followed by 0.25s; mission must remain active. Deadline is 30s from fault and includes cancellation, queue, generation and validation.

The complete emergency payload is bound to a runtime canonical contract: one phase, exact safe actuator array, exact skill name, duration_steps=1, stop_condition=duration_steps_elapsed, one-shot and max_cycles=null. Semantic validation independently checks the contract. This deliberately measures request scheduling and serialization of known commands, NOT diagnosis, action discovery, or unconstrained LLM reliability.

Primary endpoint: fault-to-fully-validated-acceptance. Every failure/error/missing trial receives its manifest deadline in a descriptive penalized mean. Conditional successful-only means/SD/median/IQR are also labeled. Secondary: queue, fault-to-dispatch, first frame/content, cancellation completion, backend token counts, mission active/completion/cancellation. Preregistered retrospective deadline sensitivity at 1,2,5,10,30s is calculated from the same observed times; it is NOT a fresh experiment with those timeout settings. First valid acceptance is immutable. NoSuppress's historical overwritten time is not reused; old quantitative ablation is removed from new evidence, not claimed repaired.

Server logs verify runner configuration. Without backend internal request-ID timing, conclusions remain client-observed cancellation and response latency; immediate GPU slot release is not claimed.

## Study B: measured physical faults and deterministic local response

Methods: local_reflex and no_reflex. The controller never reads ground-truth fault flags to select a response. Detector: nominal effort residual plus proprioceptive/IMU residuals, unchanged v2 thresholds/weights and 5-step trigger debounce. The physical fault persistently scales the selected leg's actuator gain and length bias; command arrays are not corrupted or repaired. Initialization has one reset, deterministic upright yaw, and 300 stand-settle steps; no measured-phase reset. The controller is an open-loop preset, not a CPG.

Each profile covers four legs x all phase offsets 0..19 x two methods = 160 trials; 960 total. Duration 10 simulated seconds; reference/fault time 3s. Conditions:
1. full_clean: actuator strength 0.
2. partial_clean: strength 0.5.
3. healthy_clean: no actuator fault.
4. full_noisy: strength 0 plus specified measurement noise.
5. healthy_noisy: no fault plus identical noise.
6. healthy_push: no actuator fault, lateral torso force 40N for 0.2s starting at 3s.

Noise affects copied detector measurements only, never ground-truth outcome measurements. Independent zero-mean Gaussian noise: effort std=0.05 x max(abs(nominal predicted effort), detector minimum expected force); joint position 0.002rad; joint velocity 0.02rad/s; gyro 0.01rad/s; acceleration 0.05m/s^2. Its seed is deterministic and shared by paired methods; differing trajectories can change effort-dependent noise scale. These are synthetic stress assumptions, not calibrated hardware sensor specifications. Lateral force is in initial-body lateral direction; push is NOT labeled an actuator fault. False alarms during healthy/push profiles are retained.

Physical endpoints use the same 3..10s horizon even for healthy trials. Prefault eligibility: final50 prefault samples upright>=.95 fraction>=.95, max gyro<=.50rad/s, min torso height>=.35m. Ineligible trials remain. Safety requires eligibility, >=50 postreference samples, no five-consecutive-sample fall (upright<.30 or height<.18m), postreference upright>=.80 fraction>=.95, and final50 sample upright>=.80 AND height>=.30m fraction>=.95. Safety is a fixed-horizon operational simulation criterion, not time-to-recovery or a hardware guarantee.

Primary physical descriptions: safety fraction, fall, upright-deficit integral sum(max(0,1-upright))*dt, minimum upright and height. Also report body movement/rotation and displacement; velocity norm is not forward mission progress. Detection accuracy, false triggers, prethreshold excursions and correct reflex application are separate from physical success. Healthy true-positive/latency fields are N/A, not true positives. Report all healthy exposure durations and any false-response time.

Time correction: fault injected before step k, detection measured after step j. Integrated simulation delay is (j+1-k)*dt, NOT (j-k)*dt. Wall-clock source timestamps are separately retained. A realtime audit uses eight fixed phases [0,2,5,7,10,12,15,17], all legs and both methods (64 trials), 10s each; only these and integrated trials support wall latency descriptions.

Measurement synchronization: after every environment step, physics.forward() refreshes force-dependent fields to the same post-integration activation/transmission state before the completion timestamp and sensor capture. It does not advance simulated time, qpos, qvel or activation. This corrects stale derived fields in dm_control legacy stepping; the additional forward computation is included in measured wall time. The fixed-gain/affine-bias, non-early-activation model is checked explicitly, and nominal predicted effort respects enabled actuator force clamps. Each fault records actual nominal/faulted gain and bias arrays and the actual injection phase. Regression tests verify dynamic healthy and half-strength force agreement; no detector threshold is retuned. Pre-correction pilot_002 is excluded from main evidence (see DEVIATIONS.md).

## Study C: live integration and failure containment

Five methods: local_only, hera_preempt, fifo_single, reserved_slot, backend_failure; eight randomized complete batches x four legs =160. Duration 12s, physical fault at3s, mission cap2048, supervisory deadline9s from measured detector timestamp. All methods have the SAME immediate deterministic local reflex. Physics runs in its own control thread and never waits for LLM cancellation/response. The actual measured detector event supplies leg, measured efforts, and original monotonic timestamp to the scheduler. Mission first-frame opens the plant start barrier. If the backend fails its start, the local plant still runs. Mission-active-at-detection is recorded, not assumed.

Supervisory output is intentionally a fully constrained 50-step (1s) reaffirmation of the same trusted tripod support command. It is copied only after full validation, applied on a following control tick, then expires to the local fallback if the measured horizon continues. A late application can be truncated by the fixed 12s horizon: requested duration, observed applied steps, full-command completion, horizon censoring, and observed expiry/fallback are reported separately. The horizon is not extended to manufacture a complete application. Every applied array, source, step and control-start timestamp is traced and matched against the accepted payload. This tests integration/application timing and separation of safety from inference. It does NOT test free replanning, LLM-discovered controls, or extra physical benefit beyond local_only. The study cannot support those claims even if all trials succeed.

backend_failure uses real mission inference and explicitly injects a ConnectionError on the emergency stream. It is a declared transport-failure intervention, not a naturally observed server failure. local_only has no LLM request. Primary reporting separates physical safety/local response timing from supervisor accepted/applied fraction and detector-to-application time. Report failures, delivery jitter, pacing lateness, application durations, and any stale/pre-validation application. Application before acceptance or mismatching arrays is an integrity failure, never a scientific success.

## Analysis and delivery acceptance

Analyze only exact manifest IDs, no timestamp/file-glob selection. Restore missing rows for denominators. Paired differences are within exact profile/load, block, leg and seed. Scheduler/integrated temporal uncertainty: 5000 batch-cluster bootstrap draws, seed20260907, descriptive only; eight batches from one host/session do not establish deployment generality. Offline phase-grid bootstrap is not interpreted as statistical inference; report exact means/ranges/paired counts over the enumerated grid. All-zero difference intervals do not prove equivalence or zero failure risk.

Final acceptance requires unit/regression tests; all manifests accounted for; reanalysis matching saved output; event/trace/source identity validation; simulation metrics recomputed from raw traces; applied-action linkage audit; readable tables/figures and English methods/results/limitations with matching numbers; explicit review-response disposition. Public repository/DOI publication and submission require author actions and are not performed by this local workflow.
