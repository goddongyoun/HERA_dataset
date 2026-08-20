"""Recompute the manuscript's Table 2 / Table 3 statistics directly from the
trial-level CSVs in this dataset, including the significance tests reported
in the paper (Welch's t-test on response time, Fisher's exact test on
success rate). Run: python verify_results.py

Requires: pandas, scipy (see requirements.txt)
"""
import pandas as pd
from scipy import stats


def summarize(df, group_col, label):
    print(f"\n=== {label} ===")
    for name, g in df.groupby(group_col):
        succ = g[g["success"] == 1]
        n, k = len(g), len(succ)
        line = f"{name:16s} {k}/{n} ({100*k/n:.1f}%)"
        if k > 0:
            line += (f"  response_time={succ['response_time_s'].mean():.3f}"
                     f"+/-{succ['response_time_s'].std():.3f} s"
                     f"  response_steps={succ['response_steps'].mean():.0f}"
                     f"+/-{succ['response_steps'].std():.0f}")
        if "post_recovery_speed_ms" in g.columns and succ["post_recovery_speed_ms"].notna().any():
            pr = succ["post_recovery_speed_ms"].dropna()
            line += f"  post_rec_speed={pr.mean():.3f}+/-{pr.std():.3f} m/s"
        print(line)


def significance_vs_hera(df, method_col, hera_label, other_label):
    hera = df[df[method_col] == hera_label]
    other = df[df[method_col] == other_label]
    hera_t = hera[hera["success"] == 1]["response_time_s"]
    other_t = other[other["success"] == 1]["response_time_s"]
    t, p = stats.ttest_ind(hera_t, other_t, equal_var=False)  # Welch's t-test
    d = (hera_t.mean() - other_t.mean()) / (((hera_t.std()**2 + other_t.std()**2) / 2) ** 0.5)  # Cohen's d
    table = [[len(hera[hera["success"] == 1]), len(hera[hera["success"] == 0])],
             [len(other[other["success"] == 1]), len(other[other["success"] == 0])]]
    _, p_fisher = stats.fisher_exact(table)
    print(f"HERA vs {other_label}: Welch's t={t:.2f}, p={p:.2e}, Cohen's d={d:.2f}"
          f"  |  Fisher's exact (success rate) p={p_fisher:.3f}")


def figure5_phases(df):
    print("\n=== Figure 5: locomotion speed by phase (same batch as Table 2) ===")
    for name, g in df.groupby("method"):
        line = f"{name:16s}"
        for col in ["pre_fault_speed_ms", "during_fault_speed_ms", "post_recovery_speed_ms"]:
            vals = g[col].dropna()
            if len(vals) > 1:
                line += f"  {col}={vals.mean():.3f}+/-{vals.std():.3f}"
        print(line)


if __name__ == "__main__":
    primary = pd.read_csv("primary_experiment.csv")
    summarize(primary, "method", "Table 2: Primary comparison")
    significance_vs_hera(primary, "method", "HERA", "Baseline C")
    significance_vs_hera(primary, "method", "HERA", "Baseline Fair")
    figure5_phases(primary)

    ablation = pd.read_csv("ablation.csv")
    summarize(ablation, "method", "Table 3: Ablation study")

    cross_model = pd.read_csv("cross_model.csv")
    for model, g in cross_model.groupby("llm_model"):
        summarize(g, "method", f"Cross-Model: {model}")

    latency = pd.read_csv("latency_robustness.csv")
    for lat, g in latency.groupby("added_latency_s"):
        summarize(g, "method", f"Latency: +{lat}s")

    print("\nCompare the numbers above against Table 2, Table 3, Section 4.6, "
          "and Section 4.7 of the manuscript.")
