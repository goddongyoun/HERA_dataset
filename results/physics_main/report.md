# Physics and integrated execution audit

Verified **1184/1184** manifested physics/integrated trials; 608000 raw trace samples inspected. No trial selection by timestamp or directory glob.

| Study / profile / method | Verified / expected | Safety successes | False-trigger trials | Mean deficit (s) | Median deficit (s) | Terminal speed (m/s) |
|---|---:|---:|---:|---:|---:|---:|
| integrated / integrated / backend_failure | 32/32 | 32 | 0 | 0.1478 | 0.00146 | 0.08919 |
| integrated / integrated / fifo_single | 32/32 | 32 | 0 | 0.1478 | 0.00146 | 0.08919 |
| integrated / integrated / hera_preempt | 32/32 | 32 | 0 | 0.1478 | 0.00146 | 0.08919 |
| integrated / integrated / local_only | 32/32 | 32 | 0 | 0.1478 | 0.00146 | 0.08919 |
| integrated / integrated / reserved_slot | 32/32 | 32 | 0 | 0.1478 | 0.00146 | 0.08919 |
| physics / full_clean / local_reflex | 80/80 | 80 | 0 | 0.07009 | 0.001544 | 0.1022 |
| physics / full_clean / no_reflex | 80/80 | 80 | 0 | 0.192 | 0.2322 | 0.49 |
| physics / full_noisy / local_reflex | 80/80 | 80 | 0 | 0.07344 | 0.001539 | 0.1024 |
| physics / full_noisy / no_reflex | 80/80 | 80 | 0 | 0.1929 | 0.2251 | 0.492 |
| physics / healthy_clean / local_reflex | 80/80 | 80 | 0 | 1.751e-06 | 2.83e-15 | 0.1904 |
| physics / healthy_clean / no_reflex | 80/80 | 80 | 0 | 1.751e-06 | 2.83e-15 | 0.1904 |
| physics / healthy_noisy / local_reflex | 80/80 | 80 | 0 | 2.205e-06 | 2.643e-15 | 0.1903 |
| physics / healthy_noisy / no_reflex | 80/80 | 80 | 0 | 2.205e-06 | 2.643e-15 | 0.1903 |
| physics / healthy_push / local_reflex | 80/80 | 80 | 0 | 0.0005489 | 0.0005486 | 0.2186 |
| physics / healthy_push / no_reflex | 80/80 | 80 | 0 | 0.0005489 | 0.0005486 | 0.2186 |
| physics / partial_clean / local_reflex | 80/80 | 80 | 0 | 0.004965 | 0.004955 | 0.2277 |
| physics / partial_clean / no_reflex | 80/80 | 80 | 0 | 0.004965 | 0.004955 | 0.2277 |
| physics / realtime_audit / local_reflex | 32/32 | 32 | 0 | 0.0826 | 0.001608 | 0.09792 |
| physics / realtime_audit / no_reflex | 32/32 | 32 | 0 | 0.1942 | 0.2321 | 0.5002 |

Safety/figure rates retain the manifested denominator; observed healthy false-alarm rates and continuous metrics use verified observations only. False-trigger counts are reported for every profile; only the healthy profiles are fault-free false-alarm tests.

## Interpretation

The enumerated phase/leg grid is descriptive, not independent stochastic replication; no bootstrap interval or p-value is inferred here. Physical safety is a fixed-horizon criterion, not time to locomotion recovery. Integrated accepted commands reaffirm the same canonical support action as the local reflex; these runs establish bounded command application and failure containment, not extra physical benefit or LLM-discovered control. Velocity norm is not forward progress. Detector noise is synthetic and partial. Detector-state replay starts from stored scalar scores; traces do not contain all raw per-actuator/per-joint sensor channels needed to independently reconstruct the weighted sensor-residual formula. That force-model equality is covered by separate dynamic simulator regression tests, not claimed as a full raw-trace sensor replay.

The report checks fixed-horizon safety, continuous physical endpoints, healthy pseudo-reference handling, end-of-integration simulation latency, exact validated control arrays, acceptance/application chronology, finite command duration and local fallback directly against JSONL samples and events.

Detector state is independently replayed from every saved score using the stored entry/exit thresholds and consecutive-sample counts. Every active-leg set, trigger classification, first correct detection, false-trigger event and correct/missed endpoint is checked. Clear transitions are checked through the saved active-state sequence; this experiment source emits no separate clear events. This is not a full raw-sensor score-formula reconstruction.

## Detector endpoints (independent score-state replay)

| Study / profile / method | Correct / applicable verified | Correct-leg detection event | Missed detection | False-trigger trials |
|---|---:|---:|---:|---:|
| integrated / integrated / backend_failure | 32/32 | 32 | 0 | 0 |
| integrated / integrated / fifo_single | 32/32 | 32 | 0 | 0 |
| integrated / integrated / hera_preempt | 32/32 | 32 | 0 | 0 |
| integrated / integrated / local_only | 32/32 | 32 | 0 | 0 |
| integrated / integrated / reserved_slot | 32/32 | 32 | 0 | 0 |
| physics / full_clean / local_reflex | 80/80 | 80 | 0 | 0 |
| physics / full_clean / no_reflex | 80/80 | 80 | 0 | 0 |
| physics / full_noisy / local_reflex | 80/80 | 80 | 0 | 0 |
| physics / full_noisy / no_reflex | 80/80 | 80 | 0 | 0 |
| physics / healthy_clean / local_reflex | N/A | N/A | N/A | 0 |
| physics / healthy_clean / no_reflex | N/A | N/A | N/A | 0 |
| physics / healthy_noisy / local_reflex | N/A | N/A | N/A | 0 |
| physics / healthy_noisy / no_reflex | N/A | N/A | N/A | 0 |
| physics / healthy_push / local_reflex | N/A | N/A | N/A | 0 |
| physics / healthy_push / no_reflex | N/A | N/A | N/A | 0 |
| physics / partial_clean / local_reflex | 0/80 | 0 | 80 | 0 |
| physics / partial_clean / no_reflex | 0/80 | 0 | 80 | 0 |
| physics / realtime_audit / local_reflex | 32/32 | 32 | 0 | 0 |
| physics / realtime_audit / no_reflex | 32/32 | 32 | 0 | 0 |

Correct detection additionally requires correct first selection and no false trigger. A later correct-leg event can therefore occur in a detector-incorrect trial. Healthy controls have no true-detection target (N/A); every trigger is false. A missed partial-strength fault remains a detector miss even when fixed-horizon physical safety succeeds. Denominators here include verified applicable trials only; missing observations remain in the audit above.

Phase plots display the configured gait offset. The actual injection (or healthy pseudo-reference) phase is `(reference_step + offset) % 20`; report.json records both axes and full-20-phase coverage. Local-only supervisory application is N/A. The backend-failure condition deliberately expects no supervisory application; neither is included in the successful-supervisor denominator.

## Healthy-control exposure

| Profile / method | Trials | Observation (sim s) | Pre / post-reference (sim s) | First-trigger at risk (sim s) | False local-response trials | False local-response duty |
|---|---:|---:|---:|---:|---:|---:|
| physics / healthy_clean / local_reflex | 80 | 800 | 240 / 560 | 800 | 0 | 0 |
| physics / healthy_clean / no_reflex | 80 | 800 | 240 / 560 | 800 | 0 | 0 |
| physics / healthy_noisy / local_reflex | 80 | 800 | 240 / 560 | 800 | 0 | 0 |
| physics / healthy_noisy / no_reflex | 80 | 800 | 240 / 560 | 800 | 0 | 0 |
| physics / healthy_push / local_reflex | 80 | 800 | 240 / 560 | 800 | 0 | 0 |
| physics / healthy_push / no_reflex | 80 | 800 | 240 / 560 | 800 | 0 | 0 |

Observation includes post-response closed-loop time; first-trigger at-risk time ends at the end-of-step detection sample or is censored at the fixed horizon. False local-response onset is the start of the first affected control step. These are descriptive simulated-time denominators, not independent or stationary hazard estimates. Per-trial onset and pre/post at-risk exposure are retained in report.json.

Measurement synchronization metadata: `mj_forward_post_integration`: 1184 trials. An unrecorded sampling-sync mode must be resolved against the frozen source before citing detector residuals as calibrated measurements.

## Figures

![physics_paired_phase_grid.png](physics_paired_phase_grid.png)

![physics_profile_outcomes.png](physics_profile_outcomes.png)

![physics_realtime_latency.png](physics_realtime_latency.png)

![integrated_application_timeline.png](integrated_application_timeline.png)

## Provenance

Report script SHA-256: `4808829dc631d546605bf2d77810c14ade699100a6bd41bfdb822312a85ea5d7`. The accompanying report.json records the script, source campaign, exact input hashes, per-trial audit results and recomputed outcomes.
