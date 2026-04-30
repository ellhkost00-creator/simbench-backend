import json
from datetime import date
from pathlib import Path
import simbench as sb

OUT_DIR = Path("data")
OUT_FILE = OUT_DIR / "networks.json"


def main():
    OUT_DIR.mkdir(exist_ok=True)

    all_codes = sb.collect_all_simbench_codes()

    pure_lv_codes = [
        code for code in all_codes
        if "-LV-" in code and not any(x in code for x in ["MV", "HV", "EHV"])
    ]

    networks = []

    for code in pure_lv_codes:
        print(f"Loading {code}...")

        net = sb.get_simbench_net(code)

        networks.append({
            "id": code,
            "name": f"SimBench {code}",
            "voltage": "0.4 kV",
            "type": "LV",
            "status": "validated",
            "created": str(date.today()),
            "version": "v1.0",
            "buses": int(len(net.bus)),
            "lines": int(len(net.line)),
            "transformers": int(len(net.trafo)),
            "loads": int(len(net.load)),
        })

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(networks, f, indent=2, ensure_ascii=False)

    print(f"Saved {len(networks)} networks to {OUT_FILE}")


if __name__ == "__main__":
    main()