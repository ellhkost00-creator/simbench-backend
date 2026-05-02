import json
import calendar
from pathlib import Path
from typing import Literal

import simbench as sb
import pandapower.timeseries as ts

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel


app = FastAPI(title="SimBench Backend")

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
    return any(network["id"] == network_id for network in load_networks())


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
        end = start + steps_per_day

        return range(start, end)

    if horizon == "week":
        if day is None:
            raise HTTPException(status_code=400, detail="Start day is required for week horizon")

        validate_day(year, month, day)

        day_of_year = get_day_of_year(year, month, day)
        days_in_year = 366 if calendar.isleap(year) else 365

        start_day = day_of_year
        end_day = min(day_of_year + 6, days_in_year)

        start = (start_day - 1) * steps_per_day
        end = end_day * steps_per_day

        return range(start, end)

    if horizon == "month":
        days_before_month = sum(calendar.monthrange(year, m)[1] for m in range(1, month))
        days_in_month = calendar.monthrange(year, month)[1]

        start = days_before_month * steps_per_day
        end = start + days_in_month * steps_per_day

        return range(start, end)

    raise HTTPException(status_code=400, detail="Invalid horizon")


def get_run_id(request: RunRequest):
    if request.horizon == "day":
        return f"{request.year}-{request.month:02d}-{request.day:02d}"

    if request.horizon == "week":
        return f"{request.year}-{request.month:02d}-{request.day:02d}_week"

    if request.horizon == "month":
        return f"{request.year}-{request.month:02d}"

    return "unknown"


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
            "/networks",
            "/networks/{network_id}",
            "/networks/{network_id}/run",
            "/networks/{network_id}/results/{run_id}/vm-pu",
            "/networks/{network_id}/results/{run_id}/line-loading",
            "/networks/{network_id}/results/{run_id}/trafo-loading",
        ],
    }


@app.get("/networks")
def networks():
    return load_networks()


@app.get("/networks/{network_id}")
def network_detail(network_id: str):
    networks = load_networks()

    for network in networks:
        if network["id"] == network_id:
            return network

    raise HTTPException(status_code=404, detail="Network not found")


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

        ts.run_timeseries(net, time_steps=time_steps)

        return {
            "status": "completed",
            "network_id": network_id,
            "horizon": request.horizon,
            "year": request.year,
            "month": request.month,
            "day": request.day,
            "mode": request.mode,
            "run_id": run_id,
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
    