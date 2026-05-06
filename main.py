import json
import os
import sys
import shutil
import subprocess
import calendar
from datetime import datetime
from pathlib import Path
from typing import Literal

import pandas as pd
import simbench as sb
import pandapower.timeseries as ts

from contextlib import asynccontextmanager

from db import (
    db_available, get_db_network, get_db_networks,
    get_db_runs, init_db, save_network, save_run, seed_networks_from_file,
)
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Attempt PostgreSQL connection on startup; non-fatal if unavailable.
    init_db()
    # Seed networks table from JSON file if DB is available and table is empty.
    if DATA_FILE.exists():
        seed_networks_from_file(DATA_FILE)
    yield


app = FastAPI(title="SimBench Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_FILE = Path("data/networks.json")
PLOTS_DIR = Path("data/plots")
RESULTS_DIR = Path("data/results")

# Path to the nando Nando_final directory (sibling of simbench-backend in the repo root)
NANDO_ROOT = Path(__file__).parent.parent / "nando" / "Nando_final"

try:
    from generate_networks import COLORS, style_traces, build_plot_html, compute_min_height
    _PLOT_HELPERS_AVAILABLE = True
except Exception:
    _PLOT_HELPERS_AVAILABLE = False

NANDO_NETWORK_NAMES = {
    "1": "Rural_SMR8",
    "2": "Rural_KLO14",
    "3": "Urban_HPK11",
    "4": "Urban_CRE21",
}

# Conversion pipeline steps (relative to NANDO_ROOT).
# Balanced stops after step 3; unbalanced adds the 3-phase preparation step.
_CONVERSION_STEPS_BALANCED = [
    ("conversion/dss_files_creator.py",  "Generate DSS files from Excel"),
    ("conversion/dss_to_pp_mv_build.py", "Build MV pandapower network"),
    ("conversion/dss_to_pp_lv_build.py", "Add LV network (trafos + lines)"),
]
_CONVERSION_STEPS_UNBALANCED = _CONVERSION_STEPS_BALANCED + [
    ("fixes/prepare_net_for_3ph.py", "Prepare 3-phase parameters"),
]

# Violation thresholds — must match the frontend constants in violations-overview.ts.
V_LOWER = 0.94
V_UPPER = 1.06
LOAD_LIMIT = 100.0

if PLOTS_DIR.exists():
    app.mount("/plots", StaticFiles(directory=PLOTS_DIR), name="plots")


class RunRequest(BaseModel):
    horizon: Literal["day", "week", "month"]
    year: int = 2016
    month: int
    day: int | None = None
    mode: Literal["balanced"] = "balanced"


class ConvertOpenDSSRequest(BaseModel):
    network: Literal["1", "2", "3", "4"]
    mode: Literal["balanced", "unbalanced"]


class SimulateOpenDSSRequest(BaseModel):
    network: Literal["1", "2", "3", "4"]
    mode: Literal["balanced", "unbalanced"]
    day: int  # 1–365
    validation_hours: Literal[1, 2, 4, 24] = 2


class SaveOpenDSSRequest(BaseModel):
    network: Literal["1", "2", "3", "4"]
    mode: Literal["balanced", "unbalanced"]
    validation_day: int | None = None
    metrics: dict | None = None


class OpenDSSRunRequest(BaseModel):
    day: int  # 1–365 day of year


def load_networks():
    if not DATA_FILE.exists():
        return []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def network_exists(network_id: str) -> bool:
    return get_db_network(network_id) is not None


def get_day_of_year(year: int, month: int, day: int):
    return sum(calendar.monthrange(year, m)[1] for m in range(1, month)) + day


def validate_month(year: int, month: int):
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="Invalid month")
    return calendar.monthrange(year, month)[1]


def validate_day(year: int, month: int, day: int):
    max_day = validate_month(year, month)
    if day < 1 or day > max_day:
        raise HTTPException(status_code=400, detail="Invalid day for selected month")


def get_time_steps(
    year: int,
    horizon: str,
    month: int,
    day: int | None = None,
    steps_per_day: int = 96,
):
    validate_month(year, month)

    if horizon == "day":
        if day is None:
            raise HTTPException(status_code=400, detail="Day is required for day horizon")
        validate_day(year, month, day)
        day_of_year = get_day_of_year(year, month, day)
        start = (day_of_year - 1) * steps_per_day
        return range(start, start + steps_per_day)

    if horizon == "week":
        if day is None:
            raise HTTPException(status_code=400, detail="Start day is required for week horizon")
        validate_day(year, month, day)
        day_of_year = get_day_of_year(year, month, day)
        days_in_year = 366 if calendar.isleap(year) else 365
        start_day = day_of_year
        end_day = min(day_of_year + 6, days_in_year)
        return range((start_day - 1) * steps_per_day, end_day * steps_per_day)

    if horizon == "month":
        days_before_month = sum(calendar.monthrange(year, m)[1] for m in range(1, month))
        days_in_month = calendar.monthrange(year, month)[1]
        start = days_before_month * steps_per_day
        return range(start, start + days_in_month * steps_per_day)

    raise HTTPException(status_code=400, detail="Invalid horizon")


def get_run_id(request: RunRequest) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if request.horizon == "day":
        return f"{request.year}-{request.month:02d}-{request.day:02d}_{stamp}"
    if request.horizon == "week":
        return f"{request.year}-{request.month:02d}-{request.day:02d}_week_{stamp}"
    if request.horizon == "month":
        return f"{request.year}-{request.month:02d}_{stamp}"
    return f"unknown_{stamp}"


def _run_nando_step(script_rel: str, label: str, env: dict):
    """Run a single nando pipeline script as a subprocess. Raises on non-zero exit."""
    script = NANDO_ROOT / script_rel
    if not script.exists():
        raise RuntimeError(f"Pipeline script not found: {script}")
    # Force UTF-8 I/O so scripts with non-ASCII print statements don't fail on
    # Windows terminals that default to cp1252.
    # MPLBACKEND=Agg prevents matplotlib from opening GUI windows during subprocesses.
    utf8_env = {**env, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "MPLBACKEND": "Agg"}
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(NANDO_ROOT),
        env=utf8_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "")[-3000:]
        raise RuntimeError(f"Step '{label}' failed:\n{tail}")


def _network_stats_from_pp(net_xlsx: Path, net_json: Path, mode: str) -> dict:
    """Load the converted pandapower network and return element counts."""
    import pandapower as pp
    try:
        if mode == "unbalanced" and net_json.exists():
            net = pp.from_json(str(net_json))
        elif net_xlsx.exists():
            net = pp.from_excel(str(net_xlsx))
        else:
            return {}
        return {
            "buses": len(net.bus),
            "lines": len(net.line),
            "transformers": len(net.trafo),
            "loads": len(net.load),
        }
    except Exception:
        return {}


_PLOT_WIDTH = 900  # fixed canvas width for all OpenDSS topology plots


def _generate_opendss_plot(network_id: str, net_xlsx: Path, net_json: Path, mode: str) -> tuple[str | None, int]:
    """
    Generate an interactive Plotly topology plot at its natural size (no stretch-to-fill).
    Returns (serve_url, height_px). Height is 0 when generation fails.
    Skips regeneration if the HTML file already exists.
    """
    if not _PLOT_HELPERS_AVAILABLE:
        return None, 0
    plot_filename = f"{network_id}.html"
    plot_path = PLOTS_DIR / plot_filename
    if plot_path.exists():
        # Read back the stored height from the file comment so we can return it.
        # Falls back to a sensible default if not found.
        try:
            first_line = plot_path.read_text(encoding="utf-8", errors="ignore")[:200]
            import re
            m = re.search(r"data-height=\"(\d+)\"", first_line)
            stored_h = int(m.group(1)) if m else 600
        except Exception:
            stored_h = 600
        return f"/plots/{plot_filename}", stored_h

    try:
        import pandapower as pp
        from pandapower.plotting.plotly import simple_plotly

        if mode == "unbalanced" and net_json.exists():
            net = pp.from_json(str(net_json))
        elif net_xlsx.exists():
            net = pp.from_excel(str(net_xlsx))
        else:
            return None, 0

        fig = simple_plotly(net, auto_open=False, showlegend=True, respect_switches=False)
        fig = style_traces(fig)
        plot_height = compute_min_height(fig)

        # Fixed size — no autosize, no resize-to-fill JS.
        fig.update_layout(
            autosize=False,
            width=_PLOT_WIDTH,
            height=plot_height,
            margin=dict(l=16, r=16, t=16, b=64),
            paper_bgcolor=COLORS["bg_plot"],
            plot_bgcolor=COLORS["bg_plot"],
            font=dict(
                family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
                color=COLORS["text_muted"], size=12,
            ),
            xaxis=dict(showgrid=False, zeroline=False, showticklabels=False, showline=False, fixedrange=False),
            yaxis=dict(showgrid=False, zeroline=False, showticklabels=False, showline=False, fixedrange=False),
            hoverlabel=dict(
                bgcolor="#ffffff", bordercolor=COLORS["border"],
                font=dict(size=12, color=COLORS["text_strong"],
                          family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"),
            ),
            legend=dict(
                bgcolor="rgba(255,255,255,0.97)", bordercolor=COLORS["border"], borderwidth=1,
                font=dict(size=11, color=COLORS["text_strong"],
                          family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"),
                orientation="h", x=0.5, y=-0.04, xanchor="center", yanchor="top",
                itemsizing="constant", itemclick="toggleothers", itemdoubleclick="toggle", tracegroupgap=0,
            ),
        )

        plot_div = fig.to_html(
            full_html=False,
            include_plotlyjs="cdn",
            config={"displayModeBar": True, "scrollZoom": True,
                    "modeBarButtonsToRemove": ["lasso2d", "select2d"], "displaylogo": False},
        )

        html = f"""<!DOCTYPE html>
<html lang="en" data-height="{plot_height}">
<head>
  <meta charset="UTF-8"/>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: {COLORS["bg_plot"]}; overflow: hidden; }}
  </style>
</head>
<body>{plot_div}</body>
</html>"""

        PLOTS_DIR.mkdir(parents=True, exist_ok=True)
        plot_path.write_text(html, encoding="utf-8")
        return f"/plots/{plot_filename}", plot_height
    except Exception as exc:
        logger.warning("Plot generation failed for %s: %s", network_id, exc)
        return None, 0


@app.post("/convert/opendss")
def convert_opendss(request: ConvertOpenDSSRequest):
    """
    Run the OpenDSS → pandapower conversion pipeline for one of the 4 networks.
    Skips the pipeline entirely if the output file already exists (cached).
    Balanced mode: DSS file generation + MV build + LV build.
    Unbalanced mode: same + 3-phase parameter preparation.
    Returns network element counts on success.
    """
    if not NANDO_ROOT.exists():
        raise HTTPException(status_code=500, detail=f"Nando root not found: {NANDO_ROOT}")

    network_name = NANDO_NETWORK_NAMES[request.network]
    net_subdir = f"net_{request.network}_{network_name}"
    net_dir = NANDO_ROOT / "dss_files" / net_subdir
    net_xlsx = net_dir / "net_pp.xlsx"
    net_json = net_dir / "net_pp_3ph_ready.json"

    already_converted = (
        net_json.exists() if request.mode == "unbalanced" else net_xlsx.exists()
    )

    if already_converted:
        stats = _network_stats_from_pp(net_xlsx=net_xlsx, net_json=net_json, mode=request.mode)
        network_id = f"opendss-{request.network}-{request.mode}"
        plot_url, plot_height = _generate_opendss_plot(network_id, net_xlsx, net_json, request.mode)
        return {
            "status": "completed",
            "network": request.network,
            "network_name": network_name,
            "mode": request.mode,
            "duration_seconds": 0.0,
            "network_stats": stats,
            "cached": True,
            "plot_url": plot_url,
            "plot_height": plot_height or None,
        }

    steps = (
        _CONVERSION_STEPS_UNBALANCED
        if request.mode == "unbalanced"
        else _CONVERSION_STEPS_BALANCED
    )

    env = {**os.environ, "NANDO_NETWORK": request.network}
    start_time = datetime.now()

    for script_rel, label in steps:
        try:
            _run_nando_step(script_rel, label, env)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    duration_seconds = (datetime.now() - start_time).total_seconds()

    stats = _network_stats_from_pp(net_xlsx=net_xlsx, net_json=net_json, mode=request.mode)
    network_id = f"opendss-{request.network}-{request.mode}"
    plot_url, plot_height = _generate_opendss_plot(network_id, net_xlsx, net_json, request.mode)

    return {
        "status": "completed",
        "network": request.network,
        "network_name": network_name,
        "mode": request.mode,
        "duration_seconds": round(duration_seconds, 2),
        "network_stats": stats,
        "cached": False,
        "plot_url": plot_url,
        "plot_height": plot_height or None,
    }


_SIMULATE_STEPS_BALANCED = [
    ("conversion/dss_files_creator.py",    "Regenerate load profiles for selected day"),
    ("nando_runs/nando_run_balanced.py",   "OpenDSS balanced timeseries"),
    ("panda_runs/pp_timeseries.py",        "pandapower balanced timeseries"),
    ("metrics/metrics_all_busses.py",      "Compare vm_pu (PP vs DSS)"),
    ("metrics/metrics_all_lines.py",       "Compare line loading (PP vs DSS)"),
    ("metrics/metric_trafo_loading.py",    "Compare trafo loading (PP vs DSS)"),
]

_SIMULATE_STEPS_UNBALANCED = [
    ("conversion/dss_files_creator.py",    "Regenerate load profiles for selected day"),
    ("nando_runs/nando_run_balanced.py",   "OpenDSS balanced timeseries (voltage reference)"),
    ("nando_runs/nando_run_unbalanced.py", "OpenDSS 3-phase timeseries"),
    ("panda_runs/pp_timeseries_3ph.py",   "pandapower 3-phase timeseries"),
    ("metrics/metrics_3ph_vm_pu.py",       "Compare bus voltages (3-phase)"),
    ("metrics/metrics_3ph_loading.py",     "Compare line+trafo loading (3-phase)"),
]


def _parse_metric_global(path: Path) -> dict:
    """Parse metric_global.txt into a dict. Returns {} if file missing or malformed."""
    if not path.exists():
        return {}
    data = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            try:
                data[key.strip()] = float(val.strip())
            except ValueError:
                data[key.strip()] = val.strip()
    return data


def _safe_float(v) -> float | None:
    """Return a float or None for NaN/missing values."""
    try:
        f = float(v)
        return None if (f != f) else f   # NaN check
    except (TypeError, ValueError):
        return None


def _build_validation(metrics_dir: Path, mode: str) -> dict:
    """
    Build a structured validation dict from the per-element CSV files
    generated by the metrics scripts.

    Returns a dict with keys: bus_voltage, line_loading (unbalanced), trafo_loading (unbalanced).
    Any section whose source files are missing is omitted.
    """
    import pandas as pd

    validation: dict = {}

    # ── Bus voltage ────────────────────────────────────────────────────────────
    if mode == "balanced":
        gm = _parse_metric_global(metrics_dir / "metric_global.txt")
        bus_csv = metrics_dir / "metric_per_bus.csv"
        voltage: dict = {
            "matched":      _safe_float(gm.get("Matched buses")),
            "total_points": _safe_float(gm.get("Total points")),
            "mape":         _safe_float(gm.get("Global MAPE %")),
            "max_error":    _safe_float(gm.get("Global Max  %")),
            "bias":         _safe_float(gm.get("Global Bias %")),
            "by_phase":     {},
            "worst_buses":  [],
        }
        if bus_csv.exists():
            df = pd.read_csv(bus_csv)
            if not df.empty:
                voltage["worst_buses"] = (
                    df.head(10)[["bus_name", "MAPE_%", "Max_%", "MeanSigned_%"]]
                    .rename(columns={"MAPE_%": "mape", "Max_%": "max", "MeanSigned_%": "bias"})
                    .to_dict("records")
                )
    else:
        gm = _parse_metric_global(metrics_dir / "metric_3ph_global.txt")
        bus_csv = metrics_dir / "metric_3ph_per_bus.csv"
        voltage = {
            "matched":      _safe_float(gm.get("Matched (bus, phase) pairs")),
            "total_points": _safe_float(gm.get("Total comparison points")),
            "mape":         _safe_float(gm.get("Global MAPE  %")),
            "max_error":    _safe_float(gm.get("Global Max   %")),
            "bias":         _safe_float(gm.get("Global Bias  %")),
            "by_phase":     {},
            "worst_buses":  [],
        }
        if bus_csv.exists():
            df = pd.read_csv(bus_csv)
            if not df.empty:
                for ph, grp in df.groupby("phase"):
                    voltage["by_phase"][str(ph)] = {
                        "matched": int(len(grp)),
                        "mape":  _safe_float(grp["MAPE_%"].mean()),
                        "max":   _safe_float(grp["Max_%"].max()),
                        "bias":  _safe_float(grp["MeanSigned_%"].mean()),
                    }
                voltage["worst_buses"] = (
                    df.head(10)[["bus_name", "phase", "MAPE_%", "Max_%", "MeanSigned_%"]]
                    .rename(columns={"MAPE_%": "mape", "Max_%": "max", "MeanSigned_%": "bias"})
                    .to_dict("records")
                )

    validation["bus_voltage"] = voltage

    # ── Loading ────────────────────────────────────────────────────────────────
    import re as _re

    def _agg_from_df(df: pd.DataFrame, name_col: str, balanced: bool) -> dict:
        """Build the shared loading agg dict from a (possibly filtered) DataFrame."""
        agg: dict = {
            "matched_elements": int(df[name_col].nunique()),
            "mae":    _safe_float(df["mae"].mean()    if not balanced else df["MAE_pp"].mean()),
            "max_ae": _safe_float(df["max_ae"].max()  if not balanced else df["MaxAbs_pp"].max()),
            "mbe":    _safe_float(df["mbe"].mean()    if not balanced else df["Bias_pp"].mean()),
            "by_phase": {},
            "worst":  [],
        }
        if not balanced:
            for ph, grp in df.groupby("phase"):
                agg["by_phase"][str(ph)] = {
                    "matched": int(len(grp)),
                    "mae":    _safe_float(grp["mae"].mean()),
                    "max_ae": _safe_float(grp["max_ae"].max()),
                    "mbe":    _safe_float(grp["mbe"].mean()),
                }
            worst_rows = (
                df.nlargest(10, "max_ae")[["dss_name", "phase", "mae", "max_ae", "mbe"]]
                .to_dict("records")
            )
            agg["worst"] = [
                {k: (_safe_float(v) if k not in ("dss_name", "phase") else v)
                 for k, v in row.items()}
                for row in worst_rows
            ]
        else:
            worst_rows = df.nlargest(10, "MaxAbs_pp")
            agg["worst"] = [
                {
                    "dss_name": str(r[name_col]),
                    "phase":    "—",
                    "mae":      _safe_float(r["MAE_pp"]),
                    "max_ae":   _safe_float(r["MaxAbs_pp"]),
                    "mbe":      _safe_float(r["Bias_pp"]),
                }
                for _, r in worst_rows.iterrows()
            ]
        return agg

    if mode == "unbalanced":
        # Lines — split MV / LV by _lv suffix in element name (same rule as balanced script)
        line_csv = metrics_dir / "metric_3ph_line_loading_per_element.csv"
        if line_csv.exists():
            df = pd.read_csv(line_csv)
            if not df.empty:
                is_lv = df["dss_name"].str.contains(r"_lv", case=False, regex=True)
                for mask, out_key in [(~is_lv, "mv_line_loading"), (is_lv, "lv_line_loading")]:
                    sub = df[mask]
                    if not sub.empty:
                        validation[out_key] = _agg_from_df(sub, "dss_name", balanced=False)

        # Transformers
        trafo_csv = metrics_dir / "metric_3ph_trafo_loading_per_element.csv"
        if trafo_csv.exists():
            df = pd.read_csv(trafo_csv)
            if not df.empty:
                validation["trafo_loading"] = _agg_from_df(df, "dss_name", balanced=False)

    else:
        # Balanced: read Excel outputs from metrics_all_lines.py and metric_trafo_loading.py
        for xlsx_name, out_key in [
            ("mv_line_loading_metrics.xlsx", "mv_line_loading"),
            ("lv_line_loading_metrics.xlsx", "lv_line_loading"),
        ]:
            xlsx_path = metrics_dir / xlsx_name
            if not xlsx_path.exists():
                continue
            try:
                df = pd.read_excel(xlsx_path, sheet_name="summary", engine="openpyxl")
                df = df[~df["line_name"].astype(str).str.startswith("===")]
                if not df.empty:
                    validation[out_key] = _agg_from_df(df, "line_name", balanced=True)
            except Exception:
                pass

        # Transformers
        trafo_xlsx = metrics_dir / "trafo_loading_compare.xlsx"
        if trafo_xlsx.exists():
            try:
                df = pd.read_excel(trafo_xlsx, sheet_name="summary", engine="openpyxl")
                df = df[~df["trafo_name"].astype(str).str.startswith("===")]
                if not df.empty:
                    worst_trafos = df.nlargest(10, "MaxAbs_pp")
                    validation["trafo_loading"] = {
                        "matched_elements": len(df),
                        "mae":    _safe_float(df["MAE_pp"].mean()),
                        "rmse":   _safe_float(df["RMSE_pp"].mean()),
                        "max_ae": _safe_float(df["MaxAbs_pp"].max()),
                        "mbe":    _safe_float(df["Bias_pp"].mean()),
                        "by_phase": {},
                        "worst": [
                            {
                                "dss_name": str(r["trafo_name"]),
                                "phase":    "—",
                                "mae":      _safe_float(r["MAE_pp"]),
                                "rmse":     _safe_float(r["RMSE_pp"]),
                                "max_ae":   _safe_float(r["MaxAbs_pp"]),
                                "mbe":      _safe_float(r["Bias_pp"]),
                            }
                            for _, r in worst_trafos.iterrows()
                        ],
                    }
            except Exception:
                pass

    return validation


@app.post("/convert/opendss/simulate")
def simulate_opendss(request: SimulateOpenDSSRequest):
    """
    Run the OpenDSS and pandapower timeseries simulations for the chosen day,
    then compute comparison metrics (MAPE, max error, bias).
    Balanced: DSS balanced + PP balanced + metrics_all_busses.
    Unbalanced: DSS balanced (voltage ref) + DSS 3-ph + PP 3-ph + 3-ph metrics.
    """
    if not 1 <= request.day <= 365:
        raise HTTPException(status_code=400, detail="day must be between 1 and 365")

    if not NANDO_ROOT.exists():
        raise HTTPException(status_code=500, detail=f"Nando root not found: {NANDO_ROOT}")

    network_name = NANDO_NETWORK_NAMES[request.network]
    steps = (
        _SIMULATE_STEPS_UNBALANCED
        if request.mode == "unbalanced"
        else _SIMULATE_STEPS_BALANCED
    )

    # Compute validation window: each step is 30 min → 2 steps per hour
    n_validation_steps = request.validation_hours * 2
    validation_start_hhmm = "00:00"
    validation_end_h = request.validation_hours
    validation_end_hhmm = f"{validation_end_h:02d}:00" if validation_end_h < 24 else "23:30"

    env = {
        **os.environ,
        "NANDO_NETWORK": request.network,
        "NANDO_SELECTED_DAY": str(request.day),
        "NANDO_VALIDATION_HOURS": str(request.validation_hours),
        "NANDO_VALIDATION_STEPS": str(n_validation_steps),
    }
    start_time = datetime.now()

    for script_rel, label in steps:
        try:
            _run_nando_step(script_rel, label, env)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    duration_seconds = (datetime.now() - start_time).total_seconds()

    net_subdir = f"net_{request.network}_{network_name}"
    metrics_dir = NANDO_ROOT / "metrics" / net_subdir

    if request.mode == "balanced":
        global_metrics = _parse_metric_global(metrics_dir / "metric_global.txt")
    else:
        vm_metrics   = _parse_metric_global(metrics_dir / "metric_3ph_global.txt")
        load_metrics = _parse_metric_global(metrics_dir / "metric_3ph_loading_global.txt")
        global_metrics = {**vm_metrics, **{f"loading_{k}": v for k, v in load_metrics.items()}}

    validation = _build_validation(metrics_dir, request.mode)

    return {
        "status": "completed",
        "network": request.network,
        "network_name": network_name,
        "mode": request.mode,
        "day": request.day,
        "duration_seconds": round(duration_seconds, 2),
        "metrics": global_metrics,
        "validation": validation,
        "validation_hours": request.validation_hours,
        "n_timesteps": n_validation_steps,
        "validation_start": validation_start_hhmm,
        "validation_end": validation_end_hhmm,
    }


NANDO_NETWORK_TYPES = {"1": "Rural", "2": "Rural", "3": "Urban", "4": "Urban"}


def _upsert_networks_json(record: dict) -> None:
    """Add or replace a network entry in data/networks.json (filesystem fallback)."""
    networks: list = []
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            networks = json.load(f)
    networks = [n for n in networks if n.get("id") != record["id"]]
    networks.append(record)
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(networks, f, indent=2, ensure_ascii=False)


@app.post("/convert/opendss/save")
def save_opendss_network(request: SaveOpenDSSRequest):
    """
    Persist a converted OpenDSS network into the database and networks.json
    so it appears in /networks and can be used in future simulations.
    Idempotent — re-saving the same network_id updates the existing record.
    """
    network_name = NANDO_NETWORK_NAMES[request.network]
    network_id = f"opendss-{request.network}-{request.mode}"

    net_subdir = f"net_{request.network}_{network_name}"
    net_dir = NANDO_ROOT / "dss_files" / net_subdir
    stats = _network_stats_from_pp(
        net_xlsx=net_dir / "net_pp.xlsx",
        net_json=net_dir / "net_pp_3ph_ready.json",
        mode=request.mode,
    )
    if not stats:
        raise HTTPException(
            status_code=400,
            detail="Converted network files not found. Run the conversion first.",
        )

    status = "validated" if request.validation_day is not None else "converted"
    plot_filename = f"{network_id}.html"
    plot_url = f"/plots/{plot_filename}" if (PLOTS_DIR / plot_filename).exists() else None
    record = {
        "id":           network_id,
        "name":         f"OpenDSS {network_name.replace('_', ' ')} – {request.mode.capitalize()}",
        "voltage":      "66 kV / 22 kV / 0.4 kV",
        "type":         NANDO_NETWORK_TYPES[request.network],
        "status":       status,
        "created":      datetime.now().strftime("%Y-%m-%d"),
        "version":      "v1.0",
        "buses":        stats.get("buses"),
        "lines":        stats.get("lines"),
        "transformers": stats.get("transformers"),
        "loads":        stats.get("loads"),
        "plot_url":     plot_url,
        # stored in extra column
        "source":           "opendss",
        "mode":             request.mode,
        "validation_day":   request.validation_day,
        "metrics":          request.metrics,
    }

    save_network(record)
    _upsert_networks_json(record)

    return {"status": "saved", "network_id": network_id, "network": record}


@app.post("/networks/{network_id}/run-opendss")
def run_opendss_simulation(network_id: str, request: OpenDSSRunRequest):
    """
    Run a pandapower timeseries simulation on a previously converted OpenDSS network.
    Regenerates load profiles for the requested day, runs the appropriate timeseries
    (balanced or 3-phase), copies results to the standard results directory, and
    returns the same shape as the SimBench run endpoint.
    """
    if not network_id.startswith("opendss-"):
        raise HTTPException(status_code=400, detail="Not an OpenDSS network")

    parts = network_id.split("-")   # ["opendss", "N", "balanced"|"unbalanced"]
    if len(parts) != 3 or parts[1] not in NANDO_NETWORK_NAMES:
        raise HTTPException(status_code=400, detail=f"Invalid OpenDSS network id: {network_id}")

    if not 1 <= request.day <= 365:
        raise HTTPException(status_code=400, detail="day must be between 1 and 365")

    network_num  = parts[1]
    mode         = parts[2]
    network_name = NANDO_NETWORK_NAMES[network_num]
    net_subdir   = f"net_{network_num}_{network_name}"
    net_dir      = NANDO_ROOT / "dss_files" / net_subdir

    expected_file = (net_dir / "net_pp_3ph_ready.json") if mode == "unbalanced" else (net_dir / "net_pp.xlsx")
    if not expected_file.exists():
        raise HTTPException(
            status_code=400,
            detail="Converted network not found. Run the conversion pipeline first.",
        )

    ts_script = "panda_runs/pp_timeseries_3ph.py" if mode == "unbalanced" else "panda_runs/pp_timeseries.py"
    steps = [
        ("conversion/dss_files_creator.py", "Regenerate load profiles for selected day"),
        (ts_script,                          f"pandapower {mode} timeseries"),
    ]

    env = {
        **os.environ,
        "NANDO_NETWORK":      network_num,
        "NANDO_SELECTED_DAY": str(request.day),
    }

    start_time = datetime.now()
    for script_rel, label in steps:
        try:
            _run_nando_step(script_rel, label, env)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    duration = (datetime.now() - start_time).total_seconds()

    # Copy nando results to the standard backend results tree
    run_id  = f"day{request.day:03d}_{start_time.strftime('%Y%m%d-%H%M%S')}"
    src_dir = NANDO_ROOT / "results" / net_subdir
    out_dir = RESULTS_DIR / network_id / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    for sub in ["res_bus", "res_line", "res_trafo"]:
        src = src_dir / sub
        if src.exists():
            dst = out_dir / sub
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)

    has_trafo = (out_dir / "res_trafo").exists()
    v = compute_violation_counts(out_dir, has_trafo)

    save_run(
        run_id=run_id,
        network_id=network_id,
        horizon="day",
        year=2024,
        month=1,
        day=request.day,
        mode=mode,
        has_trafo=has_trafo,
        started_at=start_time,
        duration_seconds=duration,
        violations_under_voltage=v["under_voltage"],
        violations_over_voltage=v["over_voltage"],
        violations_line_overload=v["line_overload"],
        violations_trafo_overload=v["trafo_overload"],
        violations_total=v["total"],
    )

    return {
        "status": "completed",
        "network_id": network_id,
        "run_id": run_id,
        "day": request.day,
        "mode": mode,
        "started_at": start_time.isoformat(),
        "duration_seconds": round(duration, 2),
        "violations": v,
        "results": {
            "vm_pu":        f"/networks/{network_id}/results/{run_id}/vm-pu",
            "line_loading": f"/networks/{network_id}/results/{run_id}/line-loading",
            "trafo_loading": (
                f"/networks/{network_id}/results/{run_id}/trafo-loading" if has_trafo else None
            ),
        },
    }


@app.get("/")
def root():
    networks = load_networks()
    return {
        "message": "SimBench backend is running",
        "data_file_exists": DATA_FILE.exists(),
        "plots_dir_exists": PLOTS_DIR.exists(),
        "results_dir_exists": RESULTS_DIR.exists(),
        "networks_loaded": len(networks),
        "first_network": networks[0] if networks else None,
        "endpoints": [
            "/runs",
            "/networks",
            "/networks/{network_id}",
            "/networks/{network_id}/run",
            "/networks/{network_id}/results/{run_id}/vm-pu",
            "/networks/{network_id}/results/{run_id}/line-loading",
            "/networks/{network_id}/results/{run_id}/trafo-loading",
        ],
    }


@app.get("/runs")
def list_runs():
    return get_db_runs() or []


@app.get("/networks")
def networks():
    return get_db_networks() or []


@app.get("/networks/{network_id}")
def network_detail(network_id: str):
    db_net = get_db_network(network_id)
    if db_net is not None:
        return db_net
    raise HTTPException(status_code=404, detail="Network not found")


def compute_violation_counts(out_dir: Path, has_trafo: bool) -> dict:
    """
    Scan the simulation CSVs and count per-asset violations using the same
    thresholds as the frontend.  One violation = one asset (bus / line / trafo)
    that breaches the limit at any timestep during the run.
    Returns a dict with under_voltage, over_voltage, line_overload,
    trafo_overload, and total keys.
    """
    counts = dict(under_voltage=0, over_voltage=0,
                  line_overload=0, trafo_overload=0)
    try:
        vm_path = out_dir / "res_bus" / "vm_pu.csv"
        if vm_path.exists():
            vm = pd.read_csv(vm_path, sep=";", index_col=0)
            counts["under_voltage"] = int((vm.min() < V_LOWER).sum())
            counts["over_voltage"]  = int((vm.max() > V_UPPER).sum())

        line_path = out_dir / "res_line" / "loading_percent.csv"
        if line_path.exists():
            line = pd.read_csv(line_path, sep=";", index_col=0)
            counts["line_overload"] = int((line.max() > LOAD_LIMIT).sum())

        if has_trafo:
            trafo_path = out_dir / "res_trafo" / "loading_percent.csv"
            if trafo_path.exists():
                trafo = pd.read_csv(trafo_path, sep=";", index_col=0)
                counts["trafo_overload"] = int((trafo.max() > LOAD_LIMIT).sum())
    except Exception as exc:
        # Non-fatal: return whatever was counted so far.
        import logging
        logging.getLogger(__name__).warning("Violation count failed: %s", exc)

    counts["total"] = sum(counts.values())
    return counts


@app.post("/networks/{network_id}/run")
def run_simulation(network_id: str, request: RunRequest):
    if not network_exists(network_id):
        raise HTTPException(status_code=404, detail="Network not found")

    if request.mode != "balanced":
        raise HTTPException(status_code=400, detail="Only balanced mode is supported for SimBench networks")

    try:
        net = sb.get_simbench_net(network_id)

        profiles = sb.get_absolute_values(
            net,
            profiles_instead_of_study_cases=True
        )

        sb.apply_const_controllers(net, profiles)

        time_steps = get_time_steps(
            year=request.year,
            horizon=request.horizon,
            month=request.month,
            day=request.day,
            steps_per_day=96
        )

        run_id = get_run_id(request)
        out_dir = RESULTS_DIR / network_id / run_id
        out_dir.mkdir(parents=True, exist_ok=True)

        ow = ts.OutputWriter(
            net,
            output_path=str(out_dir),
            output_file_type=".csv"
        )

        ow.log_variable("res_bus", "vm_pu")
        ow.log_variable("res_line", "loading_percent")

        has_trafo = len(net.trafo) > 0
        if has_trafo:
            ow.log_variable("res_trafo", "loading_percent")

        start_time = datetime.now()
        ts.run_timeseries(net, time_steps=time_steps)
        duration_seconds = (datetime.now() - start_time).total_seconds()

        v = compute_violation_counts(out_dir, has_trafo)

        # [DB-BACKED] Persist run metadata; CSV result files are still written to disk above.
        save_run(
            run_id=run_id,
            network_id=network_id,
            horizon=request.horizon,
            year=request.year,
            month=request.month,
            day=request.day,
            mode=request.mode,
            has_trafo=has_trafo,
            started_at=start_time,
            duration_seconds=duration_seconds,
            violations_under_voltage=v["under_voltage"],
            violations_over_voltage=v["over_voltage"],
            violations_line_overload=v["line_overload"],
            violations_trafo_overload=v["trafo_overload"],
            violations_total=v["total"],
        )

        return {
            "status": "completed",
            "network_id": network_id,
            "horizon": request.horizon,
            "year": request.year,
            "month": request.month,
            "day": request.day,
            "mode": request.mode,
            "run_id": run_id,
            "started_at": start_time.isoformat(),
            "duration_seconds": duration_seconds,
            "violations": v,
            "results_available": True,
            "results": {
                "vm_pu":        f"/networks/{network_id}/results/{run_id}/vm-pu",
                "line_loading": f"/networks/{network_id}/results/{run_id}/line-loading",
                "trafo_loading": (
                    f"/networks/{network_id}/results/{run_id}/trafo-loading"
                    if has_trafo else None
                ),
            },
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/networks/{network_id}/results/{run_id}/vm-pu")
def get_vm_pu(network_id: str, run_id: str):
    file_path = RESULTS_DIR / network_id / run_id / "res_bus" / "vm_pu.csv"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="vm_pu not found")
    return FileResponse(file_path)


@app.get("/networks/{network_id}/results/{run_id}/line-loading")
def get_line_loading(network_id: str, run_id: str):
    file_path = RESULTS_DIR / network_id / run_id / "res_line" / "loading_percent.csv"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="line loading not found")
    return FileResponse(file_path)


@app.get("/networks/{network_id}/results/{run_id}/trafo-loading")
def get_trafo_loading(network_id: str, run_id: str):
    file_path = RESULTS_DIR / network_id / run_id / "res_trafo" / "loading_percent.csv"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="trafo loading not found")
    return FileResponse(file_path)


# ── Aggregated result endpoints (fast, no full-CSV download needed) ──────────

_KIND_TO_PATH: dict[str, tuple[str, str]] = {
    "vm-pu":        ("res_bus",   "vm_pu.csv"),
    "line-loading": ("res_line",  "loading_percent.csv"),
    "trafo-loading":("res_trafo", "loading_percent.csv"),
}


def _load_result_df(network_id: str, run_id: str, kind: str) -> pd.DataFrame:
    entry = _KIND_TO_PATH.get(kind)
    if not entry:
        raise HTTPException(status_code=400, detail=f"Unknown kind '{kind}'")
    sub, fname = entry
    path = RESULTS_DIR / network_id / run_id / sub / fname
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{kind} not found")
    return pd.read_csv(path, sep=";", index_col=0)


@app.get("/networks/{network_id}/results/{run_id}/{kind}/envelope")
def get_result_envelope(network_id: str, run_id: str, kind: str):
    """Return per-timestep min/mean/max across all components — much smaller than the full CSV."""
    df = _load_result_df(network_id, run_id, kind)
    return {
        "min":     [round(v, 6) for v in df.min(axis=1).tolist()],
        "mean":    [round(v, 6) for v in df.mean(axis=1).tolist()],
        "max":     [round(v, 6) for v in df.max(axis=1).tolist()],
        "columns": df.columns.tolist(),
        "n_rows":  len(df),
    }


@app.get("/networks/{network_id}/results/{run_id}/{kind}/column/{col_name:path}")
def get_result_column(network_id: str, run_id: str, kind: str, col_name: str):
    """Return the time-series for a single component (bus / line / trafo)."""
    df = _load_result_df(network_id, run_id, kind)
    if col_name not in df.columns:
        raise HTTPException(status_code=404, detail=f"Column '{col_name}' not found")
    return {
        "column": col_name,
        "values": [round(v, 6) for v in df[col_name].tolist()],
    }
