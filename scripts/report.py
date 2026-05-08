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

            ax.plot(xs, ys, marker="o", color="#1976D2", linewidth=2, label="Aggregate throughput")
            ax.axvline(sat, color="#D32F2F", linestyle="--", linewidth=1.5,
                       label=f"Saturation point  n={sat}")

            peak_y = data["peak_throughput"]
            ax.annotate(
                f"Peak: {peak_y:.0f} ops/s",
                xy=(sat, peak_y),
                xytext=(sat + 0.5, peak_y * 0.92),
                arrowprops=dict(arrowstyle="->", color="#D32F2F"),
                fontsize=9,
            )

            ax.set_xlabel("Number of Baseline Processes", fontsize=11)
            ax.set_ylabel("Aggregate Throughput (ops/s)", fontsize=11)
            ax.set_title("Saturation", fontsize=13, fontweight="bold")
            ax.legend()
            ax.grid(True, alpha=0.3)

        elif ptype == "slack":
            resource = data["resource"]
            pts      = data["data_points"]
            
            # Calculate x, y, and color
            xs = [(p["slack_procs"] - 1) + p["slack_intensity"] for p in pts]
            ys = [((p["baseline_tput"] - p["ref_tput"]) / max(p["ref_tput"], 1.0)) * 100.0 for p in pts]
            colors  = ["#D32F2F" if p["dropped"] else "#388E3C" for p in pts]

            # Sort by x for line graph drawing
            sorted_data = sorted(zip(xs, ys, colors), key=lambda x: x[0])
            s_xs = [pt[0] for pt in sorted_data]
            s_ys = [pt[1] for pt in sorted_data]
            s_colors = [pt[2] for pt in sorted_data]

            # Line graph with shaded area and dotted baseline
            ax.plot(s_xs, s_ys, color="#757575", linewidth=1.5, zorder=2)
            ax.fill_between(s_xs, s_ys, 0, color="#757575", alpha=0.15, zorder=1)
            ax.axhline(0, color="#424242", linestyle=":", linewidth=1.5, zorder=1)

            # Scatter colored points on top
            ax.scatter(s_xs, s_ys, c=s_colors, s=60, zorder=3)
            
            ax.set_xlabel("Sweeping Worker Procs", fontsize=10)
            ax.set_ylabel("% Change from Baseline Throughput", fontsize=10)
            
            title_str = "CPU Slack" if resource == "cpu" else "I/O Slack"
            ax.set_title(title_str, fontsize=13, fontweight="bold")
            ax.grid(True, alpha=0.3)

            base_tput_text = f"Baseline ≈ {pts[0]['ref_tput']:.1f} ops/s" if pts else "Baseline Throughput"
            legend_els = [
                mlines.Line2D([], [], marker="o", linestyle="None",
                              color="#388E3C", markersize=8, label="Stable throughput"),
                mlines.Line2D([], [], marker="o", linestyle="None",
                              color="#D32F2F", markersize=8, label="Drop detected"),
                mlines.Line2D([], [], marker="", linestyle="None",
                              label=base_tput_text)
            ]
            ax.legend(handles=legend_els, fontsize=9)

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
