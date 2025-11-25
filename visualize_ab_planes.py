import numpy as np
import torch
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from Model import NewSUCS
from sucs import (
    lab_to_xyz_torch,
    xyz_to_linear_srgb_torch,
    linear_to_srgb_torch,
    oklab_to_linear_srgb_torch,
    Referee,
    rgb_to_oklab_np,
)


def _clamp_rgb_for_display(rgb: torch.Tensor) -> torch.Tensor:
    """
    Clamp RGB to [0, 1] for visualization only.
    """
    return torch.clamp(rgb, 0.0, 1.0)


def generate_lab_ab_plane(L: float = 50.0, a_min: float = -80.0, a_max: float = 80.0, b_min: float = -80.0, b_max: float = 80.0, size: int = 256) -> np.ndarray:
    """
    Generate an sRGB image visualizing the CIELAB a-b plane at fixed L*.
    """
    device = torch.device("cpu")

    a_vals = torch.linspace(a_min, a_max, size, device=device)
    b_vals = torch.linspace(b_min, b_max, size, device=device)
    A, B = torch.meshgrid(a_vals, b_vals, indexing="xy")

    L_tensor = torch.full_like(A, float(L))
    lab = torch.stack([L_tensor, A, B], dim=-1)  # (H, W, 3)

    xyz = lab_to_xyz_torch(lab)
    rgb_linear = xyz_to_linear_srgb_torch(xyz)
    rgb = linear_to_srgb_torch(rgb_linear)
    rgb = _clamp_rgb_for_display(rgb)

    return rgb.detach().cpu().numpy()


def generate_oklab_ab_plane(L_ok: float = 0.5, a_min: float = -0.4, a_max: float = 0.4, b_min: float = -0.4, b_max: float = 0.4, size: int = 256) -> np.ndarray:
    """
    Generate an sRGB image visualizing the OkLab a-b plane at fixed L_ok.
    """
    device = torch.device("cpu")

    a_vals = torch.linspace(a_min, a_max, size, device=device)
    b_vals = torch.linspace(b_min, b_max, size, device=device)
    A, B = torch.meshgrid(a_vals, b_vals, indexing="xy")

    L_tensor = torch.full_like(A, float(L_ok))
    oklab = torch.stack([L_tensor, A, B], dim=-1)  # (H, W, 3)

    rgb_linear = oklab_to_linear_srgb_torch(oklab)
    rgb = linear_to_srgb_torch(rgb_linear)
    rgb = _clamp_rgb_for_display(rgb)

    return rgb.detach().cpu().numpy()


def generate_sucs_ab_plane(J: float = 50.0, a_min: float = -80.0, a_max: float = 80.0, b_min: float = -80.0, b_max: float = 80.0, size: int = 256) -> np.ndarray:
    """
    Generate an sRGB image visualizing the New sUCS a'-b' plane at fixed J.

    Note: J here is the I/J coordinate of the New sUCS space. The numeric
    range is chosen to roughly match CIELAB-style scaling for visualization.
    """
    device = torch.device("cpu")
    model = NewSUCS(device=device)

    a_vals = torch.linspace(a_min, a_max, size, device=device)
    b_vals = torch.linspace(b_min, b_max, size, device=device)
    A, B = torch.meshgrid(a_vals, b_vals, indexing="xy")

    J_tensor = torch.full_like(A, float(J))
    sucs = torch.stack([J_tensor, A, B], dim=-1)  # (H, W, 3)

    rgb = model.sucs_to_srgb(sucs.view(-1, 3)).view(size, size, 3)
    rgb = _clamp_rgb_for_display(rgb)

    return rgb.detach().cpu().numpy()


def compute_average_step_norms(grid_size: int = 5) -> tuple[float, float, float]:
    """
    Compute the average step norm in each perceptual space when sampling
    a regular RGB grid (grid_size^3 points in [0, 1]^3).

    For each space, we:
      1. Convert the RGB grid into that space.
      2. Compute neighbor differences along R, G, B axes.
      3. Compute the mean Euclidean norm of these differences.
    """
    levels = np.linspace(0.0, 1.0, grid_size, dtype=np.float32)
    R, G, B = np.meshgrid(levels, levels, levels, indexing="ij")
    rgb = np.stack([R, G, B], axis=-1).reshape(-1, 3)

    # CIELAB coordinates via the Referee helper (uses colour-science).
    referee = Referee(metric="DE2000")
    lab = referee.rgb_to_lab(rgb)  # (N, 3)

    # OkLab coordinates.
    oklab = rgb_to_oklab_np(rgb)  # (N, 3)

    # New sUCS coordinates.
    device = torch.device("cpu")
    model = NewSUCS(device=device)
    rgb_t = torch.from_numpy(rgb).to(device)
    # Treat rgb_t as linear sRGB, consistent with Model.M_sRGB_to_XYZ usage.
    xyz = torch.matmul(rgb_t, model.M_sRGB_to_XYZ.T)
    sucs = model.xyz_to_sucs(xyz).detach().cpu().numpy()

    def avg_step(coords: np.ndarray) -> float:
        coords = coords.reshape(grid_size, grid_size, grid_size, -1)
        diffs = []
        dR = coords[1:, :, :, :] - coords[:-1, :, :, :]
        dG = coords[:, 1:, :, :] - coords[:, :-1, :, :]
        dB = coords[:, :, 1:, :] - coords[:, :, :-1, :]
        diffs.append(dR.reshape(-1, coords.shape[-1]))
        diffs.append(dG.reshape(-1, coords.shape[-1]))
        diffs.append(dB.reshape(-1, coords.shape[-1]))
        diffs = np.concatenate(diffs, axis=0)
        norms = np.linalg.norm(diffs, axis=1)
        return float(norms.mean())

    avg_lab = avg_step(lab)
    avg_oklab = avg_step(oklab)
    avg_sucs = avg_step(sucs)

    return avg_lab, avg_oklab, avg_sucs


def main(output_path: str = "ab_planes_L50.png"):
    """
    Generate a 1x3 figure comparing CIELAB, OkLab, and New sUCS a-b planes
    at mid lightness, rendered in sRGB.
    """
    # Use a small RGB LUT to estimate the average step magnitude in each
    # space, then scale the a/b ranges proportionally so that the planes
    # cover a comparable number of "typical" perceptual steps.
    avg_lab, avg_oklab, avg_sucs = compute_average_step_norms(grid_size=5)

    # Reference half-range for CIELAB (kept at 80, as before).
    range_lab = 80.0
    k = range_lab / avg_lab
    range_oklab = k * avg_oklab
    range_sucs = k * avg_sucs

    print("Average step norms (5x5x5 RGB grid):")
    print(f"  CIELAB: {avg_lab:.4f}")
    print(f"  OkLab : {avg_oklab:.4f}")
    print(f"  New sUCS: {avg_sucs:.4f}")
    print("Derived half ranges for a/b planes:")
    print(f"  CIELAB: {range_lab:.2f}")
    print(f"  OkLab : {range_oklab:.2f}")
    print(f"  New sUCS: {range_sucs:.2f}")

    lab_img = generate_lab_ab_plane(
        L=50.0,
        a_min=-range_lab,
        a_max=range_lab,
        b_min=-range_lab,
        b_max=range_lab,
    )
    oklab_img = generate_oklab_ab_plane(
        L_ok=0.5,
        a_min=-range_oklab,
        a_max=range_oklab,
        b_min=-range_oklab,
        b_max=range_oklab,
    )
    sucs_img = generate_sucs_ab_plane(
        J=50.0,
        a_min=-range_sucs,
        a_max=range_sucs,
        b_min=-range_sucs,
        b_max=range_sucs,
    )

    fig, axes = plt.subplots(1, 3, figsize=(12, 4))

    axes[0].imshow(lab_img, origin="lower", extent=[-range_lab, range_lab, -range_lab, range_lab])
    axes[0].set_title("CIELAB (L* = 50)")
    axes[0].set_xlabel("a*")
    axes[0].set_ylabel("b*")

    axes[1].imshow(oklab_img, origin="lower", extent=[-range_oklab, range_oklab, -range_oklab, range_oklab])
    axes[1].set_title("OkLab (L = 0.5)")
    axes[1].set_xlabel("a")
    axes[1].set_ylabel("b")

    axes[2].imshow(sucs_img, origin="lower", extent=[-range_sucs, range_sucs, -range_sucs, range_sucs])
    axes[2].set_title("New sUCS (J = 50)")
    axes[2].set_xlabel("a'")
    axes[2].set_ylabel("b'")

    for ax in axes:
        ax.set_aspect("equal")

    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    main()
