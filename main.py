import json
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
    get_db_runs, init_db, save_run, seed_networks_from_file,
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

        # [DB-BACKED] Persist run metadata; CSV files are still written to disk above.
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
                "vm_pu": f"/networks/{network_id}/results/{run_id}/vm-pu",
                "line_loading": f"/networks/{network_id}/results/{run_id}/line-loading",
                "trafo_loading": f"/networks/{network_id}/results/{run_id}/trafo-loading" if has_trafo else None,
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