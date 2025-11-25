import numpy as np
import argparse

import torch
import torch.nn as nn
import torch.nn.functional as F

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401

import colour
from Model import NewSUCS as NewSUCSModel


# ============================================================
# Module A: Referee (perceptual uniformity metrics)
# ============================================================


class Referee:
    """
    Perceptual referee for colormap uniformity.

    默认使用 CAM16-UCS 作为感知距离度量，以避免对 CIELAB
    baseline 的偏置（避免“既当裁判员又当运动员”）。
    原始 Bujack et al. 使用的是 CIEDE2000 + CIELAB，这里将
    ΔE_00 换成 CAM16-UCS 的欧氏距离。
    """

    def __init__(self, metric: str = "DE2000"):
        """
        Args:
            metric: 'DE2000' (default) or 'CAM16-UCS'.
        """
        metric = metric.upper()
        if metric in ("DE2000", "CIEDE2000", "CIE 2000"):
            self.metric = "DE2000"
        elif metric in ("CAM16-UCS", "CAM16UCS", "CAM16"):
            self.metric = "CAM16-UCS"
        else:
            raise ValueError(f"Unsupported referee metric: {metric}")

    @staticmethod
    def rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
        """
        Convert sRGB samples in [0, 1] to CIELAB (D65, 2°).

        Args:
            rgb: (N, 3) array in [0, 1].
        Returns:
            lab: (N, 3) array in CIELAB.
        """
        rgb = np.clip(rgb, 0.0, 1.0).astype(np.float32)
        # Use the same sRGB -> linear RGB -> XYZ pipeline as the UCS model:
        # 1) sRGB gamma decoding
        # 2) Matrix transform with the standard sRGB-to-XYZ matrix
        rgb_linear = srgb_to_linear_np(rgb)
        xyz = rgb_linear @ _SRGB_TO_XYZ.T
        lab = colour.XYZ_to_Lab(xyz)
        return lab

    @staticmethod
    def rgb_to_cam16ucs(rgb: np.ndarray) -> np.ndarray:
        """
        Convert sRGB samples in [0, 1] to CAM16-UCS J'a'b'.

        使用与 UCS 模型一致的 sRGB → 线性 → XYZ 管线，再用
        colour-science 提供的 XYZ_to_CAM16UCS 进行变换。
        """
        rgb = np.clip(rgb, 0.0, 1.0).astype(np.float32)
        rgb_linear = srgb_to_linear_np(rgb)
        xyz = rgb_linear @ _SRGB_TO_XYZ.T
        ucs = colour.XYZ_to_CAM16UCS(xyz)
        return ucs

    def evaluate(self, rgb: np.ndarray) -> dict:
        """
        Evaluate a colormap using local uniformity in the chosen metric.

        Args:
            rgb: (N, 3) array in [0, 1].

        Returns:
            dict with keys:
              - 'sigma_v': std of neighbor distances.
              - 'v_min': min neighbor distance.
        """
        # New implementation: select distance metric (default: CIEDE2000).
        if getattr(self, "metric", "DE2000") == "DE2000":
            lab = self.rgb_to_lab(rgb)
            lab1 = lab[:-1]
            lab2 = lab[1:]
            d = colour.delta_E(lab1, lab2, method="CIE 2000")
        else:
            ucs = self.rgb_to_cam16ucs(rgb)
            ucs1 = ucs[:-1]
            ucs2 = ucs[1:]
            d = colour.delta_E(ucs1, ucs2, method="CAM16-UCS")

        sigma_v = float(np.std(d, ddof=1))
        v_min = float(np.min(d))

        return {"sigma_v": sigma_v, "v_min": v_min}
        # 先转到 CAM16-UCS 空间，再在该空间内计算相邻点的欧氏感知距离。
        ucs = self.rgb_to_cam16ucs(rgb)

        # Neighbor distances: d_i = ΔE_CAM16UCS(C_i, C_{i+1})
        ucs1 = ucs[:-1]
        ucs2 = ucs[1:]
        d = colour.delta_E(ucs1, ucs2, method="CAM16-UCS")

        sigma_v = float(np.std(d, ddof=1))
        v_min = float(np.min(d))

        return {"sigma_v": sigma_v, "v_min": v_min}


# ============================================================
# Module B: Optimization Spaces (CIELAB, OkLab, New sUCS proxy)
# ============================================================


def resample_control_points(control_points: torch.Tensor, num_samples: int) -> torch.Tensor:
    """
    Linearly resample 1D control points to a dense sequence.

    Args:
        control_points: (K, 3) tensor.
        num_samples: number of samples in the output sequence.
    Returns:
        (num_samples, 3) tensor.
    """
    if control_points.shape[0] == num_samples:
        return control_points

    # Use 1D linear interpolation along the first dimension.
    # Input: (1, 3, K) -> Output: (1, 3, num_samples)
    x = control_points.T.unsqueeze(0)  # (1, 3, K)
    x_upsampled = F.interpolate(x, size=num_samples, mode="linear", align_corners=True)
    return x_upsampled.squeeze(0).T  # (num_samples, 3)


def lab_to_xyz_torch(lab: torch.Tensor) -> torch.Tensor:
    """
    CIELAB (D65, 2°) -> XYZ, differentiable PyTorch implementation.
    """
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]

    fy = (L + 16.0) / 116.0
    fx = fy + a / 500.0
    fz = fy - b / 200.0

    delta = 6.0 / 29.0
    delta_tensor = lab.new_tensor(delta)

    def f_inv(t: torch.Tensor) -> torch.Tensor:
        return torch.where(
            t > delta_tensor,
            t ** 3,
            3.0 * (delta_tensor ** 2) * (t - 4.0 / 29.0),
        )

    fx3 = f_inv(fx)
    fy3 = f_inv(fy)
    fz3 = f_inv(fz)

    # D65 white point (Xn, Yn, Zn) with Yn = 1.0
    Xn = lab.new_tensor(0.95047)
    Yn = lab.new_tensor(1.0)
    Zn = lab.new_tensor(1.08883)

    X = Xn * fx3
    Y = Yn * fy3
    Z = Zn * fz3

    return torch.stack([X, Y, Z], dim=-1)


def xyz_to_linear_srgb_torch(xyz: torch.Tensor) -> torch.Tensor:
    """
    XYZ (D65) -> linear sRGB.
    """
    M = xyz.new_tensor(
        [
            [3.2404542, -1.5371385, -0.4985314],
            [-0.9692660, 1.8760108, 0.0415560],
            [0.0556434, -0.2040259, 1.0572252],
        ]
    )
    return torch.matmul(xyz, M.T)


def linear_to_srgb_torch(rgb_linear: torch.Tensor) -> torch.Tensor:
    """
    Linear RGB -> sRGB (gamma encoded), differentiable and branch-wise.
    """
    threshold = rgb_linear.new_tensor(0.0031308)
    mask = rgb_linear <= threshold

    srgb = torch.zeros_like(rgb_linear)
    srgb[mask] = 12.92 * rgb_linear[mask]
    # For the high branch the inputs are guaranteed > threshold >= 0,
    # so no additional clamp is needed for the power.
    srgb[~mask] = 1.055 * torch.pow(rgb_linear[~mask], 1.0 / 2.4) - 0.055
    return srgb


# OkLab matrices from Björn Ottosson (2020).
_OKLAB_M1 = np.array(
    [
        [0.8189330101, 0.3618667424, -0.1288597137],
        [0.0329845436, 0.9293118715, 0.0361456387],
        [0.0482003018, 0.2643662691, 0.6338517070],
    ],
    dtype=np.float64,
)

_OKLAB_M2 = np.array(
    [
        [0.2104542553, 0.7936177850, -0.0040720468],
        [1.9779984951, -2.4285922050, 0.4505937099],
        [0.0259040371, 0.7827717662, -0.8086757660],
    ],
    dtype=np.float64,
)

_OKLAB_M1_INV = np.array(
    [
        [1.2270138511, -0.5577999807, 0.2812561490],
        [-0.0405801784, 1.1122568696, -0.0716766787],
        [-0.0763812845, -0.4214819784, 1.5861632204],
    ],
    dtype=np.float64,
)

_OKLAB_M2_INV = np.array(
    [
        [1.0000000000, 0.3963377774, 0.2158037573],
        [1.0000000000, -0.1055613458, -0.0638541728],
        [1.0000000000, -0.0894841775, -1.2914855480],
    ],
    dtype=np.float64,
)


def srgb_to_linear_np(rgb: np.ndarray) -> np.ndarray:
    """
    sRGB in [0, 1] -> linear RGB (numpy, for initialization).
    """
    rgb = np.clip(rgb, 0.0, 1.0)
    mask = rgb <= 0.04045
    linear = np.empty_like(rgb)
    linear[mask] = rgb[mask] / 12.92
    linear[~mask] = ((rgb[~mask] + 0.055) / 1.055) ** 2.4
    return linear


# Standard sRGB to XYZ matrix (D65), matching the UCS model in Model.py.
_SRGB_TO_XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ],
    dtype=np.float64,
)


def rgb_to_oklab_np(rgb: np.ndarray) -> np.ndarray:
    """
    sRGB in [0, 1] -> OkLab (numpy, for initialization).
    """
    rgb_lin = srgb_to_linear_np(rgb)
    lms = rgb_lin @ _OKLAB_M1.T
    lms_cbrt = np.cbrt(lms)
    oklab = lms_cbrt @ _OKLAB_M2.T
    return oklab


def oklab_to_linear_srgb_torch(oklab: torch.Tensor) -> torch.Tensor:
    """
    OkLab -> linear sRGB (PyTorch, differentiable).
    """
    M2_inv = oklab.new_tensor(_OKLAB_M2_INV)
    M1_inv = oklab.new_tensor(_OKLAB_M1_INV)

    # LMS' = M2_inv * OkLab
    lms_prime = torch.matmul(oklab, M2_inv.T)
    # Nonlinear decompress
    lms = lms_prime ** 3
    rgb_linear = torch.matmul(lms, M1_inv.T)
    return rgb_linear


class BaselineCIELAB(nn.Module):
    """
    Baseline model operating in CIELAB space.

    The parameters are Lab control points; output is sRGB after
    Lab -> XYZ -> linear RGB -> gamma, followed by a hard clamp
    to [0, 1] to simulate gamut clipping artifacts:
      - Nardini et al., CGF 2021.
    """

    def __init__(self, init_control_points_lab: np.ndarray, device: str = "cpu"):
        super().__init__()
        cp = torch.from_numpy(init_control_points_lab.astype(np.float32))
        self.control_points = nn.Parameter(cp.to(device))
        # Fraction of samples outside [0, 1] before clamping in the last forward pass.
        self.last_fraction_outside: float = 0.0

    def forward(self, num_samples: int = 256) -> torch.Tensor:
        dense_lab = resample_control_points(self.control_points, num_samples)
        xyz = lab_to_xyz_torch(dense_lab)
        rgb_linear = xyz_to_linear_srgb_torch(xyz)
        rgb = linear_to_srgb_torch(rgb_linear)
        # Measure how many samples are outside the displayable [0, 1] range
        # before clamping (simulates gamut clipping artifacts).
        with torch.no_grad():
            outside = (rgb < 0.0) | (rgb > 1.0)
            self.last_fraction_outside = float(outside.float().mean().cpu().item())
        # Hard clipping in output space (simulates gamut clipping)
        rgb = torch.clamp(rgb, 0.0, 1.0)
        return rgb


class BaselineOkLab(nn.Module):
    """
    Baseline model operating in OkLab space.

    Parameters live in OkLab; output is sRGB via:
      OkLab -> LMS' -> LMS -> linear RGB -> gamma,
    followed by hard clamp to [0, 1] to simulate gamut clipping.
    """

    def __init__(self, init_control_points_oklab: np.ndarray, device: str = "cpu"):
        super().__init__()
        cp = torch.from_numpy(init_control_points_oklab.astype(np.float32))
        self.control_points = nn.Parameter(cp.to(device))
        self.last_fraction_outside: float = 0.0

    def forward(self, num_samples: int = 256) -> torch.Tensor:
        dense_oklab = resample_control_points(self.control_points, num_samples)
        rgb_linear = oklab_to_linear_srgb_torch(dense_oklab)
        rgb = linear_to_srgb_torch(rgb_linear)
        with torch.no_grad():
            outside = (rgb < 0.0) | (rgb > 1.0)
            self.last_fraction_outside = float(outside.float().mean().cpu().item())
        rgb = torch.clamp(rgb, 0.0, 1.0)
        return rgb


class OursNewSUCS(nn.Module):
    """
    New sUCS optimization space using the UCS implementation from Model.py.

    Parameters live in the New sUCS latent space (J, a', b'). The mapping
    to sRGB is performed by the NewSUCS colour model, which uses
    Naka-Rushton response and tanh-based chroma compression (soft saturation)
    and does not apply any hard clipping in RGB space.
    """

    def __init__(self, init_control_points_rgb: np.ndarray, device: str = "cpu"):
        super().__init__()
        self.device = torch.device(device)
        # Bound for a' and b' components in the New sUCS latent space.
        # Based on ab-plane analysis, ±40–45 covers the bulk of the sRGB
        # gamut at mid lightness without pushing far into out-of-gamut
        # regions. We choose a conservative ±40 here.
        self.ab_bound: float = 40.0

        # Fixed, differentiable colour transform: sUCS <-> XYZ <-> sRGB.
        self.sucs_model = NewSUCSModel(device=self.device, dtype=torch.float32)

        # Initialize control points in sUCS space corresponding to the
        # provided RGB control points.
        rgb_np = init_control_points_rgb.astype(np.float32)
        rgb_t = torch.from_numpy(rgb_np).to(self.device)

        with torch.no_grad():
            # For initialization we follow the usage example in Model.py:
            # treat RGB inputs as linear and map directly to XYZ.
            xyz = torch.matmul(rgb_t, self.sucs_model.M_sRGB_to_XYZ.T)
            sucs = self.sucs_model.xyz_to_sucs(xyz)
            # Constrain initial a'/b' to stay within a reasonable range so
            # that optimization starts well inside the sUCS "ball" that maps
            # safely to sRGB.
            # sucs[:, 1:] = torch.clamp(sucs[:, 1:], -self.ab_bound, self.ab_bound)

        self.control_points = nn.Parameter(sucs)
        # Fraction of samples outside [0, 1] in the last forward pass.
        self.last_fraction_outside: float = 0.0

    def forward(self, num_samples: int = 256) -> torch.Tensor:
        # Interpolate in the New sUCS latent space.
        dense_sucs = resample_control_points(self.control_points, num_samples)
        # Map to sRGB through the New sUCS model (soft saturation, no hard clamp).
        rgb = self.sucs_model.sucs_to_srgb(dense_sucs)
        with torch.no_grad():
            outside = (rgb < 0.0) | (rgb > 1.0)
            self.last_fraction_outside = float(outside.float().mean().cpu().item())
        return rgb


# ============================================================
# Module C: Optimization Loop
# ============================================================


def build_initial_colormap(num_samples: int = 256, cmap_name: str = "rainbow") -> np.ndarray:
    """
    Build an initial non-uniform colormap (e.g., matplotlib's 'rainbow').
    """
    cmap = plt.get_cmap(cmap_name)
    xs = np.linspace(0.0, 1.0, num_samples)
    rgba = cmap(xs)
    rgb = rgba[:, :3]
    return rgb.astype(np.float32)


def create_models(
    K: int = 10,
    num_samples: int = 256,
    device: str | torch.device = "cpu",
) -> tuple[BaselineCIELAB, BaselineOkLab, OursNewSUCS, np.ndarray]:
    """
    Create three models and return them together with the initial RGB colormap.
    """
    device = torch.device(device)

    initial_rgb = build_initial_colormap(num_samples=num_samples, cmap_name="rainbow")

    # Extract K control points evenly spaced along the initial colormap.
    indices = np.linspace(0, num_samples - 1, K, dtype=int)
    control_points_rgb = initial_rgb[indices]

    referee = Referee()
    control_points_lab = referee.rgb_to_lab(control_points_rgb)
    control_points_oklab = rgb_to_oklab_np(control_points_rgb)

    model_lab = BaselineCIELAB(control_points_lab, device=device)
    model_oklab = BaselineOkLab(control_points_oklab, device=device)
    model_sucs = OursNewSUCS(control_points_rgb, device=device)

    return model_lab, model_oklab, model_sucs, initial_rgb


def run_optimization(
    num_epochs: int = 500,
    K: int = 10,
    num_samples: int = 256,
    device: str | torch.device | None = None,
    ref_metric: str = "DE2000",
    seed: int | None = 0,
):
    """
    Run joint optimization for all three models.
    """
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        device = torch.device(device)

    # Optional global seeding for reproducibility. When running multi-seed
    # experiments we will call this function with different seed values.
    if seed is not None:
        torch.manual_seed(seed)
        np.random.seed(seed)

    model_lab, model_oklab, model_sucs, initial_rgb = create_models(
        K=K, num_samples=num_samples, device=device
    )

    models = {
        "lab": model_lab,
        "oklab": model_oklab,
        "sucs": model_sucs,
    }

    # Optimizer with separate parameter groups and unified learning rates.
    optimizer = torch.optim.Adam(
        [
            {"params": model_lab.parameters(), "lr": 1e-2},
            {"params": model_oklab.parameters(), "lr": 1e-2},
            {"params": model_sucs.parameters(), "lr": 1e-2},
        ]
    )

    referee = Referee(metric=ref_metric)

    history_sigma = {name: [] for name in models}
    history_vmin = {name: [] for name in models}
    history_loss = {name: [] for name in models}
    history_fraction_outside = {name: [] for name in models}
    history_grad_norm = {name: [] for name in models}

    # Best-epoch tracking in terms of referee sigma_v (CIEDE2000).
    best_sigma = {name: float("inf") for name in models}
    best_epoch = {name: 0 for name in models}
    best_colormaps = {}

    for epoch in range(num_epochs):
        optimizer.zero_grad()

        for name, model in models.items():
            rgb_out = model(num_samples=num_samples)

            # Training loss: variance of neighbor distances in latent space.
            dense_latent = resample_control_points(model.control_points, num_samples)
            dists = torch.norm(dense_latent[1:] - dense_latent[:-1], dim=1)
            loss = torch.var(dists)
            loss.backward()

            # Gradient norm of control points (diagnostic for optimization behaviour).
            grad = model.control_points.grad
            grad_norm = float(grad.detach().norm().cpu().item()) if grad is not None else 0.0
            history_grad_norm[name].append(grad_norm)

            history_loss[name].append(float(loss.detach().cpu().item()))

            with torch.no_grad():
                rgb_np = rgb_out.detach().cpu().numpy()
                metrics = referee.evaluate(rgb_np)
                history_sigma[name].append(metrics["sigma_v"])
                history_vmin[name].append(metrics["v_min"])

                # Fraction of samples outside [0, 1] (pre-clamp for baselines,
                # direct for New sUCS) in the last forward pass.
                frac_out = getattr(model, "last_fraction_outside", float("nan"))
                history_fraction_outside[name].append(float(frac_out))

                # Best-epoch tracking w.r.t. referee sigma_v.
                sigma = metrics["sigma_v"]
                if sigma < best_sigma[name] - 1e-6:
                    best_sigma[name] = sigma
                    best_epoch[name] = epoch
                    best_colormaps[name] = rgb_np.copy()

        optimizer.step()

        # Project New sUCS control points back into a reasonable a'/b' range
        # after each optimization step to reduce the chance of drifting far
        # outside the sRGB gamut. This is a constraint in the latent space,
        # not a hard clamp in RGB.
        with torch.no_grad():
            if isinstance(model_sucs, OursNewSUCS):
                bound = model_sucs.ab_bound
                model_sucs.control_points.data[:, 1:].clamp_(-bound, bound)

        if (epoch + 1) % 50 == 0 or epoch == 0:
            print(
                f"[Epoch {epoch + 1}/{num_epochs}] "
                f"sigma_v: "
                f"Lab={history_sigma['lab'][-1]:.4f}, "
                f"OkLab={history_sigma['oklab'][-1]:.4f}, "
                f"New sUCS={history_sigma['sucs'][-1]:.4f}"
            )

    # Collect last-epoch RGB colormaps.
    last_colormaps = {}
    for name, model in models.items():
        with torch.no_grad():
            rgb = model(num_samples=num_samples).detach().cpu().numpy()
        last_colormaps[name] = rgb

    # Early-stopping colormaps: use the best referee sigma_v checkpoint
    # for each model. If for some reason a model never improved, fall
    # back to the last-epoch colormap.
    final_colormaps = {}
    for name in models.keys():
        if name in best_colormaps and len(best_colormaps[name]) > 0:
            final_colormaps[name] = best_colormaps[name]
        else:
            final_colormaps[name] = last_colormaps[name]

    # Simple convergence speed metric based on referee sigma_v:
    # iteration index where sigma_v first enters a 10% band above
    # its final value.
    convergence_iters = {}
    for name, sigma_list in history_sigma.items():
        sigma_arr = np.asarray(sigma_list, dtype=np.float64)
        initial = float(sigma_arr[0])
        final = float(sigma_arr[-1])
        if initial <= final:
            # No improvement or divergence: mark as last epoch.
            conv_iter = len(sigma_arr) - 1
        else:
            target = final + 0.1 * (initial - final)
            conv_iter = len(sigma_arr) - 1
            for i, val in enumerate(sigma_arr):
                if val <= target:
                    conv_iter = i
                    break
        convergence_iters[name] = conv_iter

    print("Convergence iterations (10% band above final sigma_v):")
    for name in ["lab", "oklab", "sucs"]:
        print(f"  {name}: {convergence_iters[name]}")

    print("Best referee sigma_v and epochs:")
    for name in ["lab", "oklab", "sucs"]:
        print(f"  {name}: best_sigma={best_sigma[name]:.4f} at epoch={best_epoch[name]}")

    # Diagnostics for New sUCS mapping to sRGB at the final epoch.
    if isinstance(model_sucs, OursNewSUCS):
        sm = model_sucs.sucs_model
        try:
            lin_min, lin_max = sm.last_linear_range
            gam_min, gam_max = sm.last_gamma_range
            print("New sUCS -> sRGB diagnostics (final epoch):")
            print(f"  linear RGB range: {lin_min:.6f} to {lin_max:.6f}")
            print(f"  gamma  RGB range: {gam_min:.6f} to {gam_max:.6f}")
            print(f"  fraction outside [0,1] (linear): {sm.last_fraction_outside_linear:.6f}")
            print(f"  fraction outside [0,1] (gamma): {sm.last_fraction_outside_gamma:.6f}")
        except Exception:
            # Diagnostics are best-effort only; do not break the main run.
            pass

    return {
        "history_sigma": history_sigma,
        "history_vmin": history_vmin,
        "history_loss": history_loss,
        "history_fraction_outside": history_fraction_outside,
        "history_grad_norm": history_grad_norm,
        "final_colormaps": final_colormaps,
        "last_colormaps": last_colormaps,
        "initial_rgb": initial_rgb,
        "convergence_iters": convergence_iters,
        "best_sigma": best_sigma,
        "best_epoch": best_epoch,
        "best_colormaps": best_colormaps,
    }


# ============================================================
# Module D: Visualization
# ============================================================


def plot_optimization_curves(
    history_sigma: dict,
    output_path: str = "optimization_uniformity.png",
    baseline_scores: dict | None = None,
):
    """
    Plot σ_v vs. iteration for the three models (referee metric-dependent).
    Optionally overlays horizontal baseline lines for static colormaps
    (e.g., Viridis, Magma, Plasma).
    """
    fig, ax = plt.subplots(figsize=(7, 4))

    iters = range(len(next(iter(history_sigma.values()))))

    ax.plot(iters, history_sigma["lab"], color="red", label="Baseline CIELAB")
    ax.plot(iters, history_sigma["oklab"], color="green", label="Baseline OkLab")
    ax.plot(iters, history_sigma["sucs"], color="blue", label="Ours New sUCS")

    if baseline_scores:
        styles = {
            "viridis": ("purple", "Viridis"),
            "magma": ("brown", "Magma"),
            "plasma": ("orange", "Plasma"),
        }
        for key, (color, nice_name) in styles.items():
            if key in baseline_scores:
                sigma = baseline_scores[key]
                ax.axhline(
                    sigma,
                    color=color,
                    linestyle="--",
                    linewidth=1.0,
                    label=f"{nice_name} (σ_v={sigma:.3f})",
                )

    ax.set_xlabel("Iteration")
    ax.set_ylabel("Referee σ_v")
    ax.set_title("Colormap Uniformity (σ_v)")
    ax.grid(True, alpha=0.3)
    ax.legend()

    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def compute_global_speed_matrix(rgb: np.ndarray, referee: Referee) -> np.ndarray:
    """
    Compute the global speed matrix V_{i,j} using the referee's metric.

    For metric = 'DE2000':
        V_{i,j} = ΔE_00(c_i, c_j) / |i - j|
    For metric = 'CAM16-UCS':
        V_{i,j} = ΔE_CAM16UCS(c_i, c_j) / |i - j|
    """
    if getattr(referee, "metric", "DE2000") == "DE2000":
        lab = referee.rgb_to_lab(rgb)
        N = lab.shape[0]
        lab_a = lab[:, None, :]
        lab_b = lab[None, :, :]
        d = colour.delta_E(lab_a, lab_b, method="CIE 2000")
    else:
        ucs = referee.rgb_to_cam16ucs(rgb)
        N = ucs.shape[0]
        ucs_a = ucs[:, None, :]
        ucs_b = ucs[None, :, :]
        d = colour.delta_E(ucs_a, ucs_b, method="CAM16-UCS")

    indices = np.arange(N)
    i_grid, j_grid = np.meshgrid(indices, indices, indexing="ij")
    steps = np.abs(i_grid - j_grid).astype(np.float64)

    with np.errstate(divide="ignore", invalid="ignore"):
        V = np.where(steps == 0, 0.0, d / steps)
    return V


def compute_neighbor_distances(rgb: np.ndarray, referee: Referee) -> np.ndarray:
    """
    Compute perceptual neighbor distances along a 1D colormap.

    For metric = 'DE2000':
        d_i = ΔE_00(c_i, c_{i+1})
    For metric = 'CAM16-UCS':
        d_i = ΔE_CAM16UCS(c_i, c_{i+1})
    """
    if getattr(referee, "metric", "DE2000") == "DE2000":
        lab = referee.rgb_to_lab(rgb)
        lab1 = lab[:-1]
        lab2 = lab[1:]
        d = colour.delta_E(lab1, lab2, method="CIE 2000")
    else:
        ucs = referee.rgb_to_cam16ucs(rgb)
        ucs1 = ucs[:-1]
        ucs2 = ucs[1:]
        d = colour.delta_E(ucs1, ucs2, method="CAM16-UCS")
    return np.asarray(d, dtype=np.float64)


def plot_global_speed_matrices(
    final_colormaps: dict,
    referee: Referee,
    output_path: str = "global_speed_matrix.png",
):
    """
    Plot 1×3 heatmaps of the global speed matrices after optimization.
    """
    names = ["lab", "oklab", "sucs"]
    titles = ["Baseline CIELAB", "Baseline OkLab", "Ours New sUCS"]

    matrices = [compute_global_speed_matrix(final_colormaps[name], referee) for name in names]
    vmax = max(m.max() for m in matrices)

    fig, axes = plt.subplots(1, 3, figsize=(12, 4), sharex=True, sharey=True)

    for ax, V, title in zip(axes, matrices, titles):
        im = ax.imshow(V, origin="lower", cmap="viridis", vmin=0.0, vmax=vmax)
        ax.set_title(title)
        ax.set_xlabel("j")
        ax.set_ylabel("i")

    fig.colorbar(im, ax=axes.ravel().tolist(), shrink=0.8, label="Speed (ΔE_00 / |i - j|)")

    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def build_srgb_gamut_boundary_samples(num: int = 16) -> np.ndarray:
    """
    Sample the sRGB gamut boundary as a set of RGB points.
    """
    g = np.linspace(0.0, 1.0, num)
    G, B = np.meshgrid(g, g)

    # Six faces of the cube [0,1]^3
    faces = []

    # R = 0
    faces.append(np.stack([np.zeros_like(G), G, B], axis=-1).reshape(-1, 3))
    # R = 1
    faces.append(np.stack([np.ones_like(G), G, B], axis=-1).reshape(-1, 3))
    # G = 0
    faces.append(np.stack([G, np.zeros_like(G), B], axis=-1).reshape(-1, 3))
    # G = 1
    faces.append(np.stack([G, np.ones_like(G), B], axis=-1).reshape(-1, 3))
    # B = 0
    faces.append(np.stack([G, B, np.zeros_like(G)], axis=-1).reshape(-1, 3))
    # B = 1
    faces.append(np.stack([G, B, np.ones_like(G)], axis=-1).reshape(-1, 3))

    boundary_rgb = np.vstack(faces)
    return boundary_rgb.astype(np.float32)


def plot_gamut_trajectory(
    final_colormaps: dict,
    referee: Referee,
    output_path: str = "gamut_trajectory_lab.png",
):
    """
    Plot 3D trajectories of the optimized colormaps in CIELAB space, together
    with an sRGB gamut boundary.
    """
    boundary_rgb = build_srgb_gamut_boundary_samples(num=14)
    boundary_lab = referee.rgb_to_lab(boundary_rgb)

    fig = plt.figure(figsize=(8, 6))
    ax = fig.add_subplot(111, projection="3d")

    # Gamut boundary as semi-transparent point cloud.
    ax.scatter(
        boundary_lab[:, 0],
        boundary_lab[:, 1],
        boundary_lab[:, 2],
        c="lightgray",
        alpha=0.1,
        s=5,
        linewidths=0,
    )

    # Trajectories of the three colormaps.
    colors = {"lab": "red", "oklab": "green", "sucs": "blue"}
    labels = {"lab": "Baseline CIELAB", "oklab": "Baseline OkLab", "sucs": "Ours New sUCS"}

    for name in ["lab", "oklab", "sucs"]:
        rgb = final_colormaps[name]
        lab = referee.rgb_to_lab(rgb)
        ax.plot(
            lab[:, 0],
            lab[:, 1],
            lab[:, 2],
            color=colors[name],
            linewidth=2.0,
            label=labels[name],
        )

    ax.set_xlabel("L*")
    ax.set_ylabel("a*")
    ax.set_zlabel("b*")
    ax.set_title("Optimized Colormap Trajectories in CIELAB Space")
    ax.legend()

    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def plot_colormap_strips(
    colormaps: dict,
    referee: Referee,
    output_path: str = "colormap_strips_comparison.png",
):
    """
    Plot colormap strips together with local speed plots for
    initial, CIELAB, OkLab, and New sUCS colormaps.

    Args:
        colormaps: dict with keys 'initial', 'lab', 'oklab', 'sucs';
                   values are (N, 3) numpy arrays in [0, 1].
    """
    names = ["initial", "lab", "oklab", "sucs"]
    titles = {
        "initial": "Initial (Rainbow)",
        "lab": "Baseline CIELAB",
        "oklab": "Baseline OkLab",
        "sucs": "Ours New sUCS",
    }

    num_rows = len(names)
    fig, axes = plt.subplots(
        num_rows,
        2,
        figsize=(10, 2.5 * num_rows),
        gridspec_kw={"width_ratios": [1, 2]},
    )

    if num_rows == 1:
        axes = np.array([axes])

    for row, name in enumerate(names):
        rgb = colormaps[name]
        strip = rgb[np.newaxis, :, :]

        ax_strip = axes[row, 0]
        ax_speed = axes[row, 1]

        ax_strip.imshow(strip, aspect="auto")
        ax_strip.set_axis_off()
        ax_strip.set_title(titles[name])

        d = compute_neighbor_distances(rgb, referee)
        x = np.linspace(0.0, 1.0, len(d))
        ax_speed.plot(x, d, color="black", linewidth=1.0)
        ax_speed.grid(True, alpha=0.3)
        ax_speed.set_ylabel("Local speed")

    axes[-1, 1].set_xlabel("Position")

    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def plot_gradient_dynamics(
    history_grad_norm: dict,
    history_fraction_outside: dict,
    output_path: str = "gradient_norm_dynamics.png",
):
    """
    Plot gradient norm dynamics and boundary contact rate over iterations
    for the three models.
    """
    iters = range(len(next(iter(history_grad_norm.values()))))

    fig, axes = plt.subplots(2, 1, figsize=(7, 6), sharex=True)

    colors = {"lab": "red", "oklab": "green", "sucs": "blue"}
    labels = {"lab": "Baseline CIELAB", "oklab": "Baseline OkLab", "sucs": "Ours New sUCS"}

    for name in ["lab", "oklab", "sucs"]:
        axes[0].plot(iters, history_grad_norm[name], color=colors[name], label=labels[name])
        axes[1].plot(iters, history_fraction_outside[name], color=colors[name], label=labels[name])

    axes[0].set_ylabel("Grad norm")
    axes[0].set_title("Gradient Norm Dynamics")
    axes[0].grid(True, alpha=0.3)
    axes[0].legend()

    axes[1].set_xlabel("Iteration")
    axes[1].set_ylabel("Fraction outside [0, 1]")
    axes[1].set_title("Boundary Contact Rate")
    axes[1].grid(True, alpha=0.3)

    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def _build_sine_wave_field(height: int, width: int, periods: int = 4) -> np.ndarray:
    """
    Build a sine-wave grating scalar field in [0, 1].
    """
    x = np.linspace(0.0, 1.0, width)
    phase = 2.0 * np.pi * periods * x
    line = 0.5 + 0.45 * np.sin(phase)
    field = np.tile(line[np.newaxis, :], (height, 1))
    return field.astype(np.float32)


def _build_pyramid_field(height: int, width: int) -> np.ndarray:
    """
    Build a simple pyramid scalar field in [0, 1] with a central maximum.
    """
    y = np.linspace(0.0, 1.0, height)
    x = np.linspace(0.0, 1.0, width)
    X, Y = np.meshgrid(x, y)
    center = 0.5
    r = np.maximum(np.abs(X - center), np.abs(Y - center))
    r_norm = r / np.max(r)
    field = 1.0 - r_norm
    field = np.clip(field, 0.0, 1.0)
    return field.astype(np.float32)


def _apply_colormap_to_scalar(field: np.ndarray, rgb_colormap: np.ndarray) -> np.ndarray:
    """
    Map a scalar field in [0, 1] to an RGB image using a discrete colormap.
    """
    h, w = field.shape
    n = rgb_colormap.shape[0]
    idx = np.clip((field * (n - 1)).astype(int), 0, n - 1)
    img = rgb_colormap[idx]
    return img.reshape(h, w, 3)


def plot_mach_band_tests(
    final_colormaps: dict,
    output_path: str = "mach_band_test.png",
):
    """
    Generate Mach-band-style test images (sine-wave grating and pyramid)
    using the three optimized colormaps.
    """
    height, width = 128, 256

    sine_field = _build_sine_wave_field(height, width, periods=4)
    pyramid_field = _build_pyramid_field(height, width)

    fields = [sine_field, pyramid_field]
    field_titles = ["Sine-wave grating", "Pyramid"]

    names = ["lab", "oklab", "sucs"]
    titles = {"lab": "Baseline CIELAB", "oklab": "Baseline OkLab", "sucs": "Ours New sUCS"}

    fig, axes = plt.subplots(
        len(fields),
        len(names),
        figsize=(4 * len(names), 3 * len(fields)),
        squeeze=False,
    )

    for i, field in enumerate(fields):
        for j, name in enumerate(names):
            rgb_cmap = final_colormaps[name]
            img = _apply_colormap_to_scalar(field, rgb_cmap)

            ax = axes[i, j]
            ax.imshow(img, origin="lower", aspect="auto")
            ax.axis("off")

            if i == 0:
                ax.set_title(titles[name])
            if j == 0:
                ax.set_ylabel(field_titles[i])

    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def run_multi_seed(
    num_seeds: int,
    num_epochs: int,
    K: int,
    num_samples: int,
    device: str | torch.device,
    ref_metric: str = "DE2000",
) -> dict:
    """
    Run the optimization for multiple random seeds and collect the best
    referee sigma_v for each model.
    """
    best_sigmas_per_model = {name: [] for name in ["lab", "oklab", "sucs"]}

    for seed in range(num_seeds):
        print(f"[Multi-seed] Running seed {seed + 1}/{num_seeds}")
        results = run_optimization(
            num_epochs=num_epochs,
            K=K,
            num_samples=num_samples,
            device=device,
            ref_metric=ref_metric,
            seed=seed,
        )
        for name in ["lab", "oklab", "sucs"]:
            best_sigmas_per_model[name].append(results["best_sigma"][name])

    return best_sigmas_per_model


def plot_robustness_boxplot(
    best_sigmas_per_model: dict,
    output_path: str = "robustness_boxplot.png",
):
    """
    Plot a boxplot of best sigma_v over multiple seeds for each model.
    """
    labels = ["Baseline CIELAB", "Baseline OkLab", "Ours New sUCS"]
    data = [
        best_sigmas_per_model["lab"],
        best_sigmas_per_model["oklab"],
        best_sigmas_per_model["sucs"],
    ]

    fig, ax = plt.subplots(figsize=(6, 4))
    bp = ax.boxplot(data, labels=labels, patch_artist=True)

    colors = ["red", "green", "blue"]
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.4)

    ax.set_ylabel("Best σ_v (CIEDE2000)")
    ax.set_title("Robustness over Random Initialization Seeds")
    ax.grid(True, axis="y", alpha=0.3)

    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def compute_referee_sigma_for_cmap(
    cmap_name: str,
    num_samples: int,
    referee: Referee,
) -> float:
    """
    Compute referee sigma_v for a static Matplotlib colormap.
    """
    cmap = plt.get_cmap(cmap_name)
    xs = np.linspace(0.0, 1.0, num_samples)
    rgba = cmap(xs)
    rgb = rgba[:, :3]
    metrics = referee.evaluate(rgb)
    return metrics["sigma_v"]


def plot_benchmark_comparison(
    history_sigma: dict,
    baseline_scores: dict,
    output_path: str = "benchmark_comparison.png",
):
    """
    Plot optimization curves together with horizontal baseline lines for
    standard Matplotlib colormaps (Viridis, Magma, Plasma).
    """
    plot_optimization_curves(
        history_sigma=history_sigma,
        output_path=output_path,
        baseline_scores=baseline_scores,
    )


def main():
    parser = argparse.ArgumentParser(description="Colormap optimization testbed for New sUCS vs. CIELAB and OkLab")
    parser.add_argument("--epochs", type=int, default=500, help="Number of optimization epochs per run")
    parser.add_argument("--K", type=int, default=10, help="Number of control points")
    parser.add_argument("--num-samples", type=int, default=256, help="Number of samples in each colormap strip")
    parser.add_argument(
        "--ref-metric",
        type=str,
        default="DE2000",
        choices=["DE2000", "CAM16-UCS"],
        help="Referee metric for local uniformity",
    )
    parser.add_argument(
        "--multi-seed",
        type=int,
        default=0,
        help="Number of random seeds for robustness test (0 disables multi-seed run)",
    )

    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device}")

    # Single-seed main experiment (seed=0 for reproducibility).
    results = run_optimization(
        num_epochs=args.epochs,
        K=args.K,
        num_samples=args.num_samples,
        device=device,
        ref_metric=args.ref_metric,
        seed=0,
    )

    referee = Referee(metric=args.ref_metric)

    # Compute benchmark sigma_v for standard static colormaps.
    baseline_scores = {
        "viridis": compute_referee_sigma_for_cmap("viridis", args.num_samples, referee),
        "magma": compute_referee_sigma_for_cmap("magma", args.num_samples, referee),
        "plasma": compute_referee_sigma_for_cmap("plasma", args.num_samples, referee),
    }

    # Plots required by the base TASK.md.
    plot_optimization_curves(
        results["history_sigma"],
        output_path="optimization_uniformity.png",
    )
    plot_global_speed_matrices(results["final_colormaps"], referee, output_path="global_speed_matrix.png")
    plot_gamut_trajectory(results["final_colormaps"], referee, output_path="gamut_trajectory_lab.png")

    # Additional plots required by refinement.md (TVCG extension).
    colormap_dict = dict(results["final_colormaps"])
    colormap_dict["initial"] = results["initial_rgb"]
    plot_colormap_strips(colormap_dict, referee, output_path="colormap_strips_comparison.png")
    plot_gradient_dynamics(
        results["history_grad_norm"],
        results["history_fraction_outside"],
        output_path="gradient_norm_dynamics.png",
    )
    plot_mach_band_tests(results["final_colormaps"], output_path="mach_band_test.png")
    plot_benchmark_comparison(results["history_sigma"], baseline_scores, output_path="benchmark_comparison.png")

    # Optional robustness stress test over multiple seeds.
    if args.multi_seed > 0:
        best_sigmas_per_model = run_multi_seed(
            num_seeds=args.multi_seed,
            num_epochs=args.epochs,
            K=args.K,
            num_samples=args.num_samples,
            device=device,
            ref_metric=args.ref_metric,
        )
        plot_robustness_boxplot(best_sigmas_per_model, output_path="robustness_boxplot.png")

    print("Saved figures:")
    print("  - optimization_uniformity.png")
    print("  - global_speed_matrix.png")
    print("  - gamut_trajectory_lab.png")
    print("  - colormap_strips_comparison.png")
    print("  - gradient_norm_dynamics.png")
    print("  - mach_band_test.png")
    print("  - benchmark_comparison.png")
    if args.multi_seed > 0:
        print("  - robustness_boxplot.png")


if __name__ == "__main__":
    main()
