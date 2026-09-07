# HERA Experiment Dataset

Dataset supporting the results reported in:

> Dongyeon Kim, Hyunjun Jung. "HERA: Hierarchical Event-driven Reflex Architecture
> for Interrupt-Driven LLM Preemption in Quadruped Robot Control." Submitted to
> *Biomimetics* (MDPI).

This repository is split into two tiers:

```
HERA_dataset/
├── README.md            (this file)
├── summary/             Trial-level CSVs + code + verification script (~19 MB)
└── full/                Complete per-simulation-step raw logs for every trial (~14 GB)
```

Both tiers cover the same experiments and use the identical trial identifiers
(`{method}_{fault_leg}_{timestamp}`), so a row in `summary/primary_experiment.csv`
and the corresponding file in `full/table2_main_comparison/...` describe the
same trial at different levels of detail.

## `summary/` — start here

Trial-level (one row per trial) CSVs covering every number reported in
**Table 2**, **Table 3**, **Figure 5**, the **Cross-Model Evaluation**
(Figure 6), and the **Latency Robustness** sweep (Figure 7), plus the full
per-step trace for the three representative trials plotted in **Figure 4**.
Includes `verify_results.py`, which recomputes every statistic (including
Welch's t-test and Fisher's exact test) directly from these CSVs and was used
to confirm they reproduce the manuscript's numbers exactly.

```
summary/
├── primary_experiment.csv      Table 2 + Figure 5 (36 rows: 3 methods x 12 trials)
├── ablation.csv                 Table 3 (36 rows: 3 variants x 12 trials)
├── cross_model.csv              Figure 6 (108 rows: 3 models x 3 methods x 12 trials)
├── latency_robustness.csv       Figure 7 (180 rows: 5 latency levels x 3 methods x 12 trials)
├── figure4_trials.csv           Figure 4 (3 full per-step traces)
├── verify_results.py            recomputes all statistics above; run `python verify_results.py`
├── requirements.txt              pandas, scipy
└── code/                         experiment source code (see below)
```

See `summary/`'s column definitions below.

### Column definitions (`primary_experiment.csv` / `ablation.csv` / `cross_model.csv` / `latency_robustness.csv`)

- `method` / `variant`: condition name (HERA, Baseline C, Baseline Fair, HERA Full, HERA-NoCancel, HERA-NoSuppress)
- `fault_leg`: which leg (FL/FR/BL/BR) the simulated fault was injected on
- `trial_timestamp`: identifies the corresponding raw log in `full/` (`{method}_{leg}_{timestamp}.json`)
- `success`: 1 if a valid recovery preset was generated and loaded within the 30 s episode, 0 otherwise
- `response_time_s`: emergency preset response time in seconds, directly measured via `time.time()` timestamps (blank if `success=0`)
- `response_steps`: the same interval in simulation steps, shown for reference only (blank if `success=0`)
- `pre_fault_speed_ms` / `during_fault_speed_ms` / `post_recovery_speed_ms` (primary_experiment.csv only): mean locomotion speed (m/s) in each of the three phases used in Figure 5 — pre-fault (steps 0-500), during-fault (step 500 to recovery), and post-recovery (recovery to trial end)
- `llm_model` (cross_model.csv only) / `added_latency_s` (latency_robustness.csv only): the swept condition
- `llm_calls`: number of LLM calls issued by the emergency-recovery path during the trial

### `figure4_trials.csv`

Full per-simulation-step trace (`method`, `step`, `speed`, `upright`,
`fault_active`, `reconnect_active`) for the one representative trial per
method plotted in Figure 4 (HERA: `ours_interrupt_FL_20260808_012716`;
Baseline C: `baseline_c_BR_20260808_013834`; Baseline Fair:
`baseline_fair_BL_20260808_055220`). Wall-clock time in the figure is
reconstructed from `step` assuming locally uniform simulation throughput, as
described in the Figure 4 caption.

### `summary/code/`

The original experiment scripts:

- `dispatcher.py`: CPG layer + LLM preset request/validation, shared by all methods
- `poc_interrupt.py`: HERA (Ours) — interrupt-driven preemption
- `baseline_c.py`: Baseline C (no-interrupt, cooldown-polled)
- `baseline_fair.py`: Baseline Fair (immediate dispatch, no preemption)

### Reproducing the statistics

```
cd summary
pip install -r requirements.txt
python verify_results.py
```

## `full/` — complete raw logs

The complete per-simulation-step telemetry (locomotion speed, upright state,
active skill, fault/reconnect flags at every simulation step) for all 360
trials, as a matching pair of `.json` (structured, with a `summary` block plus
a `steps` array) and `.csv` (the same per-step data, tabular) per trial.
Organized identically to the experiment groupings in `summary/`:

```
full/
├── table2_main_comparison/{hera,baseline_c,baseline_fair}/
├── table3_ablation/{hera_full,hera_nocancel,hera_nosuppress}/
├── cross_model/{qwen3.5_9b,qwen3.5_4b,llama3.2_3b}/{hera,baseline_c,baseline_fair}/
└── latency_robustness/{lat_0s,lat_1s,lat_3s,lat_5s,lat_10s}/{hera,baseline_c,baseline_fair}/
```

Field names differ slightly by method because each script's summary was
written independently (e.g., HERA/Baseline Fair use `total_recovery_sec`;
Baseline C uses `fault_duration_sec` and `fault_cleared_step`; a `None`/absent
value indicates the trial did not recover within the 30 s episode, i.e., a
failed trial).

## Environment

- OS: Windows 11 Pro
- Python 3.12.0
- dm_control 1.0.37, MuJoCo 3.5.0
- LLM backend: Ollama 0.32.5, models Qwen3.5 9B / Qwen3.5 4B / LLaMA 3.2 3B
- CPU: Intel Core i9-13900K; GPU: NVIDIA RTX A6000; RAM: 56 GB

## Notes

- All trials use a fixed 30 s wall-clock episode budget and a simulated
  single-leg actuator fault injected at simulation step 500 (Sections 4.1-4.2
  of the manuscript).
- Every timestamp range used to select trials for this dataset was verified
  by recomputing the mean/SD/success rate from the raw files and confirming
  it reproduces the exact number printed in the manuscript.
- Superseded/invalidated experiment runs from earlier stages of this study
  (see the manuscript's Limitations section for the implementation issues
  that necessitated re-running the experiments) are intentionally **not**
  included; every trial in this dataset is from the final corrected
  implementation.
