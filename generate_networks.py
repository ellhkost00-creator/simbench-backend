import json
from datetime import date
from pathlib import Path

import simbench as sb
from pandapower.plotting.plotly import simple_plotly

OUT_DIR = Path("data")
PLOTS_DIR = OUT_DIR / "plots"
OUT_FILE = OUT_DIR / "networks.json"

# ── Design tokens (match your sign-in UI) ─────────────────────────────────────
COLORS = {
    "bg_page":        "#f0f4f8",
    "bg_card":        "#ffffff",
    "bg_plot":        "#f8fafc",
    "border":         "#dce3ec",
    "primary":        "#1a56db",
    "primary_light":  "#e6f0fd",
    "text_strong":    "#0f1724",
    "text_muted":     "#64748b",
    "text_hint":      "#94a3b8",

    # Network element colors
    "line":           "#3b82f6",   # vivid blue lines
    "line_width":     2.2,
    "bus":            "#1a56db",   # brand blue nodes
    "bus_size":       8,
    "bus_border":     "#ffffff",
    "trafo":          "#f59e0b",   # amber transformers
    "trafo_size":     13,
    "load":           "#94a3b8",   # muted gray loads
    "load_size":      6,
    "ext_grid":       "#10b981",   # green external grid
    "ext_grid_size":  16,
    "legend_bg":      "rgba(255,255,255,0.96)",
    "legend_border":  "#dce3ec",
}


def safe_filename(code: str) -> str:
    return code.replace("/", "_").replace("\\", "_") + ".html"


def compute_min_height(fig, min_px: int = 500, max_px: int = 3000) -> int:
    """
    Derive a sensible pixel height from the network's geographic bounding box.
    Tall networks get more vertical space; wide ones get less.
    Falls back to min_px if coordinates can't be read.
    """
    try:
        xs, ys = [], []
        for trace in fig.data:
            if hasattr(trace, "x") and trace.x is not None:
                xs.extend([v for v in trace.x if v is not None])
            if hasattr(trace, "y") and trace.y is not None:
                ys.extend([v for v in trace.y if v is not None])

        if not xs or not ys:
            return min_px

        x_range = max(xs) - min(xs) or 1
        y_range = max(ys) - min(ys) or 1
        aspect  = y_range / x_range          # > 1 means taller than wide

        # Base the height on a comfortable 900 px wide viewport assumption
        computed = int(900 * aspect)
        return max(min_px, min(computed, max_px))

    except Exception:
        return min_px


def build_plot_html(fig, code: str, min_height: int = 500) -> str:
    """Wrap the Plotly figure in a branded, fully responsive HTML shell."""

    plot_div = fig.to_html(
        full_html=False,
        include_plotlyjs="cdn",
        config={
            "responsive":             True,
            "displayModeBar":         True,
            "scrollZoom":             True,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            "displaylogo":            False,
        },
    )

    c = COLORS
    today = date.today()

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>SimBench {code}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    html, body {{
      width: 100%; height: 100%;
      overflow: hidden; margin: 0; padding: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: transparent;
      color: {c['text_strong']};
      position: relative;
    }}

    #plot-wrapper {{
      position: absolute;
      inset: 0;
      padding: 0;
    }}

    #plot-card {{
      position: absolute;
      inset: 0;
      background: {c['bg_plot']};
      overflow: hidden;
    }}

    #plot-card > div,
    #plot-card > div > div {{
      width:  100% !important;
      height: 100% !important;
    }}

    /* ── Modebar ── */
    .modebar-container {{
      top: 12px !important;
      right: 12px !important;
    }}
    .modebar-group {{
      background: {c['bg_card']} !important;
      border: 1px solid {c['border']} !important;
      border-radius: 8px !important;
      padding: 2px 4px !important;
      box-shadow: 0 1px 8px rgba(15,23,36,.10) !important;
      margin-left: 4px !important;
    }}
    .modebar-btn {{
      padding: 4px 5px !important;
      border-radius: 5px !important;
      transition: background 0.15s !important;
    }}
    .modebar-btn:hover {{
      background: {c['primary_light']} !important;
    }}
    .modebar-btn path,
    .modebar-btn rect,
    .modebar-btn polygon,
    .modebar-btn line {{
      fill: {c['text_muted']} !important;
      stroke: none !important;
    }}
    .modebar-btn:hover path,
    .modebar-btn:hover rect,
    .modebar-btn:hover polygon {{
      fill: {c['primary']} !important;
    }}
    .modebar-btn[data-attr].active path,
    .modebar-btn.active path {{
      fill: {c['primary']} !important;
    }}
  </style>
</head>
<body>

<div id="plot-wrapper">
  <div id="plot-card">
    {plot_div}
  </div>
</div>

<script>
  function resizePlot() {{
    const card = document.getElementById("plot-card");
    const gd   = document.querySelector(".plotly-graph-div");
    if (!card || !gd || !window.Plotly) return;
    window.Plotly.relayout(gd, {{
      width:  card.offsetWidth,
      height: card.offsetHeight,
    }});
  }}

  // Fire after Plotly has finished its first render
  window.addEventListener("load", () => {{
    resizePlot();
    setTimeout(resizePlot, 150);
  }});

  // React to iframe being resized by the host UI
  window.addEventListener("resize", resizePlot);

  // ResizeObserver: catches any CSS-driven size changes on the card itself
  const ro = new ResizeObserver(resizePlot);
  ro.observe(document.getElementById("plot-card"));
</script>

</body>
</html>"""
    return html


def style_traces(fig):
    """Re-colour and refine every trace for a professional look."""
    c = COLORS

    for trace in fig.data:
        name = (trace.name or "").lower()

        if trace.type == "scattermapbox":
            continue

        is_line_trace = (
            trace.type == "scatter"
            and getattr(trace, "mode", "") in ("lines", "lines+markers")
            and any(k in name for k in ("line", "branch", "cable", "switch"))
        )

        is_trafo_line = (
            trace.type == "scatter"
            and getattr(trace, "mode", "") in ("lines", "lines+markers")
            and any(k in name for k in ("trafo", "transformer", "2w", "3w"))
        )

        # ── Lines / edges ──────────────────────────────────────────────────────
        if is_trafo_line:
            trace.line.color = c["trafo"]
            trace.line.width = 3.5
            trace.opacity = 1.0

            # Use text if Plotly already has element names
            trace.hovertemplate = (
                "<b>Transformer %{text}</b><extra></extra>"
                if getattr(trace, "text", None) is not None
                else "<b>Transformer</b><extra></extra>"
            )

        elif is_line_trace:
            trace.line.color = c["line"]
            trace.line.width = c["line_width"]
            trace.opacity    = 0.75
            if getattr(trace, "hovertext", None) is not None:
                trace.hovertemplate = "<b>%{hovertext}</b><extra></extra>"
                # Do not override hovertemplate.
                # simple_plotly already contains the correct line hover info.
        # ── Bus nodes ──────────────────────────────────────────────────────────
        elif trace.type == "scatter" and "bus" in name:
            if hasattr(trace, "marker"):
                trace.marker.update(
                    color=c["bus"],
                    size=c["bus_size"],
                    symbol="circle",
                    line=dict(color=c["bus_border"], width=2),
                    opacity=1.0,
                )
                trace.hovertemplate = "<b>Bus %{text}</b><extra></extra>"

        # ── Transformer markers ────────────────────────────────────────────────
        elif trace.type == "scatter" and any(k in name for k in ("trafo", "transformer")):
            if hasattr(trace, "marker"):
                trace.marker.update(
                    color=c["trafo"],
                    size=c["trafo_size"],
                    symbol="diamond",
                    line=dict(color="#ffffff", width=2),
                )
                trace.hovertemplate = (
                    "<b>Transformer %{text}</b><extra></extra>"
                    if getattr(trace, "text", None) is not None
                    else "<b>Transformer</b><extra></extra>"
                )

        # ── Load markers ───────────────────────────────────────────────────────
        elif trace.type == "scatter" and "load" in name:
            if hasattr(trace, "marker"):
                trace.marker.update(
                    color=c["load"],
                    size=c["load_size"],
                    symbol="triangle-down",
                    line=dict(color="#ffffff", width=1.5),
                )
                trace.hovertemplate = (
                    "<b>Load %{text}</b><extra></extra>"
                    if getattr(trace, "text", None) is not None
                    else "<b>Load</b><extra></extra>"
                )

        # ── External grid ──────────────────────────────────────────────────────
        elif trace.type == "scatter" and any(k in name for k in ("ext", "grid", "slack")):
            if hasattr(trace, "marker"):
                trace.marker.update(
                    color=c["ext_grid"],
                    size=c["ext_grid_size"],
                    symbol="hexagon",
                    line=dict(color="#ffffff", width=2),
                )
                trace.hovertemplate = (
                    "<b>External grid %{text}</b><extra></extra>"
                    if getattr(trace, "text", None) is not None
                    else "<b>External grid</b><extra></extra>"
                )

    return fig

def main():
    OUT_DIR.mkdir(exist_ok=True)
    PLOTS_DIR.mkdir(exist_ok=True)

    all_codes = sb.collect_all_simbench_codes()

    pure_lv_codes = [
        code for code in all_codes
        if "-LV-" in code and not any(x in code for x in ["MV", "HV", "EHV"])
    ]

    networks = []

    for code in pure_lv_codes:
        print(f"Loading {code}...")
        net = sb.get_simbench_net(code)

        plot_filename = safe_filename(code)
        plot_path     = PLOTS_DIR / plot_filename

        print(f"Creating interactive plot for {code}...")
        try:
            fig = simple_plotly(
                net,
                auto_open=False,
                showlegend=True,
                respect_switches=True,
            )

            # Re-style traces to match UI palette
            fig = style_traces(fig)

            # Compute a sensible height from the network's aspect ratio
            min_height = compute_min_height(fig)

            # Layout: polished, light, professional
            fig.update_layout(
                autosize=True,
                height=None,
                margin=dict(l=16, r=16, t=16, b=64),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor=COLORS["bg_plot"],
                font=dict(
                    family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
                    color=COLORS["text_muted"],
                    size=12,
                ),
                xaxis=dict(
                    showgrid=False, zeroline=False,
                    showticklabels=False, showline=False,
                    fixedrange=False,
                ),
                yaxis=dict(
                    showgrid=False, zeroline=False,
                    showticklabels=False, showline=False,
                    fixedrange=False,
                ),
                hoverlabel=dict(
                    bgcolor="#ffffff",
                    bordercolor=COLORS["border"],
                    font=dict(
                        size=12,
                        color=COLORS["text_strong"],
                        family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
                    ),
                ),
                legend=dict(
                    bgcolor="rgba(255,255,255,0.97)",
                    bordercolor=COLORS["border"],
                    borderwidth=1,
                    font=dict(
                        size=11,
                        color=COLORS["text_strong"],
                        family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
                    ),
                    orientation="h",
                    x=0.5,
                    y=-0.04,
                    xanchor="center",
                    yanchor="top",
                    itemsizing="constant",
                    itemclick="toggleothers",
                    itemdoubleclick="toggle",
                    tracegroupgap=0,
                ),
            )

            html_content = build_plot_html(fig, code, min_height)
            plot_path.write_text(html_content, encoding="utf-8")
            plot_url = f"/plots/{plot_filename}"

        except Exception as e:
            print(f"Plot failed for {code}: {e}")
            plot_url = None

        networks.append({
            "id":           code,
            "name":         f"SimBench {code}",
            "voltage":      "0.4 kV",
            "type":         "LV",
            "status":       "validated",
            "created":      str(date.today()),
            "version":      "v1.0",
            "buses":        int(len(net.bus)),
            "lines":        int(len(net.line)),
            "transformers": int(len(net.trafo)),
            "loads":        int(len(net.load)),
            "plot_url":     plot_url,
        })

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(networks, f, indent=2, ensure_ascii=False)

    print(f"Saved {len(networks)} networks to {OUT_FILE}")
    print(f"Plots saved to {PLOTS_DIR}")


if __name__ == "__main__":
    main()