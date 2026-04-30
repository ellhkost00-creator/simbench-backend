import json
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="SimBench Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_FILE = Path("data/networks.json")


def load_networks():
    if not DATA_FILE.exists():
        return []

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


@app.get("/")
def root():
    return {
        "message": "SimBench backend is running",
        "endpoints": ["/networks", "/networks/{network_id}"]
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