# Development/pilot deviations

## pilot_001 — source-freeze guard stop, 2026-09-07 20:42:55 KST

Three scheduler trials completed before the runner detected that tests/test_integrated.py changed after source snapshot capture. Two regression tests had been added while the freeze notification was in flight; execution/scientific code was unchanged. The source guard correctly stopped the campaign. Preserve all original records. This is an implementation pilot, not paper data. Start pilot_002 with the same pilot manifest bytes and the now-frozen complete test suite; do not resume or overwrite pilot_001.

## pilot_002 — completed pre-synchronization development audit

Completed all 160 trials on 2026-09-07 at 20:54:06 KST, 609.56 seconds, no runner failures, all 11 automatic analyses complete and exactly reproduced using the archived analyzer. This remains development data, never pooled into main results.

The installed dm_control legacy stepping path refreshes kinematic/activation quantities after integration while some force-derived fields still describe an earlier internal step. The observation extractor therefore combined inconsistent state times. Inspection of the installed stepping implementation and dynamic numerical checks established the problem; it was not inferred merely from an unfavorable scientific result.

Before main execution, physics.py now calls physics.forward() immediately after env.step and before the completion timestamp/capture. Tests prove no time/qpos/qvel/activation advance during the forward call, dynamic nominal-force equality for healthy channels, half-strength effort/residual consistency, and enabled force-limit handling. Fixed-gain/affine-bias/non-early-activation assumptions fail fast. Fault parameter arrays, actual injection phase and measurement_sync metadata are recorded. The complete 76-test suite passed after all source/test edits finished. Detector thresholds, matrix, sample size and seed were not changed. pilot_003 reuses identical pilot manifest bytes with a fresh corrected source snapshot; pilot_001 and pilot_002 remain immutable.

Two FIFO supervisory commands in pilot_002 had only 43 and 41 of 50 requested steps observed before the fixed 12s horizon ended. This is horizon censoring, not a missing source command or an excuse to extend the experiment. Reporting now explicitly separates acceptance, observed application, full duration, and observed expiry/fallback. No duration or scheduling policy was changed.

## pilot_003 — corrected implementation qualified before main

The corrected 160-trial pilot completed in 608.81 seconds on 2026-09-07 at approximately 21:09:41 KST. All 11 automatic analyses completed and exactly matched archived-source reanalysis. Independent physical/score-state/command audits passed all 124 physical and integrated records and 64,000 trace samples. Scheduler canonical/timestamp checks passed 36 records; independent integrated checks passed 20; server evidence passed all four lifecycle batches/eight exact raw log prefixes/56 LLM-study records. The complete 76-test suite passed again at 21:10:20 KST with source SHA-256 73b25c2272adadd37b4cfad44bc5cde3b8e2821d13f886f2b1996f8ca463f1c5 unchanged.

Scientific limitations were retained: half-strength faults were missed, fixed-horizon safety was at ceiling in the pilot, and some local-action continuous-metric changes were adverse. These did not trigger threshold tuning, sample expansion, or selection. Pilot-only report generation briefly overlapped execution; pilot wall-jitter observations are therefore not idle-host evidence and are excluded from main estimates of latency performance (pilot elapsed times are used only for operational runtime forecasts). No heavy report generation or simulation regression suite is scheduled concurrently with main scheduler/realtime/integrated measurements. General host load is not guaranteed absent.

The original fixed 1,472-trial main matrix, seed 20260908, was generated once in runs/manifests/main_001 before main launch. No scientific condition changed after qualification.

## main_001 — observed scheduler trigger timing, no implementation change

After all 288 scheduler trials completed, an independent event audit measured the configured nominal 250ms mission-ready-to-trigger wait. Actual monotonic intervals ranged from 219.3589 to 254.9305ms (mean 237.8150ms); 270/288 were below 250ms. The source requests asyncio.sleep(0.25), but an exact wall-clock offset was not achieved. Host/event-loop timer behavior is a plausible contributor; the audit does not establish a unique cause.

All 288 missions were active at the actual trigger. Outcome/deadline calculations use the actual measured trigger timestamp, not an assumed ready+250ms timestamp, and independent raw-event latency/identity checks passed. No trial was discarded, corrected by adding a nominal offset, or rerun; source and settings remain frozen. The manuscript reports a nominal configured wait and its achieved range, not an exact 250ms intervention. This is an observed timing deviation from the target, not a post-outcome change to the experiment.
