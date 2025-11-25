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
    New sUCS chroma compression used in Model.xyz_to_sucs:

        C_out = T * tanh(C_in / T)
    """
    return T * np.tanh(C / T)


def main(output_path: str = "chroma_compression_sucs_vs_new.png"):
    """
    Compare the original sUCS log-based chroma compression with the
    New sUCS tanh-based chroma compression over a range of C values.
    """
    model = NewSUCS(device="cpu")
    T = float(model.T.cpu().item())

    # Chroma range: from 0 to a value that clearly shows saturation behaviour.
    C = np.linspace(0.0, 200.0, 1001, dtype=np.float64)

    y_orig = original_sucs_chroma(C)
    y_new = new_sucs_chroma(C, T)

    fig, axes = plt.subplots(1, 2, figsize=(10, 4))

    # Full range comparison.
    ax = axes[0]
    ax.plot(C, y_orig, label="Original sUCS: log(1 + 0.0447*C)/0.0252", color="blue")
    ax.plot(C, y_new, label=f"New sUCS: T*tanh(C/T), T={T:.2f}", color="red")
    ax.set_xlabel("Input chroma C")
    ax.set_ylabel("Compressed chroma")
    ax.set_title("Chroma compression (full range)")
    ax.grid(True, alpha=0.3)
    ax.legend()

    # Zoom in near zero to compare low-chroma behaviour.
    ax = axes[1]
    mask = C <= 40.0
    ax.plot(C[mask], y_orig[mask], label="Original", color="blue")
    ax.plot(C[mask], y_new[mask], label="New (tanh)", color="red")
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

