import numpy as np
import torch

from Model import NewSUCS


def build_xyz_grid(levels: int = 10, lo: float = 0.01, hi: float = 0.99) -> np.ndarray:
    """
    Generate a dense XYZ grid (levels^3 samples) within [lo, hi].
    """
    axis = np.linspace(lo, hi, levels, dtype=np.float64)
    grid = np.stack(np.meshgrid(axis, axis, axis, indexing="ij"), axis=-1)
    return grid.reshape(-1, 3).astype(np.float64)


def main() -> None:
    xyz_np = build_xyz_grid(levels=10)

    device = torch.device("cpu")
    sucs_model = NewSUCS(device=device, dtype=torch.float64)

    xyz = torch.from_numpy(xyz_np).to(device=device, dtype=torch.float64)
    sucs = sucs_model.xyz_to_sucs(xyz)
    xyz_recon = sucs_model.sucs_to_xyz(sucs)

    diff = xyz_recon - xyz
    diff_np = diff.detach().cpu().numpy()

    rmse = np.sqrt(np.mean(diff_np**2))
    mae = np.mean(np.abs(diff_np))
    max_abs = np.max(np.abs(diff_np))

    print("XYZ grid validation (10x10x10 samples)")
    print(f"  RMSE:     {rmse:.3e}")
    print(f"  MAE:      {mae:.3e}")
    print(f"  Max Abs : {max_abs:.3e}")


if __name__ == "__main__":
    main()
