#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Batch runner that collects single-seed and multi-seed statistics for
colormap optimization experiments defined in sucs.py.

The script mirrors the CLI arguments of sucs.py so that one command can
generate:
  * A table summarizing per-colormap/per-color-space metrics such as
    initial σ_v, best σ_v, convergence iterations, and out-of-gamut
    fractions.
  * Multi-seed robustness statistics (mean, std, min, max) together with
    optional statistical tests and boxplots.
"""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import numpy as np
import torch

import sucs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect single-run tables and multi-seed robustness stats for New sUCS experiments."
    )
    parser.add_argument("--epochs", type=int, default=500, help="Number of epochs per optimization run.")
    parser.add_argument("--K", type=int, default=16, help="Number of control points for the spline.")
    parser.add_argument("--num-samples", type=int, default=256, help="Number of samples evaluated per colormap.")
    parser.add_argument("--lr", type=float, default=1e-2, help="Learning rate for all models.")
    parser.add_argument(
        "--color-spaces",
        type=str,
        nargs="+",
        default=["lab", "oklab", "sucs", "jzazbz"],
        help="Color-space identifiers to optimize (use 'all' for every differentiable space).",
    )
    parser.add_argument(
        "--colormaps",
        type=str,
        nargs="+",
        default=["all"],
        help="Initialization colormaps (use 'all' to expand the registry).",
    )
    parser.add_argument(
        "--lr-decay",
        type=float,
        default=1.0,
        help="Unused placeholder to stay compatible with historic configs (kept for completeness).",
    )
    parser.add_argument("--fidelity-weight", type=float, default=0.0001, help="Weight for the RGB fidelity term.")
    parser.add_argument("--gamut-penalty", type=float, default=0.0, help="Penalty weight for out-of-gamut samples.")
    parser.add_argument("--ref-metric", type=str, default="CAM16-UCS", choices=["DE2000", "CAM16-UCS"])
    parser.add_argument("--val-metric", type=str, default="CAM16-UCS", choices=["DE2000", "CAM16-UCS"])
    parser.add_argument(
        "--device",
        type=str,
        default=None,
        help="Torch device (auto / cpu / mps / cuda). Default: auto.",
    )
    parser.add_argument("--auto-adjust", action="store_true", help="Enable sigma_v-driven hyper-parameter tweaks.")
    parser.add_argument(
        "--adjust-threshold",
        type=float,
        default=0.10,
        help="If initial sigma_v is below this threshold we switch to HQ hyper-parameters.",
    )
    parser.add_argument("--hq-min-lr", type=float, default=1e-3, help="Lower bound for auto-adjusted learning rate.")
    parser.add_argument("--hq-max-K", type=int, default=32, help="Upper bound for auto-adjusted control points.")
    parser.add_argument("--hq-min-fidelity", type=float, default=0.05, help="Lower bound for fidelity weight in HQ mode.")
    parser.add_argument("--init-jitter", type=float, default=0.02, help="Initialization jitter stddev applied to latent control points.")
    parser.add_argument(
        "--multi-seed",
        type=int,
        default=10,
        help="Number of seeds for robustness analysis (set 0 to disable).",
    )
    parser.add_argument(
        "--reference-space",
        type=str,
        default="sucs",
        help="Color-space key used as reference for statistical tests.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="experiments",
        help="Directory where summary CSVs and plots will be written.",
    )
    parser.add_argument(
        "--summary-csv",
        type=str,
        default=None,
        help="Optional explicit path for the single-run summary CSV.",
    )
    parser.add_argument(
        "--multi-seed-csv",
        type=str,
        default="experiment-multi_seed.csv",
        help="Optional explicit path for the multi-seed statistics CSV.",
    )
    parser.add_argument(
        "--skip-single-run",
        action="store_true",
        help="Skip the per-colormap single-seed evaluation and only run multi-seed stats.",
    )
    parser.add_argument(
        "--skip-multi-seed",
        action="store_true",
        help="Skip the multi-seed analysis even if --multi-seed is positive.",
    )
    return parser.parse_args()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_csv(path: Path, rows: List[dict], header: Iterable[str]) -> None:
    ensure_parent(path)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(header))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def summarize_single_run(
    args: argparse.Namespace,
    device: torch.device,
    color_spaces: List[str],
    cmap_name: str,
) -> Tuple[List[dict], dict, dict]:
    """
    Run a single deterministic optimization (seed=0) and capture summary stats.
    """
    referee = sucs.Referee(metric=args.ref_metric)
    initial_rgb = sucs.build_initial_colormap(num_samples=args.num_samples, cmap_name=cmap_name)
    initial_metrics = referee.evaluate(initial_rgb)

    local_K, local_lr, local_fidelity, adjust_meta = sucs.resolve_cmap_hyperparams(
        cmap_name, args, initial_sigma_v=initial_metrics["sigma_v"]
    )

    results = sucs.run_optimization(
        num_epochs=args.epochs,
        K=local_K,
        num_samples=args.num_samples,
        color_spaces=color_spaces,
        learning_rate=local_lr,
        fidelity_weight=local_fidelity,
        adjust_metadata=adjust_meta,
        device=device,
        ref_metric=args.ref_metric,
        val_metric=args.val_metric,
        seed=0,
        cmap_name=cmap_name,
        gamut_penalty=args.gamut_penalty,
        control_point_jitter=args.init_jitter,
    )

    summary_rows: List[dict] = []
    for name in results["model_order"]:
        history_sigma = results["history_sigma"].get(name, [])
        history_sigma_val = results["history_sigma_val"].get(name, [])
        fraction_history = results["history_fraction_outside"].get(name, [])

        initial_sigma_report = float(history_sigma[0]) if history_sigma else math.nan
        initial_sigma_val = float(history_sigma_val[0]) if history_sigma_val else math.nan
        final_fraction = float(fraction_history[-1]) if fraction_history else math.nan

        row = {
            "cmap": cmap_name,
            "color_space": name,
            "initial_sigma_ref": float(initial_metrics["sigma_v"]),
            "initial_sigma_report": initial_sigma_report,
            "initial_sigma_val": initial_sigma_val,
            "best_sigma_report": float(results["best_sigma_report"].get(name, math.inf)),
            "best_sigma_val": float(results["best_sigma_val"].get(name, math.inf)),
            "best_epoch": int(results["best_epoch"].get(name, 0)),
            "convergence_iter": int(results["convergence_iters"].get(name, -1)),
            "final_fraction_outside": final_fraction,
            "num_epochs": args.epochs,
            "K": local_K,
            "learning_rate": local_lr,
            "fidelity_weight": local_fidelity,
            "auto_adjust": bool(adjust_meta.get("auto_adjust", False)),
        }
        summary_rows.append(row)

    hyperparams = {
        "K": local_K,
        "lr": local_lr,
        "fidelity": local_fidelity,
        "adjust_meta": adjust_meta,
        "initial_sigma_ref": float(initial_metrics["sigma_v"]),
    }
    return summary_rows, results, hyperparams


def compute_statistical_tests(
    best_sigmas: Dict[str, List[float]],
    reference_space: str,
) -> Dict[str, dict]:
    """
    Compute optional statistical tests vs. the reference space. Falls back to empty
    results if SciPy is not available.
    """
    try:
        from scipy import stats as scipy_stats  # type: ignore
    except Exception:
        print("SciPy is not available; skipping statistical tests.")
        return {}

    if reference_space not in best_sigmas:
        print(f"Reference color space '{reference_space}' not found in multi-seed data.")
        return {}

    reference_values = np.asarray(best_sigmas[reference_space], dtype=np.float64)
    if reference_values.size == 0:
        return {}

    tests: Dict[str, dict] = {}
    for name, values in best_sigmas.items():
        arr = np.asarray(values, dtype=np.float64)
        entry = {
            "paired_t_stat_vs_ref": math.nan,
            "paired_t_p_vs_ref": math.nan,
            "mannwhitney_u_vs_ref": math.nan,
            "mannwhitney_p_vs_ref": math.nan,
        }
        if name == reference_space:
            tests[name] = entry
            continue
        if arr.size == 0:
            tests[name] = entry
            continue
        if arr.size == reference_values.size:
            t_stat, t_p = scipy_stats.ttest_rel(reference_values, arr)
            entry["paired_t_stat_vs_ref"] = float(t_stat)
            entry["paired_t_p_vs_ref"] = float(t_p)
        u_stat, u_p = scipy_stats.mannwhitneyu(reference_values, arr, alternative="two-sided")
        entry["mannwhitney_u_vs_ref"] = float(u_stat)
        entry["mannwhitney_p_vs_ref"] = float(u_p)
        tests[name] = entry

    return tests


def summarize_multi_seed(
    args: argparse.Namespace,
    device: torch.device,
    model_order: List[str],
    cmap_name: str,
    hyperparams: dict,
    output_dir: Path,
) -> Tuple[List[dict], Path]:
    """
    Run multi-seed sweeps and summarize robustness stats (mean/std/min/max).
    """
    best_sigmas = sucs.run_multi_seed(
        num_seeds=args.multi_seed,
        num_epochs=args.epochs,
        K=hyperparams["K"],
        num_samples=args.num_samples,
        color_spaces=model_order,
        learning_rate=hyperparams["lr"],
        fidelity_weight=hyperparams["fidelity"],
        device=device,
        ref_metric=args.ref_metric,
        val_metric=args.val_metric,
        cmap_name=cmap_name,
        gamut_penalty=args.gamut_penalty,
        control_point_jitter=args.init_jitter,
    )

    boxplot_path = output_dir / f"{cmap_name}_robustness_boxplot_{args.multi_seed}seeds.png"
    sucs.plot_robustness_boxplot(
        best_sigmas_per_model=best_sigmas,
        output_path=str(boxplot_path),
        metric_name=args.val_metric,
        model_order=model_order,
    )

    test_results = compute_statistical_tests(best_sigmas, args.reference_space)

    stats_rows: List[dict] = []
    for name in model_order:
        values = best_sigmas.get(name, [])
        arr = np.asarray(values, dtype=np.float64)
        row = {
            "cmap": cmap_name,
            "color_space": name,
            "num_seeds": len(values),
            "mean_best_sigma_val": float(arr.mean()) if arr.size else math.nan,
            "std_best_sigma_val": float(arr.std(ddof=1)) if arr.size > 1 else 0.0,
            "min_best_sigma_val": float(arr.min()) if arr.size else math.nan,
            "max_best_sigma_val": float(arr.max()) if arr.size else math.nan,
            "boxplot_path": str(boxplot_path),
        }
        tests = test_results.get(name)
        if tests:
            row.update(tests)
        else:
            row.update(
                {
                    "paired_t_stat_vs_ref": math.nan,
                    "paired_t_p_vs_ref": math.nan,
                    "mannwhitney_u_vs_ref": math.nan,
                    "mannwhitney_p_vs_ref": math.nan,
                }
            )
        stats_rows.append(row)

    return stats_rows, boxplot_path


def main() -> None:
    args = parse_args()

    if args.ref_metric.upper() != "CAM16-UCS" or args.val_metric.upper() != "CAM16-UCS":
        print("[collect_uniformity_metrics] Forcing CAM16-UCS as referee/validation metric for consistency.")
    args.ref_metric = "CAM16-UCS"
    args.val_metric = "CAM16-UCS"
    if args.init_jitter < 0.0:
        args.init_jitter = 0.0

    device_name = args.device or "auto"
    if device_name == "auto":
        if torch.cuda.is_available():
            device_name = "cuda"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            device_name = "mps"
        else:
            device_name = "cpu"

    if device_name.startswith("cuda") and not torch.cuda.is_available():
        raise ValueError("Requested CUDA device, but torch.cuda.is_available() is False.")
    if device_name == "mps" and not (hasattr(torch.backends, "mps") and torch.backends.mps.is_available()):
        raise ValueError("Requested MPS device, but torch.backends.mps.is_available() is False.")

    device = torch.device(device_name)
    print(f"[collect_uniformity_metrics] Using device: {device}")

    color_spaces, skipped_spaces = sucs.resolve_color_spaces(args.color_spaces)
    if skipped_spaces:
        print(f"Skipping unsupported color spaces: {', '.join(skipped_spaces)}")
    if not color_spaces:
        raise ValueError("No valid color spaces selected.")

    colormaps = sucs.resolve_colormaps(args.colormaps)
    if not colormaps:
        raise ValueError("No colormaps resolved from the provided list.")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    summary_rows: List[dict] = []
    multi_seed_rows: List[dict] = []

    for cmap in colormaps:
        print("=" * 60)
        print(f"[collect_uniformity_metrics] Processing cmap: {cmap}")

        results = None
        hyperparams: dict | None = None

        if not args.skip_single_run:
            rows, results, hyperparams = summarize_single_run(args, device, color_spaces, cmap)
            summary_rows.extend(rows)
            print(f"  -> collected {len(rows)} summary rows for {cmap}")
        else:
            # Even when skipping we still need hyperparameters (default to CLI values).
            hyperparams = {
                "K": args.K,
                "lr": args.lr,
                "fidelity": args.fidelity_weight,
                "adjust_meta": {},
                "initial_sigma_ref": math.nan,
            }
            print("  -> single-run summary skipped by user request.")

        if args.multi_seed > 0 and not args.skip_multi_seed:
            if hyperparams is None:
                raise RuntimeError("Hyper-parameters missing before multi-seed execution.")
            model_order = results["model_order"] if results is not None else color_spaces
            stats_rows, boxplot_path = summarize_multi_seed(
                args, device, model_order, cmap, hyperparams, output_dir
            )
            multi_seed_rows.extend(stats_rows)
            print(f"  -> multi-seed stats written; boxplot stored at {boxplot_path}")
        else:
            print("  -> multi-seed analysis skipped.")

    if summary_rows:
        summary_path = Path(args.summary_csv) if args.summary_csv else output_dir / "uniformity_summary.csv"
        summary_header = [
            "cmap",
            "color_space",
            "initial_sigma_ref",
            "initial_sigma_report",
            "initial_sigma_val",
            "best_sigma_report",
            "best_sigma_val",
            "best_epoch",
            "convergence_iter",
            "final_fraction_outside",
            "num_epochs",
            "K",
            "learning_rate",
            "fidelity_weight",
            "auto_adjust",
        ]
        write_csv(summary_path, summary_rows, summary_header)
        print(f"[collect_uniformity_metrics] Summary table saved to {summary_path}")

    if multi_seed_rows:
        multi_seed_path = Path(args.multi_seed_csv) if args.multi_seed_csv else output_dir / "multi_seed_stats.csv"
        multi_seed_header = [
            "cmap",
            "color_space",
            "num_seeds",
            "mean_best_sigma_val",
            "std_best_sigma_val",
            "min_best_sigma_val",
            "max_best_sigma_val",
            "boxplot_path",
            "paired_t_stat_vs_ref",
            "paired_t_p_vs_ref",
            "mannwhitney_u_vs_ref",
            "mannwhitney_p_vs_ref",
        ]
        write_csv(multi_seed_path, multi_seed_rows, multi_seed_header)
        print(f"[collect_uniformity_metrics] Multi-seed stats saved to {multi_seed_path}")


if __name__ == "__main__":
    main()
