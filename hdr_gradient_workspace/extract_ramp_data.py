from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR / "src"
ROOT_DIR = SCRIPT_DIR.parent
for path in (SRC_DIR, ROOT_DIR):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.append(path_str)

from src.color_spaces import build_color_space  # noqa: E402
from src.visualization import generate_tone_mapping_ramp_data  # noqa: E402

DEFAULT_RAMP_PRESETS = {
    "blue": np.array([0.05, 0.1, 1.0], dtype=np.float64),
    "red": np.array([1.0, 0.05, 0.1], dtype=np.float64),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export tone-mapping ramp samples for documentation.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--space", type=str, default="sucs", help="Adapter to evaluate.")
    parser.add_argument(
        "--preset",
        type=str,
        default="blue",
        choices=sorted(DEFAULT_RAMP_PRESETS.keys()),
        help="Preset BT.2020 colour to use when --bt2020 is not provided.",
    )
    parser.add_argument(
        "--bt2020",
        type=float,
        nargs=3,
        metavar=("R", "G", "B"),
        help="Override the preset with a custom BT.2020 triplet.",
    )
    parser.add_argument("--max-luminance", type=float, default=4000.0, help="HDR peak luminance in nits.")
    parser.add_argument("--num-samples", type=int, default=256, help="Number of ramp samples.")
    parser.add_argument("--sigmoid-gain", type=float, default=3.0, help="Gain for the global sigmoid.")
    parser.add_argument("--pivot", type=float, default=0.5, help="Sigmoid pivot in normalised luminance coordinates.")
    parser.add_argument(
        "--ratio-min",
        type=float,
        default=0.25,
        help="Lower bound for chroma scaling during luminance remapping.",
    )
    parser.add_argument(
        "--ratio-max",
        type=float,
        default=4.0,
        help="Upper bound for chroma scaling during luminance remapping.",
    )
    parser.add_argument("--device", type=str, default="cpu", help="Torch device to instantiate the adapter on.")
    parser.add_argument(
        "--output",
        type=str,
        help="Destination JSON file. Defaults to hdr_gradient_workspace/data/<space>_<label>_ramp.json.",
    )
    return parser.parse_args()


def resolve_bt2020(args: argparse.Namespace) -> np.ndarray:
    if args.bt2020 is not None:
        return np.array(args.bt2020, dtype=np.float64)
    if args.preset not in DEFAULT_RAMP_PRESETS:
        raise ValueError(f"Unknown ramp preset '{args.preset}'.")
    return DEFAULT_RAMP_PRESETS[args.preset]


def export_ramp(args: argparse.Namespace) -> Path:
    bt2020_rgb = resolve_bt2020(args)
    adapter = build_color_space(args.space, device=args.device)
    ramp_data = generate_tone_mapping_ramp_data(
        adapter=adapter,
        bt2020_rgb=bt2020_rgb,
        max_luminance=args.max_luminance,
        num_samples=args.num_samples,
        sigmoid_gain=args.sigmoid_gain,
        pivot=args.pivot,
        ratio_min=args.ratio_min,
        ratio_max=args.ratio_max,
    )
    payload = {
        "space": adapter.display_name,
        "space_key": args.space,
        "preset": None if args.bt2020 is not None else args.preset,
        "bt2020_rgb": ramp_data["bt2020_rgb"].tolist(),
        "max_luminance": ramp_data["max_luminance"],
        "num_samples": ramp_data["num_samples"],
        "sigmoid_gain": ramp_data["sigmoid_gain"],
        "pivot": ramp_data["pivot"],
        "ratio_bounds": list(ramp_data["ratio_bounds"]),
        "samples": ramp_data["samples"].tolist(),
        "xyz_hdr": ramp_data["xyz_hdr"].tolist(),
        "xyz_sdr": ramp_data["xyz_sdr"].tolist(),
        "rgb_rec709": ramp_data["rgb_rec709"].tolist(),
        "srgb": ramp_data["srgb"].tolist(),
    }

    default_name = f"{args.space}_{args.preset}_ramp.json" if payload["preset"] else f"{args.space}_custom_ramp.json"
    destination = Path(args.output) if args.output else (SCRIPT_DIR / "data" / default_name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(destination, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    return destination


def main() -> None:
    args = parse_args()
    output_path = export_ramp(args)
    print(f"[extract_ramp_data] Saved ramp samples to {output_path}")


if __name__ == "__main__":
    main()
