import json
from pathlib import Path


def build_table(summary_path: Path) -> str:
    data = json.loads(summary_path.read_text())
    rows = {}
    for entry in data["results"]:
        stim = entry["stimulus"]["name"]
        space = entry["space_key"]
        cam = entry.get("cam16_delta", float("nan"))
        rows.setdefault(stim, {})[space] = cam
    ordered = sorted(
        rows.keys(), key=lambda x: (int(x.split("_")[-1].replace("nits", "")), x)
    )
    lines = [
        "| Stimulus | sUCS ΔE_CAM16 | JzAzBz ΔE_CAM16 | ICtCp ΔE_CAM16 |",
        "| --- | --- | --- | --- |",
    ]
    for stim in ordered:
        vals = rows[stim]
        lines.append(
            f"| {stim} | {vals.get('sucs', float('nan')):.3f} | "
            f"{vals.get('jzazbz', float('nan')):.3f} | {vals.get('ictcp', float('nan')):.3f} |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    summary_file = Path("hdr_gradient_workspace/report_runs/summary.json")
    print(build_table(summary_file))
