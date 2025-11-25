import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from Model import NewSUCS


def original_sucs_chroma(C: np.ndarray) -> np.ndarray:
    """
    Original sUCS chroma compression from MATLAB code:

        C_1 = log(1 + 0.0447 * C) / 0.0252;

    This is applied to C >= 0. We plot it as a scalar function of C.
    """
    return np.log(1.0 + 0.0447 * C) / 0.0252


def new_sucs_chroma(C: np.ndarray, T: float) -> np.ndarray:
    """
    New sUCS chroma compression used in Model.xyz_to_sucs (dual-log):

        C_out = T1 * log(1 + C_in / T2)
    """
    T1, T2 = T
    return T1 * np.log1p(C / T2)


def main(output_path: str = "chroma_compression_sucs_vs_new.png"):
    """
    Compare the original sUCS log-based chroma compression with the
    New sUCS tanh-based chroma compression over a range of C values.
    """
    model = NewSUCS(device="cpu")
    T1 = float(model.T1.cpu().item())
    T2 = float(model.T2.cpu().item())

    # Chroma range: from 0 to a value that clearly shows saturation behaviour.
    C = np.linspace(0.0, 200.0, 1001, dtype=np.float64)

    y_orig = original_sucs_chroma(C)
    y_new = new_sucs_chroma(C, (T1, T2))

    fig, axes = plt.subplots(1, 2, figsize=(10, 4))

    # Full range comparison.
    ax = axes[0]
    ax.plot(C, y_orig, label="Original sUCS: log(1 + 0.0447*C)/0.0252", color="blue")
    ax.plot(C, y_new, label=f"New sUCS: T1*log(1 + C/T2), T1={T1:.2f}, T2={T2:.2f}", color="red")
    ax.set_xlabel("Input chroma C")
    ax.set_ylabel("Compressed chroma")
    ax.set_title("Chroma compression (full range)")
    ax.grid(True, alpha=0.3)
    ax.legend()

    # Zoom in near zero to compare low-chroma behaviour.
    ax = axes[1]
    mask = C <= 40.0
    ax.plot(C[mask], y_orig[mask], label="Original", color="blue")
    ax.plot(C[mask], y_new[mask], label="New (dual-log)", color="red")
    ax.set_xlabel("Input chroma C (zoomed)")
    ax.set_ylabel("Compressed chroma")
    ax.set_title("Chroma compression (near zero)")
    ax.grid(True, alpha=0.3)
    ax.legend()

    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    main()
