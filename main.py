from datetime import date
from fastapi import FastAPI
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


@app.get("/")
def root():
    return {
        "message": "SimBench backend is running",
        "endpoints": ["/networks"]
    }


@app.get("/networks")
def networks():
    all_codes = sb.collect_all_simbench_codes()

    pure_lv_codes = [
        code for code in all_codes
        if "-LV-" in code and not any(x in code for x in ["MV", "HV", "EHV"])
    ]

    # προσωρινά μόνο τα πρώτα 20 για να φορτώνει γρήγορα
    pure_lv_codes = pure_lv_codes[:20]

    return [
        {
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
        for code in pure_lv_codes
    ]