import pandas as pd
import matplotlib.pyplot as plt
from pathlib import Path

# =========================
# AUTO FILE DETECTION
# =========================
if Path("vm_pu.xlsx").exists():
    FILE = "vm_pu.xlsx"
    df = pd.read_excel(FILE)
elif Path("vm_pu.csv").exists():
    FILE = "vm_pu.csv"
    df = pd.read_csv(FILE, sep=";")
else:
    raise FileNotFoundError("No vm_pu.csv or vm_pu.xlsx found")

print("Loaded:", FILE)

# =========================
# CLEAN DATA
# =========================
for col in df.columns:
    if col.lower() in ["time", "time_step", "index"]:
        df = df.drop(columns=[col])

df = df.apply(pd.to_numeric, errors="coerce")

# =========================
# PLOT
# =========================
plt.figure(figsize=(10, 5))

plt.plot(df.mean(axis=1), label="Mean voltage")
plt.plot(df.min(axis=1), label="Min voltage")
plt.plot(df.max(axis=1), label="Max voltage")

plt.axhline(0.95, linestyle="--", linewidth=1, label="0.95 pu")
plt.axhline(1.05, linestyle="--", linewidth=1, label="1.05 pu")

plt.title("Voltage Profile (vm_pu)")
plt.xlabel("Time step")
plt.ylabel("Voltage [pu]")

plt.grid(True, alpha=0.3)
plt.legend()
plt.tight_layout()

plt.show()