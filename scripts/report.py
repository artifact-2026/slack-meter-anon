#!/usr/bin/env python3
"""
Slack Meter Report Generator
=============================
Reads a results JSON file produced by orchestrate.py and writes a
self-contained HTML report with embedded base64 plots.

Usage
-----
    python3 scripts/report.py results/experiment.json \
        [--out-dir results/plots] [--report results/report.html]

Dependencies
------------
    pip install matplotlib numpy
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# matplotlib – gracefully degrade if absent
# ---------------------------------------------------------------------------
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.lines as mlines
    import numpy as np
    HAS_MPL = True
except ImportError:
    HAS_MPL = False
    print("[WARN] matplotlib/numpy not installed – plots will be omitted.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Plot helpers – return base64-encoded PNG strings
# ---------------------------------------------------------------------------

def _fig_to_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


def plot_combined(results: list[dict]) -> str | None:
    if not HAS_MPL:
        return None

    sat_data = next((r for r in results if r["type"] == "saturation"), None)
    slack_cpu = next((r for r in results if r["type"] == "slack" and r["resource"] == "cpu"), None)
    slack_io = next((r for r in results if r["type"] == "slack" and r["resource"] == "io"), None)

    panels = []
    if sat_data: panels.append(("sat", sat_data))
    if slack_cpu: panels.append(("slack", slack_cpu))
    if slack_io: panels.append(("slack", slack_io))

    if not panels:
        return None

    fig, axes = plt.subplots(1, len(panels), figsize=(6 * len(panels), 5), squeeze=False)
    axes = axes[0]

    for i, (ptype, data) in enumerate(panels):
        ax = axes[i]
        if ptype == "sat":
            pts  = data["data_points"]
            xs   = [p["n_procs"]    for p in pts]
            ys   = [p["throughput"] for p in pts]
            sat  = data["saturation_procs"]

            ax.plot(xs, ys, marker="o", color="#1976D2", linewidth=2)

            peak_y = data["peak_throughput"]
            ax.annotate(
                f"Saturation Point: {peak_y:.0f} ops/s",
                xy=(sat, peak_y),
                xytext=(sat + 1.5, peak_y),
                arrowprops=dict(arrowstyle="->", color="#D32F2F"),
                fontsize=9,
                va="center"
            )

            ax.set_xlabel("Baseline Process", fontsize=11)
            ax.set_ylabel("Aggregate Throughput (ops/s)", fontsize=11)
            ax.set_title("Inducing Saturation", fontsize=13, fontweight="bold")
            ax.legend()
            ax.grid(True, alpha=0.3)

        elif ptype == "slack":
            resource = data["resource"]
            pts      = data["data_points"]
            
            # Calculate x, y_base, y_slack
            xs = [(p["slack_procs"] - 1) + p["slack_intensity"] for p in pts]
            ys_base = [p["baseline_tput"] for p in pts]
            ys_slack = [p.get("slack_tput", 0.0) for p in pts]
            dropped = [p["dropped"] for p in pts]

            # Sort by x
            sorted_data = sorted(zip(xs, ys_base, ys_slack, dropped), key=lambda x: x[0])
            s_xs = np.array([pt[0] for pt in sorted_data])
            s_ys_base = np.array([pt[1] for pt in sorted_data])
            s_ys_slack = np.array([pt[2] for pt in sorted_data])
            s_dropped = np.array([pt[3] for pt in sorted_data])

            # Find the max safe slack boundary
            safe_xs = [x for x, drop in zip(s_xs, s_dropped) if not drop]
            max_safe_x = max(safe_xs) if safe_xs else s_xs[0]

            # Area below the blue line (Baseline)
            ax.fill_between(s_xs, 0, s_ys_base, color="#BBDEFB", alpha=0.8)

            # Area above the blue line (Slack)
            mask = s_xs <= max_safe_x
            ax.fill_between(s_xs[mask], s_ys_base[mask], (s_ys_base + s_ys_slack)[mask], 
                            color="#A5D6A7", alpha=0.9)

            # Draw strong borders for the areas
            ax.plot(s_xs, s_ys_base, color="#1565C0", linewidth=2, label='Baseline Throughput')
            ax.plot(s_xs, s_ys_base + s_ys_slack, color="saddlebrown", linewidth=2, label='Total (Baseline + Sweeping) Throughput')

            # Scatter points on both the Blue line and the Top (Brown) line
            scatter_colors = ["#D32F2F" if d else "#388E3C" for d in s_dropped]
            ax.scatter(s_xs, s_ys_base, color=scatter_colors, s=40, zorder=4)
            ax.scatter(s_xs, s_ys_base + s_ys_slack, color=scatter_colors, s=40, zorder=4)
            
            ax.set_xlabel("Sweeping Process", fontsize=10)
            ax.set_ylabel("Throughput (ops/s)", fontsize=10)
            
            # Limit y-axis bottom to "skip" the mid range and focus on the lines
            y_min = min(s_ys_base)
            if y_min > 0:
                ax.set_ylim(bottom=y_min * 0.85)
            
            title_str = "Sweeping CPU Slack" if resource == "cpu" else "Sweeping I/O Slack"
            ax.set_title(title_str, fontsize=13, fontweight="bold")
            ax.grid(True, alpha=0.3)

            # Rebuild legend
            handles, labels = ax.get_legend_handles_labels()
            handles.append(mlines.Line2D([], [], marker="o", linestyle="None", color="#388E3C", markersize=8))
            labels.append("Stable (within tolerance)")
            handles.append(mlines.Line2D([], [], marker="o", linestyle="None", color="#D32F2F", markersize=8))
            labels.append("Drop exceeds threshold")
            handles.append(mlines.Line2D([], [], color="#A5D6A7", marker="s", linestyle="None", markersize=10))
            labels.append("Slack")
            ax.legend(handles, labels, loc="center right", bbox_to_anchor=(0.85, 0.45), fontsize=9)

    fig.tight_layout()
    return _fig_to_b64(fig)


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

_CSS = """
  body  { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
          margin:0; padding:2rem; background:#F5F7FA; color:#222; }
  .wrap { max-width:1400px; margin:0 auto; }
  h1    { color:#0D47A1; margin-bottom:.25rem; }
  .sub  { color:#666; margin-top:0; font-size:.95rem; }
  h2    { color:#1565C0; border-bottom:2px solid #BBDEFB; padding-bottom:.4rem; }
  .card { background:#fff; border-radius:10px; padding:1.5rem 2rem;
          margin-bottom:2rem; box-shadow:0 2px 8px rgba(0,0,0,.08); }
  .chip { display:inline-block; background:#E3F2FD; border-radius:20px;
          padding:.35rem .9rem; margin:.25rem .15rem; font-size:1rem; }
  .chip strong { color:#0D47A1; }
  .verdict { display:inline-block; border-radius:8px; padding:.5rem 1.2rem;
             font-size:1.1rem; font-weight:600; margin-top:.6rem; }
  .cpu-verdict { background:#FFF3E0; color:#BF360C; border:1px solid #FFCCBC; }
  .io-verdict  { background:#E8F5E9; color:#1B5E20; border:1px solid #C8E6C9; }
  img   { max-width:100%; border-radius:6px; margin-top:1rem; }
  table { border-collapse:collapse; width:100%; margin-top:1rem; }
  th,td { padding:.55rem 1rem; text-align:left; border-bottom:1px solid #EEE; }
  th    { background:#E3F2FD; font-weight:600; }
  tr:hover td { background:#F5F5F5; }
"""

_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Slack Meter Report</title>
  <style>{css}</style>
</head>
<body>
<div class="wrap">
  <h1>Slack Meter Report</h1>
  <p class="sub">Empirical measurement of CPU and I/O slack in a resource-limited system.</p>
  {body}
</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _sat_section(data: dict) -> str:
    rows = "".join(
        f"<tr><td>{p['n_procs']}</td><td>{p['throughput']:.1f}</td></tr>"
        for p in data["data_points"]
    )

    return f"""
<div class="card">
  <h2>Saturation Experiment</h2>
  <p>Baseline workload: <code>io_mix={data['params']['io_mix']}</code>
     &nbsp;·&nbsp; <code>intensity={data['params']['intensity']}</code></p>
  <span class="chip">Saturation point: <strong>{data['saturation_procs']} processes</strong></span>
  <span class="chip">Peak throughput: <strong>{data['peak_throughput']:.1f} ops/s</strong></span>
  <h3 style="margin-top:1.5rem">Data Points</h3>
  <table>
    <tr><th>Processes</th><th>Throughput (ops/s)</th></tr>
    {rows}
  </table>
</div>"""


def _slack_section(data: dict) -> str:
    resource = data["resource"]
    sm       = data["slack_measurement"]

    verdict_class = f"{resource}-verdict"
    interp = (
        f"Throughput was unaffected up to {sm['procs'] - 1} full-intensity "
        f"{resource.upper()}-only process(es). A drop was first detected at "
        f"intensity&nbsp;≈&nbsp;<strong>{sm['intensity']:.3f}</strong> on the "
        f"{sm['procs']}-th process."
    ) if sm["procs"] > 1 else (
        f"The baseline workload first showed interference at intensity&nbsp;≈&nbsp;"
        f"<strong>{sm['intensity']:.3f}</strong> of a single {resource.upper()}-only process."
    )

    return f"""
<div class="card">
  <h2>{resource.upper()} Slack Measurement</h2>
  <div class="verdict {verdict_class}">
    Slack: ({sm['procs']} proc(s), intensity = {sm['intensity']:.3f})
  </div>
  <p style="margin-top:.8rem">{interp}</p>
</div>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate_report(results: list[dict], report_path: str) -> None:
    sections: list[str] = []

    combined_b64 = plot_combined(results)
    if combined_b64:
        sections.append(f'<div class="card" style="max-width:100%;"><img src="data:image/png;base64,{combined_b64}" alt="Combined Plots" style="width:100%;"></div>')

    sat_data     = next((r for r in results if r["type"] == "saturation"), None)
    slack_items  = [r for r in results if r["type"] == "slack"]

    if sat_data:
        sections.append(_sat_section(sat_data))
    for s in slack_items:
        sections.append(_slack_section(s))

    if not sections:
        sections.append('<div class="card"><p>No experiment data found.</p></div>')

    html = _HTML.format(css=_CSS, body="\n".join(sections))
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w") as f:
        f.write(html)
    print(f"[report] Written → {report_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Slack Meter Report Generator")
    parser.add_argument("results_json", help="JSON file from orchestrate.py")
    parser.add_argument("--report", default="results/report.html")
    args = parser.parse_args()

    with open(args.results_json) as f:
        results = json.load(f)

    generate_report(results, args.report)


if __name__ == "__main__":
    main()
