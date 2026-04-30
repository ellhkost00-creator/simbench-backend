import os
from pathlib import Path
import pandas as pd
import simbench as sb
import pandapower.timeseries as ts

BASE_DIR = Path("data/results")


def run_simulation(network_id):
    print(f"Running simulation for {network_id}")

    net = sb.get_simbench_net(network_id)

    profiles = sb.get_absolute_values(net, profiles_instead_of_study_cases=True)
    sb.apply_const_controllers(net, profiles)

    out_dir = BASE_DIR / network_id
    out_dir.mkdir(parents=True, exist_ok=True)

    ts.OutputWriter(
        net,
        output_path=str(out_dir),
        output_file_type=".csv"
    )

    ts.run_timeseries(net)

    print(f"Saved results to {out_dir}")


if __name__ == "__main__":
    # βάλε όποιο δίκτυο θες για αρχή
    run_simulation("1-LV-rural1--0-sw")