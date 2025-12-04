# -*- coding: utf-8 -*-
import numpy as np
import argparse
from typing import Callable, Dict, List

import torch
import torch.nn as nn
import torch.nn.functional as F

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401

import colour
from Model import NewSUCS as NewSUCSModel

# Registries for colour spaces and colormap initializations.
COLOR_SPACE_REGISTRY: Dict[str, type["BaseColorSpace"]] = {}
COLOR_SPACE_DISPLAY_NAMES: Dict[str, str] = {}
COLORMAP_REGISTRY: Dict[str, str] = {}
MODEL_COLOR_HINTS: Dict[str, str] = {
    "lab": "tab:red",
    "oklab": "tab:green",
    "sucs": "tab:blue",
    "jzazbz": "tab:orange",
    "cam16ucs": "tab:brown",
}
BASELINE_CMAP_METRICS: Dict[str, float] = {
    "viridis": 0.0705,
    "plasma": 0.1567,
    "inferno": 0.1420,
    "magma": 0.1420,
    "cividis": 0.0911,
    "turbo": 0.2140,
}


def register_color_space(name: str, display_name: str):
    """
    Decorator used to register a color-space optimization module.
    """

    def decorator(cls: type["BaseColorSpace"]) -> type["BaseColorSpace"]:
        COLOR_SPACE_REGISTRY[name] = cls
        COLOR_SPACE_DISPLAY_NAMES[name] = display_name
        cls.space_name = name
        cls.display_name = display_name
        return cls

    return decorator


def register_colormap(name: str, mpl_name: str | None = None) -> None:
    """
    Register a Matplotlib colormap for initialization.
    """

    COLORMAP_REGISTRY[name] = mpl_name or name


class BaseColorSpace(nn.Module):
    """
    Shared scaffolding for differentiable colour spaces.
    """

    space_name: str = "base"
    display_name: str = "Base"
    supports_optimization: bool = True

    def __init__(self, init_control_points_rgb: np.ndarray, device: str = "cpu"):
        super().__init__()
        self.device = torch.device(device)
        self.setup_latent_space(self.device)
        latent = self.rgb_to_latent(init_control_points_rgb.astype(np.float32))
        self.control_points = nn.Parameter(torch.from_numpy(latent).to(self.device))
        self.last_fraction_outside: float = 0.0

    def rgb_to_latent(self, rgb: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def latent_to_rgb(self, latent: torch.Tensor) -> torch.Tensor:
        raise NotImplementedError

    def post_step(self) -> None:
        """
        Optional hook executed after each optimizer step (e.g., latent clamping).
        """

    def setup_latent_space(self, device: torch.device) -> None:
        """
        Hook for subclasses to initialize latent-space specific resources
        (e.g., differentiable transforms) once the nn.Module base has been
        initialized.
        """

    def forward(self, num_samples: int = 256) -> torch.Tensor:
        dense_latent = resample_control_points(self.control_points, num_samples)
        rgb = self.latent_to_rgb(dense_latent)
        with torch.no_grad():
            outside = (rgb < 0.0) | (rgb > 1.0)
            self.last_fraction_outside = float(outside.float().mean().cpu().item())
        return rgb


# ============================================================
# Module A: Referee (perceptual uniformity metrics)
# ============================================================


class Referee:
    """
    Perceptual referee for colormap uniformity.
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

        使用�?UCS 模型一致的 sRGB �?线�?�?XYZ 管线，再�?
        colour-science 提供�?XYZ_to_CAM16UCS 进行变换�?
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


def compute_gamut_penalty(rgb: torch.Tensor) -> torch.Tensor:
    """
    Quadratic penalty measuring how far samples are outside [0, 1].
    """
    below = F.relu(-rgb)
    above = F.relu(rgb - 1.0)
    penalty = below.square() + above.square()
    return penalty.mean()


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

PQ_C1 = 3424.0 / 4096.0
PQ_C2 = 2413.0 / 128.0
PQ_C3 = 2392.0 / 128.0
PQ_M1 = 2610.0 / 16384.0
PQ_M2 = 2523.0 / 32.0
JZ_B = 1.15
JZ_G = 0.66
JZ_D = -0.56
JZ_D0 = 1.6295499532821566e-11

BT2020_RGB_TO_XYZ = np.array(
    [
        [0.63695805, 0.1446169, 0.16888098],
        [0.26270021, 0.67799807, 0.05930172],
        [0.0, 0.02807269, 1.06098506],
    ],
    dtype=np.float64,
)
BT2020_XYZ_TO_RGB = np.array(
    [
        [1.71665119, -0.35567078, -0.25336628],
        [-0.66668435, 1.61648124, 0.01576855],
        [0.01763986, -0.04277061, 0.94210312],
    ],
    dtype=np.float64,
)

JZ_XYZ_TO_LMS = np.array(
    [
        [0.41478972, 0.579999, 0.0146480],
        [-0.2015100, 1.120649, 0.0531008],
        [-0.0166008, 0.264800, 0.6684799],
    ],
    dtype=np.float64,
)
JZ_LMS_TO_XYZ = np.linalg.inv(JZ_XYZ_TO_LMS)
JZ_LMSP_TO_IZAZBZ = np.array(
    [
        [0.5, 0.5, 0.0],
        [3.524000, -4.066708, 0.542708],
        [0.199076, 1.096799, -1.295875],
    ],
    dtype=np.float64,
)
JZ_IZAZBZ_TO_LMSP = np.linalg.inv(JZ_LMSP_TO_IZAZBZ)


def pq_encode_np(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, None) / 10000.0
    x = np.power(x, PQ_M1)
    numerator = PQ_C1 + PQ_C2 * x
    denominator = 1.0 + PQ_C3 * x
    return np.power(numerator / denominator, PQ_M2)


def pq_decode_np(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    x = np.power(x, 1.0 / PQ_M2)
    numerator = np.clip(x - PQ_C1, 0.0, None)
    denominator = PQ_C2 - PQ_C3 * x
    ratio = np.clip(numerator / (denominator + 1e-12), 0.0, None)
    return 10000.0 * np.power(ratio, 1.0 / PQ_M1)


def pq_encode_torch(x: torch.Tensor) -> torch.Tensor:
    x = torch.clamp(x, min=0.0) / 10000.0
    x = torch.pow(x, PQ_M1)
    numerator = PQ_C1 + PQ_C2 * x
    denominator = 1.0 + PQ_C3 * x
    return torch.pow(numerator / denominator, PQ_M2)


def pq_decode_torch(x: torch.Tensor) -> torch.Tensor:
    x = torch.clamp(x, min=0.0, max=1.0)
    x = torch.pow(x, 1.0 / PQ_M2)
    numerator = torch.clamp(x - PQ_C1, min=0.0)
    denominator = PQ_C2 - PQ_C3 * x
    ratio = torch.clamp(numerator / (denominator + 1e-12), min=0.0)
    return 10000.0 * torch.pow(ratio, 1.0 / PQ_M1)


def xyz_to_jzazbz_np(xyz: np.ndarray) -> np.ndarray:
    X = xyz[..., 0]
    Y = xyz[..., 1]
    Z = xyz[..., 2]
    X_p = JZ_B * X - (JZ_B - 1.0) * Z
    Y_p = JZ_G * Y - (JZ_G - 1.0) * X
    Z_p = Z
    xyz_p = np.stack([X_p, Y_p, Z_p], axis=-1)
    lms = np.matmul(xyz_p, JZ_XYZ_TO_LMS.T)
    lms = np.clip(lms, 0.0, None)
    lms_p = pq_encode_np(lms)
    izazbz = np.matmul(lms_p, JZ_LMSP_TO_IZAZBZ.T)
    Iz = izazbz[..., 0]
    az = izazbz[..., 1]
    bz = izazbz[..., 2]
    Jz = ((1.0 + JZ_D) * Iz) / (1.0 + JZ_D * Iz) - JZ_D0
    return np.stack([Jz, az, bz], axis=-1)


def jzazbz_to_xyz_torch(jzazbz: torch.Tensor) -> torch.Tensor:
    Jz = jzazbz[..., 0]
    az = jzazbz[..., 1]
    bz = jzazbz[..., 2]
    numerator = Jz + JZ_D0
    denominator = 1.0 + JZ_D - JZ_D * (Jz + JZ_D0)
    Iz = numerator / torch.clamp(denominator, min=1e-8)
    izazbz = torch.stack([Iz, az, bz], dim=-1)
    lms_p = torch.matmul(izazbz, jzazbz.new_tensor(JZ_IZAZBZ_TO_LMSP).T)
    lms = pq_decode_torch(lms_p)
    xyz_p = torch.matmul(lms, jzazbz.new_tensor(JZ_LMS_TO_XYZ).T)
    X_p = xyz_p[..., 0]
    Y_p = xyz_p[..., 1]
    Z_p = xyz_p[..., 2]
    X = (X_p + (JZ_B - 1.0) * Z_p) / JZ_B
    Y = (Y_p + (JZ_G - 1.0) * X) / JZ_G
    return torch.stack([X, Y, Z_p], dim=-1)


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


@register_color_space("lab", "Baseline CIELAB")
class CIELABColorSpace(BaseColorSpace):
    """
    CIELAB latent optimization space.
    """

    def __init__(self, init_control_points_rgb: np.ndarray, device: str = "cpu"):
        self._referee = Referee()
        super().__init__(init_control_points_rgb, device)

    def rgb_to_latent(self, rgb: np.ndarray) -> np.ndarray:
        return self._referee.rgb_to_lab(rgb).astype(np.float32)

    def latent_to_rgb(self, latent: torch.Tensor) -> torch.Tensor:
        xyz = lab_to_xyz_torch(latent)
        rgb_linear = xyz_to_linear_srgb_torch(xyz)
        return linear_to_srgb_torch(rgb_linear)


@register_color_space("oklab", "Baseline OkLab")
class OkLabColorSpace(BaseColorSpace):
    """
    OkLab latent optimization space.
    """

    def rgb_to_latent(self, rgb: np.ndarray) -> np.ndarray:
        return rgb_to_oklab_np(rgb).astype(np.float32)

    def latent_to_rgb(self, latent: torch.Tensor) -> torch.Tensor:
        rgb_linear = oklab_to_linear_srgb_torch(latent)
        return linear_to_srgb_torch(rgb_linear)


@register_color_space("sucs", "Ours New sUCS")
class NewSUCSpace(BaseColorSpace):
    """
    Log-version New sUCS latent space.
    """

    def setup_latent_space(self, device: torch.device) -> None:
        self.sucs_model = NewSUCSModel(device=device, dtype=torch.float32)

    def rgb_to_latent(self, rgb: np.ndarray) -> np.ndarray:
        rgb_lin = srgb_to_linear_np(rgb).astype(np.float32)
        rgb_t = torch.from_numpy(rgb_lin).to(self.device)
        with torch.no_grad():
            xyz = torch.matmul(rgb_t, self.sucs_model.M_sRGB_to_XYZ.T)
            sucs = self.sucs_model.xyz_to_sucs(xyz)
        return sucs.cpu().numpy().astype(np.float32)

    def latent_to_rgb(self, latent: torch.Tensor) -> torch.Tensor:
        return self.sucs_model.sucs_to_srgb(latent)


@register_color_space("jzazbz", "JzAzBz")
class JzAzBzColorSpace(BaseColorSpace):
    """
    JzAzBz HDR-uniform latent space.
    """

    def rgb_to_latent(self, rgb: np.ndarray) -> np.ndarray:
        rgb_lin = srgb_to_linear_np(rgb)
        xyz = np.matmul(rgb_lin, _SRGB_TO_XYZ.T)
        jz = xyz_to_jzazbz_np(xyz)
        return jz.astype(np.float32)

    def latent_to_rgb(self, latent: torch.Tensor) -> torch.Tensor:
        xyz = jzazbz_to_xyz_torch(latent)
        rgb_linear = xyz_to_linear_srgb_torch(xyz)
        return linear_to_srgb_torch(rgb_linear)


@register_color_space("cam16ucs", "CAM16-UCS")
class CAM16UCSColorSpace(BaseColorSpace):
    """
    Placeholder for CAM16-UCS optimization (not yet differentiable).
    """

    supports_optimization = False

    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "CAM16-UCS is registered for evaluation only. Differentiable support "
            "requires a dedicated implementation."
        )


def _register_default_colormaps() -> None:
    defaults = [
        "rainbow",
        "jet",
        "nipy_spectral",
        "turbo",
        "gist_rainbow",
        "hsv",
        "cubehelix",
        "coolwarm",
        "bwr",
        "seismic",
        "RdBu",
        "Spectral",
        "PuOr",
        "PRGn",
        "BrBG",
        "PiYG",
        "viridis",
        "plasma",
        "inferno",
        "magma",
        "cividis",
        "RdYlBu",
        "GnBu",
        "YlOrRd",
        "gray",
    ]
    for name in defaults:
        register_colormap(name)


_register_default_colormaps()


# ============================================================
# Module C: Optimization Loop
# ============================================================


def resolve_color_spaces(selected: List[str]) -> tuple[list[str], list[str]]:
    """
    Filter and validate requested color spaces.
    """

    if not selected:
        selected = ["lab", "oklab", "sucs"]
    if any(name.lower() == "all" for name in selected):
        selected = list(COLOR_SPACE_REGISTRY.keys())
    normalized: list[str] = []
    skipped: list[str] = []
    for name in selected:
        key = name.lower()
        if key not in COLOR_SPACE_REGISTRY:
            skipped.append(name)
            continue
        if COLOR_SPACE_REGISTRY[key].supports_optimization:
            normalized.append(key)
        else:
            skipped.append(name)
    return normalized, skipped


def resolve_colormaps(selected: List[str]) -> list[str]:
    """
    Expand requested colormap names.
    """

    if not selected:
        selected = ["rainbow"]
    if any(name.lower() == "all" for name in selected):
        return list(COLORMAP_REGISTRY.keys())
    resolved: list[str] = []
    for name in selected:
        key = name.lower()
        if key in COLORMAP_REGISTRY:
            resolved.append(key)
    return resolved


def resolve_cmap_hyperparams(
    cmap_name: str,
    args,
    initial_sigma_v: float | None = None,
) -> tuple[int, float, float, dict]:
    """
    Determine control-point count, learning rate, and fidelity weight for a given colormap.
    Returns (K, lr, fidelity_weight, metadata dict).
    """

    base_K = args.K
    base_lr = args.lr
    base_fidelity = args.fidelity_weight
    metadata = {"auto_adjust": False, "sigma_v": initial_sigma_v}

    if not args.auto_adjust:
        return base_K, base_lr, base_fidelity, metadata

    sigma = initial_sigma_v
    if sigma is None:
        key = cmap_name.lower()
        sigma = BASELINE_CMAP_METRICS.get(key)

    if sigma is None or sigma > args.adjust_threshold:
        return base_K, base_lr, base_fidelity, metadata

    ratio = max(sigma / args.adjust_threshold, 1e-3)
    adjusted_lr = max(base_lr * ratio, args.hq_min_lr)
    adjusted_K = min(int(base_K / ratio), args.hq_max_K)
    adjusted_K = max(adjusted_K, base_K)
    adjusted_fidelity = max(base_fidelity * (args.adjust_threshold / sigma), args.hq_min_fidelity)

    metadata.update({"auto_adjust": True, "sigma_v": sigma})
    return adjusted_K, adjusted_lr, adjusted_fidelity, metadata


def build_initial_colormap(num_samples: int = 256, cmap_name: str = "rainbow") -> np.ndarray:
    """
    Build an initial non-uniform colormap (e.g., matplotlib's 'rainbow').
    """
    mpl_name = COLORMAP_REGISTRY.get(cmap_name, cmap_name)
    cmap = plt.get_cmap(mpl_name)
    xs = np.linspace(0.0, 1.0, num_samples)
    rgba = cmap(xs)
    rgb = rgba[:, :3]
    return rgb.astype(np.float32)


def create_models(
    color_spaces: List[str],
    control_points_rgb: np.ndarray,
    device: str | torch.device = "cpu",
) -> Dict[str, BaseColorSpace]:
    """
    Instantiate registered colour-space models.
    """

    device = torch.device(device)
    models: Dict[str, BaseColorSpace] = {}
    for name in color_spaces:
        cls = COLOR_SPACE_REGISTRY[name]
        models[name] = cls(control_points_rgb, device=device)
    return models


def run_optimization(
    num_epochs: int = 500,
    K: int = 10,
    num_samples: int = 256,
    color_spaces: List[str] | None = None,
    learning_rate: float = 1e-2,
    fidelity_weight: float = 0.0,
    adjust_metadata: dict | None = None,
    device: str | torch.device | None = None,
    ref_metric: str = "DE2000",
    val_metric: str = "CAM16-UCS",
    seed: int | None = 0,
    cmap_name: str = "rainbow",
    gamut_penalty: float = 0.0,
    control_point_jitter: float = 0.0,
):
    """
    Run joint optimization for all three models.
    """
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        device = torch.device(device)

    if seed is not None:
        torch.manual_seed(seed)
        np.random.seed(seed)

    initial_rgb = build_initial_colormap(num_samples=num_samples, cmap_name=cmap_name)
    indices = np.linspace(0, num_samples - 1, K, dtype=int)
    control_points_rgb = initial_rgb[indices]

    color_spaces = color_spaces or ["lab", "oklab", "sucs"]
    models = create_models(color_spaces=color_spaces, control_points_rgb=control_points_rgb, device=device)
    if not models:
        raise ValueError("No valid colour spaces were selected for optimization.")
    model_names = list(models.keys())

    jitter_scale = max(0.0, float(control_point_jitter))
    if jitter_scale > 0.0:
        for model in models.values():
            with torch.no_grad():
                noise = torch.randn_like(model.control_points) * jitter_scale
                model.control_points.add_(noise)


    optim_params = [{"params": model.parameters(), "lr": learning_rate} for model in models.values()]
    optimizer = torch.optim.Adam(optim_params)

    report_referee = Referee(metric=ref_metric)
    val_referee = Referee(metric=val_metric)

    history_sigma = {name: [] for name in model_names}
    history_sigma_val = {name: [] for name in model_names}
    history_vmin = {name: [] for name in model_names}
    history_loss = {name: [] for name in model_names}
    history_fraction_outside = {name: [] for name in model_names}
    history_grad_norm = {name: [] for name in model_names}

    best_sigma_val = {name: float("inf") for name in model_names}
    best_epoch = {name: 0 for name in model_names}
    best_colormaps = {}

    initial_rgb_tensor = torch.from_numpy(initial_rgb).to(device)

    for epoch in range(num_epochs):
        optimizer.zero_grad()

        for name, model in models.items():
            rgb_out = model(num_samples=num_samples)

            dense_latent = resample_control_points(model.control_points, num_samples)
            dists = torch.norm(dense_latent[1:] - dense_latent[:-1], dim=1)
            latent_loss = torch.var(dists)

            total_loss = latent_loss
            if gamut_penalty > 0.0:
                penalty = compute_gamut_penalty(rgb_out)
                total_loss = total_loss + gamut_penalty * penalty
            else:
                penalty = torch.zeros(1, device=device)

            if fidelity_weight > 0.0:
                fidelity_loss = F.mse_loss(rgb_out, initial_rgb_tensor)
                total_loss = total_loss + fidelity_weight * fidelity_loss
            else:
                fidelity_loss = torch.zeros(1, device=device)

            total_loss.backward()

            grad = model.control_points.grad
            grad_norm = float(grad.detach().norm().cpu().item()) if grad is not None else 0.0
            history_grad_norm[name].append(grad_norm)
            history_loss[name].append(float(total_loss.detach().cpu().item()))

            with torch.no_grad():
                rgb_np = rgb_out.detach().cpu().numpy()
                rgb_display = np.clip(rgb_np, 0.0, 1.0)

                metrics_report = report_referee.evaluate(rgb_display)
                metrics_val = val_referee.evaluate(rgb_display)

                history_sigma[name].append(metrics_report["sigma_v"])
                history_vmin[name].append(metrics_report["v_min"])
                history_sigma_val[name].append(metrics_val["sigma_v"])

                frac_out = getattr(model, "last_fraction_outside", float("nan"))
                history_fraction_outside[name].append(float(frac_out))

                sigma_val = metrics_val["sigma_v"]
                if sigma_val < best_sigma_val[name] - 1e-6:
                    best_sigma_val[name] = sigma_val
                    best_epoch[name] = epoch
                    best_colormaps[name] = rgb_display.copy()

        optimizer.step()

        with torch.no_grad():
            for model in models.values():
                model.post_step()

        if (epoch + 1) % 50 == 0 or epoch == 0:
            status = ", ".join(
                f"{COLOR_SPACE_DISPLAY_NAMES[name]}={history_sigma[name][-1]:.4f}"
                for name in model_names
            )
            print(f"[{cmap_name}] Epoch {epoch + 1}/{num_epochs} | report σ_v: {status}")

    last_colormaps = {}
    for name, model in models.items():
        with torch.no_grad():
            rgb = model(num_samples=num_samples).detach().cpu().numpy()
        last_colormaps[name] = np.clip(rgb, 0.0, 1.0)

    final_colormaps = {}
    for name in models.keys():
        if name in best_colormaps and len(best_colormaps[name]) > 0:
            final_colormaps[name] = best_colormaps[name]
        else:
            final_colormaps[name] = last_colormaps[name]

    convergence_iters = {}
    for name, sigma_list in history_sigma.items():
        sigma_arr = np.asarray(sigma_list, dtype=np.float64)
        initial = float(sigma_arr[0])
        final = float(sigma_arr[-1])
        if initial <= final:
            conv_iter = len(sigma_arr) - 1
        else:
            target = final + 0.1 * (initial - final)
            conv_iter = len(sigma_arr) - 1
            for i, val in enumerate(sigma_arr):
                if val <= target:
                    conv_iter = i
                    break
        convergence_iters[name] = conv_iter

    print("Convergence iterations (10% band above final report-metric σ_v):")
    for name in model_names:
        label = COLOR_SPACE_DISPLAY_NAMES.get(name, name)
        print(f"  [{cmap_name}] {label}: {convergence_iters[name]}")

    print("Best σ_v by validation metric and epochs:")
    for name in model_names:
        label = COLOR_SPACE_DISPLAY_NAMES.get(name, name)
        print(
            f"  [{cmap_name}] {label}: best_{val_metric}={best_sigma_val[name]:.4f} "
            f"at epoch={best_epoch[name]}"
        )

    best_sigma_report = {
        name: float(np.min(history_sigma[name])) if history_sigma[name] else float("inf")
        for name in models
    }

    for name, model in models.items():
        if isinstance(model, NewSUCSpace):
            sm = model.sucs_model
            try:
                lin_min, lin_max = sm.last_linear_range
                gam_min, gam_max = sm.last_gamma_range
                label = COLOR_SPACE_DISPLAY_NAMES.get(name, name)
                print(f"[{cmap_name}] {label} -> sRGB diagnostics (final epoch):")
                print(f"  linear RGB range: {lin_min:.6f} to {lin_max:.6f}")
                print(f"  gamma  RGB range: {gam_min:.6f} to {gam_max:.6f}")
                print(f"  fraction outside [0,1] (linear): {sm.last_fraction_outside_linear:.6f}")
                print(f"  fraction outside [0,1] (gamma): {sm.last_fraction_outside_gamma:.6f}")
            except Exception:
                pass

    return {
        "history_sigma": history_sigma,
        "history_sigma_val": history_sigma_val,
        "history_vmin": history_vmin,
        "history_loss": history_loss,
        "history_fraction_outside": history_fraction_outside,
        "history_grad_norm": history_grad_norm,
        "final_colormaps": final_colormaps,
        "last_colormaps": last_colormaps,
        "initial_rgb": initial_rgb,
        "convergence_iters": convergence_iters,
        "best_sigma_val": best_sigma_val,
        "best_sigma_report": best_sigma_report,
        "best_epoch": best_epoch,
        "best_colormaps": best_colormaps,
        "cmap_name": cmap_name,
        "report_metric": ref_metric,
        "val_metric": val_metric,
        "model_order": model_names,
        "adjust_metadata": adjust_metadata or {},
    }


# ============================================================
# Module D: Visualization
# ============================================================


def plot_optimization_curves(
    history_sigma: dict,
    output_path: str = "optimization_uniformity.png",
    baseline_scores: dict | None = None,
    model_order: List[str] | None = None,
    metric_name: str | None = None,
):
    """
    Plot ?_v vs. iteration for the three models (referee metric-dependent).
    Optionally overlays horizontal baseline lines for static colormaps
    (e.g., Viridis, Magma, Plasma).
    """
    fig, ax = plt.subplots(figsize=(7, 4))
    iters = range(len(next(iter(history_sigma.values()))))
    order = model_order or list(history_sigma.keys())
    for name in order:
        if name not in history_sigma:
            continue
        values = history_sigma[name]
        label = COLOR_SPACE_DISPLAY_NAMES.get(name, name)
        color = MODEL_COLOR_HINTS.get(name)
        ax.plot(iters, values, label=label, color=color)
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
                    label=f"{nice_name} (?_v={sigma:.3f})",
                )
    metric_label = (metric_name or "Referee").upper()
    ax.set_xlabel("Iteration")
    ax.set_ylabel(f"{metric_label} ?_v")
    ax.set_title(f"Colormap Uniformity ({metric_label} ?_v)")
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
    model_order: List[str] | None = None,
):
    """
    Plot 1×3 heatmaps of the global speed matrices after optimization.
    """
    names = [n for n in (model_order or list(final_colormaps.keys())) if n in final_colormaps]
    matrices = [
        compute_global_speed_matrix(final_colormaps[name], referee)
        for name in names
        if name in final_colormaps
    ]
    titles = [COLOR_SPACE_DISPLAY_NAMES.get(name, name) for name in names if name in final_colormaps]
    vmax = max(m.max() for m in matrices)

    cols = len(matrices)
    fig, axes = plt.subplots(1, cols, figsize=(4 * cols, 4), sharex=True, sharey=True)
    if cols == 1:
        axes = [axes]

    for ax, V, title in zip(axes, matrices, titles):
        im = ax.imshow(V, origin="lower", cmap="viridis", vmin=0.0, vmax=vmax)
        ax.set_title(title)
        ax.set_xlabel("j")
        ax.set_ylabel("i")

    metric = getattr(referee, "metric", "DE2000")
    if metric == "DE2000":
        speed_label = "Speed (?E_00 / |i - j|)"
    else:
        speed_label = "Speed (?E_CAM16 / |i - j|)"
    fig.colorbar(im, ax=axes.ravel().tolist(), shrink=0.8, label=speed_label)

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
    model_order: List[str] | None = None,
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
    order = model_order or list(final_colormaps.keys())
    for name in order:
        if name not in final_colormaps:
            continue
        rgb = final_colormaps[name]
        lab = referee.rgb_to_lab(rgb)
        ax.plot(
            lab[:, 0],
            lab[:, 1],
            lab[:, 2],
            color=MODEL_COLOR_HINTS.get(name),
            linewidth=2.0,
            label=COLOR_SPACE_DISPLAY_NAMES.get(name, name),
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
    model_order: List[str] | None = None,
):
    """
    Plot colormap strips together with local speed plots for
    initial, CIELAB, OkLab, and New sUCS colormaps.

    Args:
        colormaps: dict with keys 'initial', 'lab', 'oklab', 'sucs';
                   values are (N, 3) numpy arrays in [0, 1].
    """
    names = ["initial"]
    if model_order:
        names.extend([n for n in model_order if n in colormaps])
    titles = {"initial": "Initial"}
    for name in names:
        if name == "initial":
            continue
        titles[name] = COLOR_SPACE_DISPLAY_NAMES.get(name, name)

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
    model_order: List[str] | None = None,
):
    """
    Plot gradient norm dynamics and boundary contact rate over iterations
    for the three models.
    """
    iters = range(len(next(iter(history_grad_norm.values()))))

    fig, axes = plt.subplots(2, 1, figsize=(7, 6), sharex=True)

    order = model_order or list(history_grad_norm.keys())

    for name in order:
        if name not in history_grad_norm:
            continue
        color = MODEL_COLOR_HINTS.get(name)
        label = COLOR_SPACE_DISPLAY_NAMES.get(name, name)
        axes[0].plot(iters, history_grad_norm[name], color=color, label=label)
        axes[1].plot(
            iters, history_fraction_outside[name], color=color, label=label
        )

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
    model_order: List[str] | None = None,
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

    names = model_order or list(final_colormaps.keys())
    titles = {name: COLOR_SPACE_DISPLAY_NAMES.get(name, name) for name in names}

    fig, axes = plt.subplots(
        len(fields),
        len(names),
        figsize=(4 * len(names), 3 * len(fields)),
        squeeze=False,
    )

    for i, field in enumerate(fields):
        for j, name in enumerate(names):
            if name not in final_colormaps:
                continue
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
    color_spaces: List[str],
    learning_rate: float,
    fidelity_weight: float,
    device: str | torch.device,
    ref_metric: str = "DE2000",
    val_metric: str = "CAM16-UCS",
    cmap_name: str = "rainbow",
    gamut_penalty: float = 0.0,
    control_point_jitter: float = 0.0,
) -> dict:
    """
    Run the optimization for multiple random seeds and collect the best
    referee sigma_v for each model.
    """
    best_sigmas_per_model = {name: [] for name in color_spaces}

    for seed in range(num_seeds):
        print(f"[Multi-seed] Running seed {seed + 1}/{num_seeds}")
        results = run_optimization(
            num_epochs=num_epochs,
            K=K,
            num_samples=num_samples,
            color_spaces=color_spaces,
            learning_rate=learning_rate,
            fidelity_weight=fidelity_weight,
            device=device,
            ref_metric=ref_metric,
            val_metric=val_metric,
            seed=seed,
            cmap_name=cmap_name,
            gamut_penalty=gamut_penalty,
            control_point_jitter=control_point_jitter,
        )
        for name in best_sigmas_per_model:
            if name in results["best_sigma_val"]:
                best_sigmas_per_model[name].append(results["best_sigma_val"][name])

    return best_sigmas_per_model


def plot_robustness_boxplot(
    best_sigmas_per_model: dict,
    output_path: str = "robustness_boxplot.png",
    metric_name: str = "CIEDE2000",
    model_order: List[str] | None = None,
):
    """
    Plot a boxplot of best sigma_v over multiple seeds for each model.
    """
    order = model_order or list(best_sigmas_per_model.keys())
    labels: List[str] = []
    data: List[list[float]] = []
    filtered_order: List[str] = []
    for name in order:
        if name not in best_sigmas_per_model:
            continue
        filtered_order.append(name)
        labels.append(COLOR_SPACE_DISPLAY_NAMES.get(name, name))
        data.append(best_sigmas_per_model[name])

    fig, ax = plt.subplots(figsize=(6, 4))
    bp = ax.boxplot(data, labels=labels, patch_artist=True)

    for patch, name in zip(bp["boxes"], filtered_order):
        color = MODEL_COLOR_HINTS.get(name)
        if color:
            patch.set_facecolor(color)
        patch.set_alpha(0.4)

    ax.set_ylabel(f"Best σ_v ({metric_name})")
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
    model_order: List[str] | None = None,
    metric_name: str | None = None,
):
    """
    Plot optimization curves together with horizontal baseline lines for
    standard Matplotlib colormaps (Viridis, Magma, Plasma).
    """
    plot_optimization_curves(
        history_sigma=history_sigma,
        output_path=output_path,
        baseline_scores=baseline_scores,
        model_order=model_order,
        metric_name=metric_name,
    )
def main():
    parser = argparse.ArgumentParser(description="Colormap optimization testbed for New sUCS vs. CIELAB and OkLab")
    parser.add_argument("--epochs", type=int, default=500, help="Number of optimization epochs per run")
    parser.add_argument("--K", type=int, default=10, help="Number of control points (baseline setting)")
    parser.add_argument("--num-samples", type=int, default=256, help="Number of samples in each colormap strip")
    parser.add_argument(
        "--lr",
        type=float,
        default=1e-2,
        help="Base learning rate for all color spaces",
    )
    parser.add_argument(
        "--ref-metric",
        type=str,
        default="CAM16-UCS",
        choices=["DE2000", "CAM16-UCS"],
        help="Referee metric for local uniformity",
    )
    parser.add_argument(
        "--val-metric",
        type=str,
        default="CAM16-UCS",
        choices=["DE2000", "CAM16-UCS"],
        help="Metric used for early stopping / best-checkpoint selection",
    )
    parser.add_argument(
        "--multi-seed",
        type=int,
        default=0,
        help="Number of random seeds for robustness test (0 disables multi-seed run)",
    )
    parser.add_argument(
        "--init-cmaps",
        type=str,
        nargs="+",
        default=["rainbow"],
        help="List of Matplotlib colormaps used as initialization (use 'all' for registry defaults)",
    )
    parser.add_argument(
        "--color-spaces",
        type=str,
        nargs="+",
        default=["lab", "oklab", "sucs", "jzazbz"],
        help="Colour spaces to optimize (use 'all' to include every registered differentiable UCS)",
    )
    parser.add_argument(
        "--gamut-penalty",
        type=float,
        default=0.0,
        help="Weight for the quadratic penalty on RGB samples outside [0, 1]",
    )
    parser.add_argument(
        "--fidelity-weight",
        type=float,
        default=0.0,
        help="Weight for the RGB fidelity term that keeps the optimized map close to the initialization",
    )
    parser.add_argument(
        "--auto-adjust",
        action="store_true",
        help="Enable data-driven hyper-parameter adjustment based on initial sigma_v",
    )
    parser.add_argument(
        "--adjust-threshold",
        type=float,
        default=0.10,
        help="Sigma_v threshold that triggers the fidelity-friendly hyper-parameters",
    )
    parser.add_argument(
        "--hq-min-lr",
        type=float,
        default=1e-3,
        help="Lower bound for auto-adjusted learning rate",
    )
    parser.add_argument(
        "--hq-max-K",
        type=int,
        default=32,
        help="Maximum control point count when auto adjustment is triggered",
    )
    parser.add_argument(
        "--hq-min-fidelity",
        type=float,
        default=0.05,
        help="Minimum fidelity weight when auto adjustment is triggered",
    )

    parser.add_argument(
        "--init-jitter",
        type=float,
        default=0.02,
        help="Stddev of Gaussian noise applied to latent control points at initialization",
    )

    args = parser.parse_args()

    if args.ref_metric.upper() != "CAM16-UCS" or args.val_metric.upper() != "CAM16-UCS":
        print("[config] Forcing CAM16-UCS for referee/validation metrics to keep evaluation consistent.")
    args.ref_metric = "CAM16-UCS"
    args.val_metric = "CAM16-UCS"
    if args.init_jitter < 0.0:
        args.init_jitter = 0.0

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device}")

    color_spaces, skipped_spaces = resolve_color_spaces(args.color_spaces)
    if skipped_spaces:
        print(f"Skipping unsupported or non-optimizable color spaces: {', '.join(skipped_spaces)}")
    if not color_spaces:
        raise ValueError("No valid color spaces selected.")

    multiple_cmaps = resolve_colormaps(args.init_cmaps)
    if not multiple_cmaps:
        raise ValueError("No valid initialization colormaps were resolved.")

    for cmap_name in multiple_cmaps:
        print("=" * 60)
        print(f"Running optimization for initialization cmap: {cmap_name}")
        initial_evaluation: float | None = None
        print("  Computing initial sigma_v for auto-adjustment...")
        initial_rgb = build_initial_colormap(num_samples=args.num_samples, cmap_name=cmap_name)
        referee_for_init = Referee(metric=args.ref_metric)
        metrics = referee_for_init.evaluate(initial_rgb)
        initial_evaluation = metrics["sigma_v"]
        print(f"  Initial sigma_v ({args.ref_metric}): {initial_evaluation:.4f}")

        local_K, local_lr, local_fidelity, adjust_meta = resolve_cmap_hyperparams(
            cmap_name, args, initial_sigma_v=initial_evaluation
        )
        print(
            f"  -> hyper-parameters: K={local_K}, lr={local_lr}, fidelity_weight={local_fidelity}"
        )
        if adjust_meta.get("auto_adjust"):
            print(
                f"     auto-adjust triggered (sigma_v={adjust_meta['sigma_v']:.4f} <= {args.adjust_threshold})"
            )
        else:
            print("     auto-adjust inactive (threshold not met or disabled)")
        results = run_optimization(
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

        referee = Referee(metric=args.ref_metric)

        baseline_scores = {
            "viridis": compute_referee_sigma_for_cmap("viridis", args.num_samples, referee),
            "magma": compute_referee_sigma_for_cmap("magma", args.num_samples, referee),
            "plasma": compute_referee_sigma_for_cmap("plasma", args.num_samples, referee),
        }

        suffix = f"{cmap_name}_" if len(multiple_cmaps) > 1 else ""
        def out(name: str) -> str:
            return f"{suffix}{name}"

        model_order = results["model_order"]
        plot_optimization_curves(
            results["history_sigma"],
            output_path=out("optimization_uniformity.png"),
            model_order=model_order,
            metric_name=args.ref_metric,
        )
        plot_global_speed_matrices(
            results["final_colormaps"],
            referee,
            output_path=out("global_speed_matrix.png"),
            model_order=model_order,
        )
        plot_gamut_trajectory(
            results["final_colormaps"],
            referee,
            output_path=out("gamut_trajectory_lab.png"),
            model_order=model_order,
        )

        colormap_dict = dict(results["final_colormaps"])
        colormap_dict["initial"] = results["initial_rgb"]
        plot_colormap_strips(
            colormap_dict,
            referee,
            output_path=out("colormap_strips_comparison.png"),
            model_order=model_order,
        )
        plot_gradient_dynamics(
            results["history_grad_norm"],
            results["history_fraction_outside"],
            output_path=out("gradient_norm_dynamics.png"),
            model_order=model_order,
        )
        plot_mach_band_tests(
            results["final_colormaps"],
            output_path=out("mach_band_test.png"),
            model_order=model_order,
        )
        plot_benchmark_comparison(
            results["history_sigma"],
            baseline_scores,
            output_path=out("benchmark_comparison.png"),
            model_order=model_order,
            metric_name=args.ref_metric,
        )

        if args.multi_seed > 0:
            best_sigmas_per_model = run_multi_seed(
                num_seeds=args.multi_seed,
                num_epochs=args.epochs,
                K=local_K,
                num_samples=args.num_samples,
                color_spaces=model_order,
                learning_rate=local_lr,
                fidelity_weight=local_fidelity,
                device=device,
                ref_metric=args.ref_metric,
                val_metric=args.val_metric,
                cmap_name=cmap_name,
                gamut_penalty=args.gamut_penalty,
                control_point_jitter=args.init_jitter,
            )
            plot_robustness_boxplot(
                best_sigmas_per_model,
                output_path=out("robustness_boxplot.png"),
                metric_name=args.val_metric,
                model_order=model_order,
            )

        print("Saved figures for cmap", cmap_name)
        print(f"  - {out('optimization_uniformity.png')}")
        print(f"  - {out('global_speed_matrix.png')}")
        print(f"  - {out('gamut_trajectory_lab.png')}")
        print(f"  - {out('colormap_strips_comparison.png')}")
        print(f"  - {out('gradient_norm_dynamics.png')}")
        print(f"  - {out('mach_band_test.png')}")
        print(f"  - {out('benchmark_comparison.png')}")
        if args.multi_seed > 0:
            print(f"  - {out('robustness_boxplot.png')}")


if __name__ == "__main__":
    main()
