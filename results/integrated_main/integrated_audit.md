# Integrated command lifecycle audit

Audited 160 manifested trials; integrity/timing errors: 0.

Acceptance is not application, and application is not completion of the prescribed duration. Fixed-horizon truncation is retained and is not an infrastructure failure.

| Model | Method | N | Accepted | Applied | Full duration | Horizon truncated | Expiry observed | Physically safe |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| llama3.2:3b | backend_failure | 32 | 0 | 0 | 0 | 0 | 0 | 32 |
| llama3.2:3b | fifo_single | 32 | 32 | 32 | 32 | 0 | 32 | 32 |
| llama3.2:3b | hera_preempt | 32 | 32 | 32 | 32 | 0 | 32 | 32 |
| llama3.2:3b | local_only | 32 | 0 | 0 | 0 | 0 | 0 | 32 |
| llama3.2:3b | reserved_slot | 32 | 32 | 32 | 32 | 0 | 32 | 32 |

Without an accepted command, duration/expiry counts are not applicable rather than supervisor failures.

| Method | Detect to accept mean (ms) | Accept to apply mean (ms) | Detect to apply mean (ms) | Local detect to apply mean (ms) |
|---|---:|---:|---:|---:|
| backend_failure | N/A | N/A | N/A | 19.486 |
| fifo_single | 7144.086 | 30.027 | 7174.113 | 19.744 |
| hera_preempt | 493.627 | 29.810 | 523.437 | 19.711 |
| local_only | N/A | N/A | N/A | 19.366 |
| reserved_slot | 556.898 | 28.865 | 585.763 | 19.564 |

## Fixed-horizon cases

| Trial | Requested / observed steps | Full duration | Expiry observed |
|---|---:|---|---|

Runtime-given command reaffirms the same local support. Acceptance, application, complete duration, and observed expiry are distinct endpoints. No additional physical benefit or LLM-discovered control is inferred. Retain the original fixed horizon.

The JSON includes every manifested trial, exact artifact paths and hash checks, mission activity, timing ranges, and endpoint definitions. Inference precision is not estimated from this supplemental audit.
