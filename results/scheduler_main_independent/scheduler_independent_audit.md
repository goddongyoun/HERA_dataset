# Independent scheduler audit

Manifested trials: 288; audit errors: 0. No experiment implementation was imported.

The audit checks full independently constructed canonical commands, worker/events hashes, event sequence and request generation, active-mission barrier, absolute deadline origin, raw acceptance timing, policy-specific cancellation/dispatch order, and reported output-token controls.

| Token cap | Method | Mean acceptance (s) | Queue (ms) | First frame (ms from fault) | First frame to acceptance (ms) |
|---:|---|---:|---:|---:|---:|
| 256 | hera_preempt | 0.486391 | 0.236 | 61.690 | 424.701 |
| 256 | fifo_single | 1.476245 | 984.655 | 1051.605 | 424.640 |
| 256 | reserved_slot | 0.542976 | 0.303 | 50.649 | 492.327 |
| 1024 | hera_preempt | 0.485864 | 0.230 | 58.853 | 427.011 |
| 1024 | fifo_single | 5.239603 | 4704.577 | 4813.362 | 426.241 |
| 1024 | reserved_slot | 0.544168 | 0.284 | 51.623 | 492.545 |
| 2048 | hera_preempt | 0.512009 | 0.219 | 59.764 | 452.245 |
| 2048 | fifo_single | 11.100965 | 10471.789 | 10648.487 | 452.478 |
| 2048 | reserved_slot | 0.575051 | 0.288 | 53.421 | 521.630 |

## Timing qualification

Nominal ready-to-fault delay is 250 ms. The measured range is 219.3589–254.9305 ms, mean 237.8150 ms; 270/288 fall below 250 ms. Report the nominal setting and actual range rather than claiming an exact offset. Acceptance latency uses the actual logged fault timestamp, and all audited missions remain active at that timestamp.

## Statistical and scope qualifications

Each workload has eight batches and four paired legs per batch. Independently resampled balanced batch means using 5000 draws, seed 20260907, percentile interpolation. Same cluster draw sequence, independently aggregated values; negative latency difference favors left. No multiplicity correction or population-general confidence interpretation.

Independent canonical construction and event verification use no experiment source imports. Scheduler acceptance is not plant application. Cancelled streams lack final backend eval_count; response chars are not tokens. Client connection close is not server/GPU idle attestation.

The token-cap workloads were collected in separate sequential workload blocks, so between-workload differences can also include time-dependent host/backend drift. Within each workload the method contrasts are paired by batch, leg, and seed. A batch bootstrap accounts for within-batch clustering, not arbitrary cross-batch temporal dependence.

The canonical command is supplied by the runtime and the accepted payload is audited from persisted artifacts. This is a formatting/scheduling benchmark, not evidence that the model discovered a safe controller. All observed acceptances are before 30 s; that does not prove a population success probability of one. Mission throughput, wasted generation after cancellation, GPU resource isolation, and server-side quiescence are not established by these client events.
