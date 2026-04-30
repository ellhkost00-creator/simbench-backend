from datetime import date
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import simbench as sb

app = FastAPI(title="SimBench Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_pure_lv_codes():
    all_codes = sb.collect_all_simbench_codes()

    return [
        code for code in all_codes
        if "-LV-" in code and not any(x in code for x in ["MV", "HV", "EHV"])
    ]


def network_metadata(code: str):
    return {
        "id": code,
        "name": f"SimBench {code}",
        "voltage": "0.4 kV",
        "type": "LV",
        "status": "validated",
        "created": str(date.today()),
        "version": "v1.0",
        "buses": 0,
        "lines": 0,
        "transformers": 0,
        "loads": 0,
    }


@app.get("/")
def root():
    return {
        "message": "SimBench backend is running",
        "endpoints": ["/networks", "/networks/{id}"]
    }


@app.get("/networks")
def networks():
    codes = get_pure_lv_codes()[:20]

    return [network_metadata(code) for code in codes]


@app.get("/networks/{network_id}")
def network_detail(network_id: str):
    codes = get_pure_lv_codes()

    if network_id not in codes:
        raise HTTPException(status_code=404, detail="Network not found")

    net = sb.get_simbench_net(network_id)

    item = network_metadata(network_id)
    item["buses"] = len(net.bus)
    item["lines"] = len(net.line)
    item["transformers"] = len(net.trafo)
    item["loads"] = len(net.load)

    return item