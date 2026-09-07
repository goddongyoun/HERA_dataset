# Scheduler results

All results are descriptive for one model, host and backend. The payload is a supplied canonical command, not discovered recovery.

Latency summaries below use all manifested trials; any nonaccepted trial contributes its 30 s deadline, not a fictitious observed acceptance.

| Mission token cap | Method | Accepted / N | Penalized mean (s) | Penalized SD (s) | Penalized median (s) |
|---:|---|---:|---:|---:|---:|
| 256 | Preempt | 32/32 | 0.4864 | 0.0098 | 0.4882 |
| 256 | FIFO | 32/32 | 1.4762 | 0.0158 | 1.4776 |
| 256 | Parallel slot | 32/32 | 0.5430 | 0.0059 | 0.5437 |
| 1024 | Preempt | 32/32 | 0.4859 | 0.0086 | 0.4862 |
| 1024 | FIFO | 32/32 | 5.2396 | 0.0154 | 5.2428 |
| 1024 | Parallel slot | 32/32 | 0.5442 | 0.0054 | 0.5442 |
| 2048 | Preempt | 32/32 | 0.5120 | 0.0166 | 0.5161 |
| 2048 | FIFO | 32/32 | 11.1010 | 0.2800 | 11.2102 |
| 2048 | Parallel slot | 32/32 | 0.5751 | 0.0135 | 0.5791 |

## Paired differences

| Token cap | Contrast | Mean difference (s) | Batch-bootstrap 95% interval (s) |
|---:|---|---:|---|
| 256 | hera_preempt_minus_fifo_single | -0.98985 | [-0.99401, -0.98559] |
| 256 | hera_preempt_minus_reserved_slot | -0.05658 | [-0.05884, -0.05448] |
| 256 | reserved_slot_minus_fifo_single | -0.93327 | [-0.93863, -0.92794] |
| 1024 | hera_preempt_minus_fifo_single | -4.75374 | [-4.76274, -4.74449] |
| 1024 | hera_preempt_minus_reserved_slot | -0.05830 | [-0.06268, -0.05486] |
| 1024 | reserved_slot_minus_fifo_single | -4.69543 | [-4.70185, -4.68784] |
| 2048 | hera_preempt_minus_fifo_single | -10.58896 | [-10.72493, -10.39848] |
| 2048 | hera_preempt_minus_reserved_slot | -0.06304 | [-0.06983, -0.05626] |
| 2048 | reserved_slot_minus_fifo_single | -10.52591 | [-10.65744, -10.33745] |

The deadline figure re-evaluates one set of observed 30-second-deadline trials; it is not a rerun at each timeout. No failed trial is dropped. Intervals are exploratory batch-cluster summaries, not confirmatory significance or deployment-general confidence.
