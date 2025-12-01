from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

import matplotlib.pyplot as plt
import numpy as np
import torch

from .color_spaces import (
    ColorSpaceAdapter,
    bt2020_to_xyz_np,
    xyz_to_rec709_rgb_torch,
)


def _linear_to_srgb(rgb: np.ndarray) -> np.ndarray:
    a = 0.055
    srgb = np.where(
        rgb <= 0.0031308,
        12.92 * rgb,
        (1 + a) * np.power(np.clip(rgb, 0.0, None), 1 / 2.4) - a,
    )
    return np.clip(srgb, 0.0, 1.0)


def generate_tone_mapping_ramp_data(
    adapter: ColorSpaceAdapter,
    bt2020_rgb: np.ndarray,
    max_luminance: float,
    num_samples: int = 256,
    sigmoid_gain: float = 3.0,
    pivot: float = 0.5,
    ratio_min: float = 0.25,
    ratio_max: float = 4.0,
) -> dict:
    """
    Sample the tone-mapping ramp without rendering an image.

    Returns a dictionary with metadata plus the intermediate XYZ / RGB arrays so
    downstream tooling (e.g., documentation exports) can reuse the values.
    """

    bt2020_rgb = np.asarray(bt2020_rgb, dtype=np.float64)
    samples = np.linspace(0.01, 1.0, num_samples)
    xyz_hdr = np.stack(
        [bt2020_to_xyz_np(bt2020_rgb * level, max_luminance) for level in samples]
    )
    tensor_xyz = torch.tensor(xyz_hdr, dtype=torch.float64, device=adapter.device)
    coords = adapter.xyz_to_coords(tensor_xyz)
    luminance_axis = coords[..., 0]
    luminance_norm = (luminance_axis - luminance_axis.min()) / (
        luminance_axis.max() - luminance_axis.min() + 1e-6
    )
    toned = torch.sigmoid(sigmoid_gain * (luminance_norm - pivot))
    coords_mapped = coords.clone()
    coords_mapped[..., 0] = (
        toned * (coords[..., 0].max() - coords[..., 0].min()) + coords[..., 0].min()
    )
    luminance_ratio = coords_mapped[..., 0] / (coords[..., 0] + 1e-6)
    luminance_ratio = torch.clamp(
        luminance_ratio, min=ratio_min, max=ratio_max
    )
    coords_mapped[..., 1:] = coords_mapped[..., 1:] * luminance_ratio.unsqueeze(-1)
    xyz_sdr = adapter.coords_to_xyz(coords_mapped)
    rgb_rec709 = torch.clamp(
        xyz_to_rec709_rgb_torch(xyz_sdr),
        0.0,
        1.0,
    )
    xyz_sdr_np = xyz_sdr.detach().cpu().numpy()
    rgb_rec709_np = rgb_rec709.detach().cpu().numpy()
    srgb = _linear_to_srgb(rgb_rec709_np)
    return {
        "bt2020_rgb": bt2020_rgb,
        "max_luminance": float(max_luminance),
        "num_samples": num_samples,
        "sigmoid_gain": float(sigmoid_gain),
        "pivot": float(pivot),
        "ratio_bounds": (float(ratio_min), float(ratio_max)),
        "samples": samples,
        "xyz_hdr": xyz_hdr,
        "xyz_sdr": xyz_sdr_np,
        "rgb_rec709": rgb_rec709_np,
        "srgb": srgb,
    }


def plot_hue_trajectory(records, adapter_name: str, output_path: str | Path) -> None:
    """
    Plot hue angle evolution from optimisation records.
    """

    iterations = [rec.iteration for rec in records]
    hues = []
    for rec in records:
        coords = rec.coords[0]
        angle = np.degrees(np.arctan2(coords[2], coords[1]))
        hues.append(angle)
    hues = np.unwrap(np.radians(hues))
    hues_deg = np.degrees(hues)
    plt.figure(figsize=(6, 3))
    plt.plot(iterations, hues_deg, marker="o", linewidth=1.5)
    plt.title(f"Hue Trajectory – {adapter_name}")
    plt.xlabel("Iteration")
    plt.ylabel("Hue (deg)")
    plt.grid(True, alpha=0.3)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout()
    plt.savefig(output_path, dpi=200)
    plt.close()


def plot_loss_landscape(surface: dict, adapter_name: str, output_path: str | Path) -> None:
    """
    Render contour map for the sampled loss landscape.
    """

    l = surface["l_offsets"]
    c = surface["c_offsets"]
    grid = surface["loss_grid"]
    plt.figure(figsize=(5, 4))
    cs = plt.contourf(c, l, grid, levels=30, cmap="viridis")
    plt.colorbar(cs, label="Loss")
    plt.xlabel("Δa")
    plt.ylabel("ΔJ")
    plt.title(f"Loss Landscape – {adapter_name}")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout()
    plt.savefig(output_path, dpi=200)
    plt.close()


def render_tone_mapping_ramp(
    adapter: ColorSpaceAdapter,
    bt2020_rgb: np.ndarray,
    max_luminance: float,
    output_path: str | Path,
    num_samples: int = 256,
    sigmoid_gain: float = 3.0,
    pivot: float = 0.5,
    ratio_min: float = 0.25,
    ratio_max: float = 4.0,
) -> None:
    """
    Apply a global sigmoid in the adapter's latent space to visualise hue drift.

    ratio_min/ratio_max bound the chroma scaling factor so low-luminance
    samples do not explode outside the Rec.709 cube.
    """

    data = generate_tone_mapping_ramp_data(
        adapter=adapter,
        bt2020_rgb=bt2020_rgb,
        max_luminance=max_luminance,
        num_samples=num_samples,
        sigmoid_gain=sigmoid_gain,
        pivot=pivot,
        ratio_min=ratio_min,
        ratio_max=ratio_max,
    )
    ramp = np.tile(data["srgb"][np.newaxis, :, :], (40, 1, 1))
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    plt.figure(figsize=(6, 1))
    plt.imshow(ramp, aspect="auto")
    plt.axis("off")
    plt.title(f"{adapter.display_name} Ramp")
    plt.tight_layout()
    plt.savefig(output_path, dpi=200, bbox_inches="tight", pad_inches=0.05)
    plt.close()
