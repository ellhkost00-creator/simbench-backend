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
    horizon: Literal["day"]
    year: Literal[2016]
    month: int
    day: int
    mode: Literal["balanced"]


def load_networks():
    if not DATA_FILE.exists():
        return []

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def network_exists(network_id: str) -> bool:
    return any(network["id"] == network_id for network in load_networks())


def get_day_time_steps(month: int, day: int, steps_per_day: int = 96):
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="Invalid month")

    max_day = calendar.monthrange(2016, month)[1]

    if day < 1 or day > max_day:
        raise HTTPException(status_code=400, detail="Invalid day for selected month")

    day_of_year = sum(
        calendar.monthrange(2016, m)[1]
        for m in range(1, month)
    ) + day

    start = (day_of_year - 1) * steps_per_day
    end = start + steps_per_day

    return range(start, end)


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
            "/networks/{network_id}/results/vm-pu",
            "/networks/{network_id}/results/line-loading",
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

    if request.horizon != "day":
        raise HTTPException(status_code=400, detail="Only day horizon is supported for now")

    if request.year != 2016:
        raise HTTPException(status_code=400, detail="Only year 2016 is supported")

    if request.mode != "balanced":
        raise HTTPException(status_code=400, detail="Only balanced mode is supported for SimBench networks")

    try:
        net = sb.get_simbench_net(network_id)

        profiles = sb.get_absolute_values(
            net,
            profiles_instead_of_study_cases=True
        )

        sb.apply_const_controllers(net, profiles)

        time_steps = get_day_time_steps(
            month=request.month,
            day=request.day,
            steps_per_day=96
        )

        out_dir = RESULTS_DIR / network_id
        out_dir.mkdir(parents=True, exist_ok=True)

        ow = ts.OutputWriter(
            net,
            output_path=str(out_dir),
            output_file_type=".csv"
        )

        ow.log_variable("res_bus", "vm_pu")
        ow.log_variable("res_line", "loading_percent")

        if len(net.trafo) > 0:
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
            "results_available": True,
            "results": {
                "vm_pu": f"/networks/{network_id}/results/vm-pu",
                "line_loading": f"/networks/{network_id}/results/line-loading",
                "trafo_loading": f"/networks/{network_id}/results/trafo-loading",
            },
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/networks/{network_id}/results/vm-pu")
def get_vm_pu(network_id: str):
    file_path = RESULTS_DIR / network_id / "res_bus" / "vm_pu.csv"

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="vm_pu not found")

    return FileResponse(file_path)


@app.get("/networks/{network_id}/results/line-loading")
def get_line_loading(network_id: str):
    file_path = RESULTS_DIR / network_id / "res_line" / "loading_percent.csv"

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="line loading not found")

    return FileResponse(file_path)


@app.get("/networks/{network_id}/results/trafo-loading")
def get_trafo_loading(network_id: str):
    file_path = RESULTS_DIR / network_id / "res_trafo" / "loading_percent.csv"

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="trafo loading not found")

    return FileResponse(file_path)