"""Render a static conceptual architecture diagram, not experimental results.

Only the requested PNG/SVG figure files are generated. No experiment source,
campaign artifact, live server, PDF, or publication endpoint is touched.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch


LOCAL = "#175A49"
ASYNC = "#285886"
INK = "#192D3A"
MUTED = "#4C5D67"


def box(ax, x, y, title, body, *, edge, width=1.62, height=1.05):
    patch = FancyBboxPatch(
        (x - width / 2, y - height / 2), width, height,
        boxstyle="round,pad=0.025,rounding_size=0.07",
        linewidth=1.15, edgecolor=edge, facecolor="white", zorder=3,
    )
    ax.add_patch(patch)
    ax.text(x, y + 0.30, title, ha="center", va="center", fontsize=10.2,
            fontweight="bold", color=edge, zorder=4)
    ax.text(x, y - 0.085, body, ha="center", va="center", fontsize=9.1,
            color=INK, linespacing=1.45, zorder=4)


def arrow(ax, points, *, color, width=1.3):
    """Orthogonal route with an arrowhead only on the final segment."""
    for start, end in zip(points[:-2], points[1:-1]):
        ax.plot([start[0], end[0]], [start[1], end[1]], color=color,
                linewidth=width, solid_capstyle="round", zorder=2)
    ax.add_patch(FancyArrowPatch(
        points[-2], points[-1], arrowstyle="-|>", mutation_scale=10,
        linewidth=width, color=color, shrinkA=0, shrinkB=0, zorder=2,
    ))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path(__file__).resolve().parent / "generated" / "architecture",
    )
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 9.1,
        "svg.fonttype": "none", "svg.hashsalt": "hera-v3-conceptual-architecture",
    })
    fig, ax = plt.subplots(figsize=(8.0, 5.8))
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
    ax.set_xlim(0, 8)
    ax.set_ylim(0, 5.8)
    ax.axis("off")
    fig.patch.set_facecolor("white")

    ax.text(0.25, 5.57, "Independent local response and bounded supervision",
            color=INK, fontsize=12.7, fontweight="bold", va="center")
    ax.text(0.25, 5.31, "Conceptual architecture - arrows and positions do not encode measured timing",
            color=MUTED, fontsize=9.0, va="center")

    for origin, height, face, edge in (
        ((0.16, 3.14), 1.96, "#F0F7F3", "#BBD2C6"),
        ((0.16, 0.83), 1.91, "#F1F5FA", "#C3D3E2"),
    ):
        ax.add_patch(FancyBboxPatch(
            origin, 7.68, height, boxstyle="round,pad=0.012,rounding_size=0.08",
            facecolor=face, edgecolor=edge, linewidth=0.8, zorder=0,
        ))

    ax.text(0.32, 4.90, "LOCAL CONTROL · 50 Hz control interval (20 ms)",
            color=LOCAL, fontsize=10.0, fontweight="bold", va="center")
    ax.text(2.00, 2.52, "ASYNCHRONOUS REQUEST PATH", color=ASYNC,
            fontsize=10.0, fontweight="bold", va="center")

    centers = (1.13, 3.04, 4.95, 6.86)
    local_y, request_y = 3.97, 1.65
    local_nodes = (
        ("Measurements", "After env.step\nphysics.forward()\nSynchronized state"),
        ("Residual detector", "Measured-state input\nFixed, debounced score\nNo fault-label input"),
        ("Local action", "Immediate support\nCommand selector\nFallback retained"),
        ("MuJoCo plant", "Applied actuator array\nPersistent plant fault\n5 ms internal steps"),
    )
    request_nodes = (
        ("Scheduler", "Same-lane preempt\nSingle-lane FIFO\nTwo-slot serving"),
        ("LLM service", "Predetermined array\nCanonical serialization\nNo action discovery"),
        ("Validation", "Schema + semantics\nRequest / generation\nidentity checks"),
        ("Mailbox", "Accepted command\nNonblocking transfer\nNo direct actuation"),
    )
    for x, (title, body) in zip(centers, local_nodes):
        box(ax, x, local_y, title, body, edge=LOCAL)
    for x, (title, body) in zip(centers, request_nodes):
        box(ax, x, request_y, title, body, edge=ASYNC)
    for left, right in zip(centers[:-1], centers[1:]):
        arrow(ax, [(left + 0.84, local_y), (right - 0.84, local_y)], color=LOCAL)
        arrow(ax, [(left + 0.84, request_y), (right - 0.84, request_y)], color=ASYNC)

    # Plant feedback is post-integration state, not an instantaneous timing claim.
    arrow(ax, [(centers[-1], 4.52), (centers[-1], 4.68),
               (centers[0], 4.68), (centers[0], 4.52)], color=LOCAL, width=1.0)
    ax.text(4.0, 4.69, "post-integration plant state", ha="center", va="bottom",
            fontsize=8.4, color=LOCAL, bbox={"facecolor": "#F0F7F3", "edgecolor": "none", "pad": 1})

    # Measured events are delivered without placing inference on the local path.
    arrow(ax, [(centers[1], 3.42), (centers[1], 2.96),
               (centers[0], 2.96), (centers[0], 2.20)], color=ASYNC)
    ax.text(0.38, 3.035, "Measured event + original timestamp", color=ASYNC,
            fontsize=8.6, va="center")

    # Mailbox publication does not directly actuate the plant: the control tick polls it.
    arrow(ax, [(centers[-1], 2.20), (centers[-1], 2.96),
               (centers[2], 2.96), (centers[2], 3.42)], color=ASYNC)
    ax.text(5.20, 3.035, "Poll on a following control tick", color=ASYNC,
            fontsize=8.6, va="center")

    ax.text(0.38, 3.30, "No wait for inference or cancellation.",
            color=LOCAL, fontsize=8.55, va="center")
    ax.text(5.18, 3.30, "Same array: ≤50 ticks, then fallback*", color=LOCAL,
            fontsize=8.15, va="center")
    ax.text(0.36, 1.00, "LLM output reaffirms the trusted local action; this does not establish added physical benefit.",
            color=ASYNC, fontsize=8.9, va="center")

    ax.text(0.25, 0.55, "* The fixed observation horizon can truncate a late hold; expiry must be observed to be claimed.",
            color=MUTED, fontsize=8.1, va="center")
    ax.text(0.25, 0.31, "50 Hz is a control cadence, not a hard-real-time guarantee. No immediate GPU-slot release is implied.",
            color=MUTED, fontsize=8.1, va="center")

    fig.savefig(output / "hera_architecture.png", dpi=300, facecolor="white",
                metadata={"Software": "Matplotlib; HERA v3 conceptual architecture"})
    fig.savefig(output / "hera_architecture.svg", facecolor="white",
                metadata={"Date": None, "Creator": "HERA v3 architecture figure generator",
                          "Description": "Conceptual architecture, not measured timing or experimental results."})
    plt.close(fig)
    print(output / "hera_architecture.png")
    print(output / "hera_architecture.svg")


if __name__ == "__main__":
    main()
