from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR / "src"
ROOT_DIR = SCRIPT_DIR.parent
for path in (SRC_DIR, ROOT_DIR):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.append(path_str)

from src.color_spaces import build_color_space  # noqa: E402
from src.optimizer import HDRGradientOptimizer, OptimizationSettings  # noqa: E402
from src.stimuli import HDRStimulus, generate_extreme_stimuli, stimuli_from_config  # noqa: E402
from src.visualization import (  # noqa: E402
    plot_hue_trajectory,
    plot_loss_landscape,
    render_tone_mapping_ramp,
)

DEFAULT_RAMP_PRESETS = {
    "blue": np.array([0.05, 0.1, 1.0]),
    "red": np.array([1.0, 0.05, 0.1]),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="HDR Gradient Stress Test – sUCS vs. JzAzBz vs. ICtCp",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--spaces",
        nargs="+",
        default=["sucs", "jzazbz", "ictcp"],
        help="Colour spaces to evaluate.",
    )
    parser.add_argument(
        "--stimuli-config",
        type=str,
        help="Optional JSON file describing custom HDR stimuli.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(SCRIPT_DIR / "artifacts"),
        help="Destination for figures and logs.",
    )
    parser.add_argument("--iterations", type=int, default=500, help="Optimizer steps.")
    parser.add_argument("--lr", type=float, default=5e-3, help="Adam learning rate.")
    parser.add_argument(
        "--lambda-metric", type=float, default=1.0, help="Weight for ΔE term."
    )
    parser.add_argument(
        "--lambda-hue", type=float, default=0.01, help="Weight for hue-stability term."
    )
    parser.add_argument(
        "--lambda-luminance",
        type=float,
        default=0.1,
        help="Weight for SDR luminance penalty.",
    )
    parser.add_argument(
        "--lambda-gamut", type=float, default=5.0, help="Weight for gamut penalty."
    )
    parser.add_argument(
        "--gamut-projection",
        type=str,
        default="hue_preserving",
        choices=["none", "hue_preserving"],
        help="Projection strategy to keep Rec.709 gamut.",
    )
    parser.add_argument(
        "--gradient-clip", type=float, default=50.0, help="Gradient clipping norm."
    )
    parser.add_argument(
        "--device",
        type=str,
        default="cpu",
        help="Torch device (cpu / cuda).",
    )
    parser.add_argument(
        "--landscape-steps",
        type=int,
        default=60,
        help="Resolution of the loss-landscape grid.",
    )
    parser.add_argument(
        "--sigmoid-gain",
        type=float,
        default=3.0,
        help="Gain of the global sigmoid used in hue-shift ramp visualisations.",
    )
    parser.add_argument(
        "--tone-luminance",
        type=float,
        default=4000.0,
        help="Peak luminance for the tone-mapping ramps.",
    )
    return parser.parse_args()


def load_stimuli(args: argparse.Namespace) -> List[HDRStimulus]:
    if args.stimuli_config:
        with open(args.stimuli_config, "r", encoding="utf-8") as handle:
            config = json.load(handle)
        return stimuli_from_config(config)
    return generate_extreme_stimuli()


def run_pipeline(args: argparse.Namespace) -> dict:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stimuli = load_stimuli(args)
    settings = OptimizationSettings(
        lr=args.lr,
        iterations=args.iterations,
        lambda_metric=args.lambda_metric,
        lambda_hue=args.lambda_hue,
        lambda_luminance=args.lambda_luminance,
        lambda_gamut=args.lambda_gamut,
        gradient_clip=args.gradient_clip,
        device=args.device,
        gamut_projection_mode=args.gamut_projection,
    )
    summary = {"results": [], "spaces": args.spaces, "stimuli": [s.as_dict() for s in stimuli]}

    for space_name in args.spaces:
        adapter = build_color_space(space_name, device=args.device)
        space_dir = output_dir / space_name
        space_dir.mkdir(parents=True, exist_ok=True)

        for stimulus in stimuli:
            stim_dir = space_dir / stimulus.name
            stim_dir.mkdir(parents=True, exist_ok=True)
            optimizer = HDRGradientOptimizer(
                adapter=adapter,
                target_xyz=stimulus.xyz(),
                settings=settings,
            )
            final_record = optimizer.run()
            surface = optimizer.evaluate_loss_grid(steps=args.landscape_steps)
            history_path = stim_dir / "history.json"
            with open(history_path, "w", encoding="utf-8") as handle:
                json.dump(
                    [
                        {
                            "iteration": rec.iteration,
                            "loss": rec.loss,
                            "metric_loss": rec.metric_loss,
                            "hue_loss": rec.hue_loss,
                            "luminance_loss": rec.luminance_loss,
                            "gamut_loss": rec.gamut_loss,
                            "grad_norm": rec.grad_norm,
                            "fraction_outside": rec.fraction_outside,
                            "luminance_weight": rec.luminance_weight,
                            "gamut_weight": rec.gamut_weight,
                        }
                        for rec in optimizer.history
                    ],
                    handle,
                    indent=2,
                )

            hue_plot = stim_dir / "hue_trajectory.png"
            plot_hue_trajectory(optimizer.history, adapter.display_name, hue_plot)
            landscape_plot = stim_dir / "loss_landscape.png"
            plot_loss_landscape(surface, adapter.display_name, landscape_plot)
            tone_plot = stim_dir / "tone_ramp.png"
            render_tone_mapping_ramp(
                adapter=adapter,
                bt2020_rgb=stimulus.bt2020_rgb,
                max_luminance=stimulus.luminance_nits,
                output_path=tone_plot,
                sigmoid_gain=args.sigmoid_gain,
            )

            summary_entry = {
                "space": adapter.display_name,
                "space_key": space_name,
                "stimulus": stimulus.as_dict(),
                "final_loss": final_record.loss,
                "cam16_delta": optimizer.final_cam16_delta(),
                "history_path": str(history_path),
                "hue_plot": str(hue_plot),
                "landscape_plot": str(landscape_plot),
                "tone_plot": str(tone_plot),
                "final_rgb": optimizer.history[-1].rgb_rec709.tolist() if optimizer.history else [],
            }
            summary["results"].append(summary_entry)

        # Ramp diagnostics once per colour space
        ramp_dir = space_dir / "ramps"
        ramp_dir.mkdir(exist_ok=True)
        for label, rgb in DEFAULT_RAMP_PRESETS.items():
            render_tone_mapping_ramp(
                adapter=adapter,
                bt2020_rgb=rgb,
                max_luminance=args.tone_luminance,
                output_path=ramp_dir / f"{label}_ramp.png",
                sigmoid_gain=args.sigmoid_gain,
            )

    summary_path = output_dir / "summary.json"
    with open(summary_path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)
    return {"summary_path": str(summary_path), "num_results": len(summary["results"])}


def main() -> None:
    args = parse_args()
    report = run_pipeline(args)
    print(f"[HDR Gradient Stress Test] Completed {report['num_results']} runs.")
    print(f"Summary written to: {report['summary_path']}")


if __name__ == "__main__":
    main()
