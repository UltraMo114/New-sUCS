import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

import torch

from Model import NewSUCS


def original_sucs_gamma(x: np.ndarray) -> np.ndarray:
    """
    Original sUCS gamma-like nonlinearity applied to LMS:

        LMS_TM = (LMS >= 0) .* (LMS.^0.43) + (LMS < 0) .* (-(-LMS).^0.43);

    This is a signed power-law with exponent 0.43.
    """
    y = np.empty_like(x)
    mask_pos = x >= 0
    y[mask_pos] = x[mask_pos] ** 0.43
    y[~mask_pos] = -((-x[~mask_pos]) ** 0.43)
    return y


def new_sucs_naka_rushton(x: np.ndarray, model: NewSUCS) -> np.ndarray:
    """
    New sUCS Naka-Rushton response used in Model.xyz_to_sucs, including
    the auto-gain factor:

        v_g = |v|^Gamma
        s_g = Sigma^Gamma
        response = (v_g / (v_g + s_g)) * sign(v)
        lms_prime = response * Gain
    """
    device = model.Gamma.device
    dtype = model.Gamma.dtype

    v = torch.from_numpy(x.astype(np.float32)).to(device=device, dtype=dtype)

    eps = 1e-10
    v_abs = torch.abs(v)
    v_sign = torch.sign(v)

    v_g = torch.pow(v_abs, model.Gamma)
    s_g = torch.pow(model.Sigma, model.Gamma)

    response = (v_g / (v_g + s_g + eps)) * v_sign
    lms_prime = response * model.Gain

    return lms_prime.detach().cpu().numpy()


def main(output_path: str = "gamma_curves_sucs_vs_new.png"):
    """
    Compare the original sUCS power-law gamma curve with the New sUCS
    Naka-Rushton response for LMS values in a symmetric range.
    """
    # Instantiate the New sUCS model to get Gamma, Sigma, Gain.
    model = NewSUCS(device="cpu")

    # Sample LMS values in a symmetric range.
    x = np.linspace(-1.0, 1.0, 1001, dtype=np.float32)

    y_orig = original_sucs_gamma(x)
    y_new = new_sucs_naka_rushton(x, model)

    fig, axes = plt.subplots(1, 2, figsize=(10, 4))

    # Full range comparison.
    ax = axes[0]
    ax.plot(x, y_orig, label="Original sUCS power-law (0.43)", color="blue")
    ax.plot(x, y_new, label="New sUCS Naka-Rushton", color="red")
    ax.set_xlabel("LMS input")
    ax.set_ylabel("Nonlinear output")
    ax.set_title("Gamma curves (full range)")
    ax.grid(True, alpha=0.3)
    ax.legend()

    # Zoomed-in view around the origin to see low-level differences.
    ax = axes[1]
    zoom_mask = (x >= -0.2) & (x <= 0.2)
    ax.plot(x[zoom_mask], y_orig[zoom_mask], label="Original (0.43)", color="blue")
    ax.plot(x[zoom_mask], y_new[zoom_mask], label="Naka-Rushton", color="red")
    ax.set_xlabel("LMS input (zoomed)")
    ax.set_ylabel("Nonlinear output")
    ax.set_title("Gamma curves (near zero)")
    ax.grid(True, alpha=0.3)
    ax.legend()

    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    main()

