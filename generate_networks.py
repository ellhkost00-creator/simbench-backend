import json
from datetime import date
from pathlib import Path

import simbench as sb
from pandapower.plotting.plotly import simple_plotly

OUT_DIR = Path("data")
PLOTS_DIR = OUT_DIR / "plots"
OUT_FILE = OUT_DIR / "networks.json"


def safe_filename(code: str) -> str:
    return code.replace("/", "_").replace("\\", "_") + ".html"


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
        plot_path = PLOTS_DIR / plot_filename

        print(f"Creating interactive plot for {code}...")

        try:
            fig = simple_plotly(
                net,
                auto_open=False,
                showlegend=True,
                respect_switches=True,
            )

            fig.update_layout(
                autosize=True,
                margin=dict(l=10, r=10, t=10, b=10),
                height=650,
                legend=dict(
                orientation="v",
                x=1.02,
                y=1,
                xanchor="left",
                yanchor="top",
            ),
        )

            fig.write_html(
                str(plot_path),
                include_plotlyjs="cdn",
                full_html=True,
                config={
                "responsive": True,
                "displayModeBar": True,
                "scrollZoom": True,
            },
        )
            plot_url = f"/plots/{plot_filename}"

        except Exception as e:
            print(f"Plot failed for {code}: {e}")
            plot_url = None

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
            "plot_url": plot_url,
        })

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(networks, f, indent=2, ensure_ascii=False)

    print(f"Saved {len(networks)} networks to {OUT_FILE}")
    print(f"Plots saved to {PLOTS_DIR}")


if __name__ == "__main__":
    main()