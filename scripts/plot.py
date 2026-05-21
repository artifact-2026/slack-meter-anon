#!/usr/bin/env python3
"""
Slack Meter – Experiment CSV Plotter
=====================================
Reads results/experiment.csv and produces one 4-panel bar-chart figure per
case group (case1.*, case2.*, …).

Usage
-----
    # Plot all 5 case groups
    python3 scripts/plot.py

    # Plot only case1.*
    python3 scripts/plot.py --row 1

    # Custom CSV / output directory
    python3 scripts/plot.py --csv results/experiment.csv --out-dir results/plots

Panels (per figure)
-------------------
  1. duty_cycle  →  saturation_n
  2. duty_cycle  →  io_slack_added_pct
  3. duty_cycle  →  cpu_slack_added_pct
  4. duty_cycle  →  ram_slack_added_pct

Dependencies
------------
    pip install matplotlib pandas numpy
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IO_MIX_LABELS: dict[float, str] = {
    0.0: "CPU-Only Workload",
    0.3: "CPU-Heavy Workload with Moderate I/O",
    0.5: "Evenly-Balanced CPU-I/O Workload",
    0.7: "I/O-Heavy Workload with Moderate CPU",
    1.0: "I/O-Focused Workload",
}

# Colour palette – one colour per duty_cycle value (sorted descending: 1.0 → 0.25)
DUTY_COLOURS = {
    1.00: "#1565C0",   # deep blue
    0.75: "#2E7D32",   # deep green
    0.50: "#F57F17",   # amber
    0.25: "#6A1B9A",   # purple
}

FIGURE_DPI  = 150
BAR_WIDTH   = 0.35   # narrower than the 1-unit x spacing so bars have clear gaps
BAR_ALPHA   = 0.88


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _duty_label(v: float) -> str:
    return f"{v:.2f}"


def _io_mix_title(io_mix: float) -> str:
    return IO_MIX_LABELS.get(round(io_mix, 1), f"io_mix={io_mix}")


# ---------------------------------------------------------------------------
# Core plot routine – one 3-panel figure for a single case group
# ---------------------------------------------------------------------------

def plot_case(
    df_case: pd.DataFrame,
    case_prefix: str,
    out_dir: Path,
) -> None:
    """Produce and save a 3-panel figure for *df_case* (rows of one case group).

    X-axis: integer positions 0, 1, 2, … with duty_cycle values as tick labels.
    One coloured bar per duty_cycle; no legend (axis labels are self-describing).
    """

    io_mix      = df_case["io_mix"].iloc[0]
    df_sorted   = df_case.sort_values("duty_cycle", ascending=False)
    duty_cycles = df_sorted["duty_cycle"].tolist()
    tick_labels = [_duty_label(dc) for dc in duty_cycles]
    x_pos       = np.arange(len(duty_cycles))        # integer bar centres

    fig, axes = plt.subplots(1, 4, figsize=(17, 4.5))
    fig.suptitle(
        f"{case_prefix}  ·  {_io_mix_title(io_mix)}  (io_mix={io_mix})",
        fontsize=12, fontweight="bold", y=1.02,
    )

    # -----------------------------------------------------------------------
    # Collect per-bar values
    # -----------------------------------------------------------------------
    sat_vals:   list[float] = []
    io_pct:     list[float] = []
    cpu_cores:  list[float] = []
    cpu_pct:    list[float] = []
    ram_pct:    list[float] = []
    colours:    list[str]   = []

    for dc in duty_cycles:
        row = df_sorted[df_sorted["duty_cycle"] == dc].iloc[0]
        colours.append(DUTY_COLOURS.get(dc, "#888888"))
        sat_vals.append(float(row["saturation_n"]))
        io_pct.append(float(row["io_slack_added_pct"]))
        cpu_cores.append(float(row["cpu_slack_cores"]))
        cpu_pct.append(float(row["cpu_slack_added_pct"]))
        ram_pct.append(float(row.get("ram_slack_added_pct", 0.0)))

    # -----------------------------------------------------------------------
    # Shared x-axis helper
    # -----------------------------------------------------------------------
    def _fmt_xax(ax: plt.Axes) -> None:
        ax.set_xticks(x_pos)
        ax.set_xticklabels(tick_labels, fontsize=9)
        ax.set_xlabel("duty_cycle", fontsize=10)
        ax.set_xlim(-0.6, len(duty_cycles) - 0.4)

    # -----------------------------------------------------------------------
    # Panel 1 – saturation_n
    # -----------------------------------------------------------------------
    ax1 = axes[0]
    for xi, (val, col) in enumerate(zip(sat_vals, colours)):
        ax1.bar(xi, val, BAR_WIDTH, color=col, alpha=BAR_ALPHA)
        ax1.text(xi, val + 0.15, f"{val:.0f}",
                 ha="center", va="bottom", fontsize=8, color=col, fontweight="bold")

    _fmt_xax(ax1)
    ax1.set_ylabel("Saturation Processes", fontsize=10)
    ax1.set_title("Saturation Point", fontsize=11, fontweight="bold")
    ax1.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))
    ax1.set_ylim(bottom=0, top=max(sat_vals) * 1.25)
    ax1.grid(axis="y", alpha=0.3)

    # Dynamic y-axis: panels 2 & 3 share the same upper bound so they are
    # directly comparable.  Headroom = 12 % above the tallest bar across both
    # panels; floor at 10 % so near-zero cases still look reasonable.
    def _pct_ymax(vals: list[float]) -> float:
        peak = max(vals) if vals else 0.0
        return max(peak * 1.12, 0.10)

    shared_pct_ymax = max(_pct_ymax(io_pct), _pct_ymax(cpu_pct), _pct_ymax(ram_pct))

    # -----------------------------------------------------------------------
    # Panel 2 – io_slack_added_pct
    # -----------------------------------------------------------------------
    ax2 = axes[1]
    for xi, (val, col) in enumerate(zip(io_pct, colours)):
        ax2.bar(xi, val, BAR_WIDTH, color=col, alpha=BAR_ALPHA)
        ax2.text(xi, val + 0.003, f"{val:.1%}",
                 ha="center", va="bottom", fontsize=7.5, color=col, fontweight="bold")

    _fmt_xax(ax2)
    ax2.set_ylabel("Slack Added (fraction of peak xput)", fontsize=10)
    ax2.set_title("I/O Slack", fontsize=11, fontweight="bold")
    ax2.set_ylim(0.0, shared_pct_ymax)
    ax2.yaxis.set_major_formatter(ticker.PercentFormatter(xmax=1.0))
    ax2.grid(axis="y", alpha=0.3)

    # -----------------------------------------------------------------------
    # Panel 3 – cpu_slack_added_pct only (bars, same scale as panel 2)
    # -----------------------------------------------------------------------
    ax3 = axes[2]
    for xi, (pct, col) in enumerate(zip(cpu_pct, colours)):
        ax3.bar(xi, pct, BAR_WIDTH, color=col, alpha=BAR_ALPHA)
        ax3.text(xi, pct + 0.003, f"{pct:.1%}",
                 ha="center", va="bottom", fontsize=7.5, color=col, fontweight="bold")

    _fmt_xax(ax3)
    ax3.set_ylabel("Slack Added (fraction of peak xput)", fontsize=10)
    ax3.set_title("CPU Slack", fontsize=11, fontweight="bold")
    ax3.set_ylim(0.0, shared_pct_ymax)
    ax3.yaxis.set_major_formatter(ticker.PercentFormatter(xmax=1.0))
    ax3.grid(axis="y", alpha=0.3)

    # -----------------------------------------------------------------------
    # Panel 4 – ram_slack_added_pct
    # -----------------------------------------------------------------------
    ax4 = axes[3]
    for xi, (pct, col) in enumerate(zip(ram_pct, colours)):
        ax4.bar(xi, pct, BAR_WIDTH, color=col, alpha=BAR_ALPHA)
        ax4.text(xi, pct + 0.003, f"{pct:.1%}",
                 ha="center", va="bottom", fontsize=7.5, color=col, fontweight="bold")

    _fmt_xax(ax4)
    ax4.set_ylabel("Slack Added (fraction of peak xput)", fontsize=10)
    ax4.set_title("RAM Slack", fontsize=11, fontweight="bold")
    ax4.set_ylim(0.0, shared_pct_ymax)
    ax4.yaxis.set_major_formatter(ticker.PercentFormatter(xmax=1.0))
    ax4.grid(axis="y", alpha=0.3)

    # -----------------------------------------------------------------------
    # Save
    # -----------------------------------------------------------------------
    fig.tight_layout()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{case_prefix}_slack_summary.png"
    fig.savefig(out_path, dpi=FIGURE_DPI, bbox_inches="tight")
    plt.close(fig)
    print(f"[plot] Saved → {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Slack Meter CSV Plotter")
    parser.add_argument(
        "--csv", default="results/experiment.csv",
        help="Path to the experiment CSV file (default: results/experiment.csv)",
    )
    parser.add_argument(
        "--out-dir", default="results/plots",
        help="Directory to write PNG figures into (default: results/plots)",
    )
    parser.add_argument(
        "--row", type=int, default=None, metavar="N",
        help="Plot only case<N>.* rows (1–5).  Omit to plot all 5 cases.",
    )
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise SystemExit(f"[ERROR] CSV not found: {csv_path}")

    df = pd.read_csv(csv_path)

    # Coerce numeric columns (guards against stray commas like "0.953,,0.4303")
    for col in ["io_mix", "duty_cycle", "saturation_n",
                "cpu_slack_cores", "cpu_slack_added_pct",
                "io_slack_cores", "io_slack_added_pct",
                "ram_slack_cores", "ram_slack_added_pct"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df.dropna(subset=["io_mix", "duty_cycle", "saturation_n"], inplace=True)

    out_dir = Path(args.out_dir)

    # Determine which case groups to plot
    if args.row is not None:
        prefixes = [f"case{args.row}"]
    else:
        # Discover all caseN prefixes present in the data
        prefixes = sorted({
            row.split(".")[0]
            for row in df["case"].astype(str)
            if "." in row
        })

    for prefix in prefixes:
        mask = df["case"].astype(str).str.startswith(prefix + ".")
        df_case = df[mask].copy()
        if df_case.empty:
            print(f"[WARN] No rows found for prefix '{prefix}' — skipping.")
            continue
        plot_case(df_case, prefix, out_dir)

    print(f"\n[done] Figures written to {out_dir}/")


if __name__ == "__main__":
    main()
