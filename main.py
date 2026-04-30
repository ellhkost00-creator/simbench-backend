import json
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

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

if PLOTS_DIR.exists():
    app.mount("/plots", StaticFiles(directory=PLOTS_DIR), name="plots")


def load_networks():
    if not DATA_FILE.exists():
        return []

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


@app.get("/")
def root():
    networks = load_networks()

    return {
        "message": "SimBench backend is running",
        "data_file_exists": DATA_FILE.exists(),
        "plots_dir_exists": PLOTS_DIR.exists(),
        "networks_loaded": len(networks),
        "first_network": networks[0] if networks else None,
        "endpoints": ["/networks", "/networks/{network_id}", "/plots/{plot_file}"]
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