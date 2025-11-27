import math
from pathlib import Path
from typing import Callable, Dict, List, Sequence, Tuple

import matplotlib.pyplot as plt
import numpy as np
import torch
from colour import CCS_ILLUMINANTS, xyY_to_XYZ, xy_to_XYZ
from colour.adaptation import chromatic_adaptation
from colour.notation.datasets.munsell import MUNSELL_COLOURS


class LocalSUCS:
    """
    Re-implementation of New sUCS with 0-1 Jab scaling for tone mapping.
    """

    def __init__(self, device: str = "cpu", dtype: torch.dtype = torch.float32) -> None:
        self.device = torch.device(device)
        self.dtype = dtype

        self.gamma = torch.tensor(0.7174, device=self.device, dtype=self.dtype)
        self.sigma = torch.tensor(0.6464, device=self.device, dtype=self.dtype)
        self.t1 = torch.tensor(0.595784, device=self.device, dtype=self.dtype)
        self.t2 = torch.tensor(0.398886, device=self.device, dtype=self.dtype)

        m_raw_np = np.array(
            [
                [0.4002, 0.7075, -0.0807],
                [-0.2280, 1.1500, 0.0612],
                [0.0000, 0.0000, 0.9184],
            ]
        )
        row_sums = m_raw_np.sum(axis=1, keepdims=True)
        m_hpe_norm = m_raw_np / row_sums

        w_l = np.array([2.0, 1.0, 0.05])
        row_i = w_l / w_l.sum()
        rgyb_fixed = np.array(
            [
                [4.30, -4.70, 0.40],
                [0.49, 0.49, -0.98],
            ]
        )
        m_iab_np = np.vstack([row_i, rgyb_fixed])

        self.M_HPE = torch.tensor(m_hpe_norm, device=self.device, dtype=self.dtype)
        self.M_Iab = torch.tensor(m_iab_np, device=self.device, dtype=self.dtype)
        self.M_HPE_inv = torch.inverse(self.M_HPE)
        self.M_Iab_inv = torch.inverse(self.M_Iab)

        self.gain = torch.tensor(1.0 + float(self.sigma**self.gamma), device=self.device, dtype=self.dtype)

        srgb_to_xyz_np = np.array(
            [
                [0.4124564, 0.3575761, 0.1804375],
                [0.2126729, 0.7151522, 0.0721750],
                [0.0193339, 0.1191920, 0.9503041],
            ]
        )
        self.M_sRGB_to_XYZ = torch.tensor(srgb_to_xyz_np, device=self.device, dtype=self.dtype)
        self.M_XYZ_to_sRGB = torch.tensor(np.linalg.inv(srgb_to_xyz_np), device=self.device, dtype=self.dtype)
        xyz_white = srgb_to_xyz_np @ np.array([1.0, 1.0, 1.0])
        self.XYZ_white_from_unit_rgb = torch.tensor(xyz_white, device=self.device, dtype=self.dtype)

    def xyz_to_sucs(self, xyz: torch.Tensor) -> torch.Tensor:
        xyz_norm = xyz / self.XYZ_white_from_unit_rgb
        lms = torch.matmul(xyz_norm, self.M_HPE.T)

        eps = 1e-10
        lms_abs = torch.abs(lms)
        lms_sign = torch.sign(lms)
        v_g = torch.pow(lms_abs, self.gamma)
        s_g = torch.pow(self.sigma, self.gamma)
        response = (v_g / (v_g + s_g + eps)) * lms_sign
        lms_prime = response * self.gain

        iab_lin = torch.matmul(lms_prime, self.M_Iab.T)
        I = iab_lin[:, 0:1]
        a_lin = iab_lin[:, 1:2]
        b_lin = iab_lin[:, 2:3]

        C_lin = torch.sqrt(a_lin**2 + b_lin**2 + eps)
        C_out = self.t1 * torch.log1p(C_lin / self.t2)
        scale = C_out / (C_lin + eps)
        a_out = a_lin * scale
        b_out = b_lin * scale
        return torch.cat([I, a_out, b_out], dim=1)

    def sucs_to_xyz(self, sucs: torch.Tensor) -> torch.Tensor:
        J = sucs[:, 0:1]
        a_prime = sucs[:, 1:2]
        b_prime = sucs[:, 2:3]

        eps = 1e-10
        C_out = torch.sqrt(a_prime**2 + b_prime**2 + eps)
        C_lin = self.t2 * (torch.exp(C_out / self.t1) - 1.0)
        scale = C_lin / (C_out + eps)
        a_lin = a_prime * scale
        b_lin = b_prime * scale
        iab_lin = torch.cat([J, a_lin, b_lin], dim=1)

        lms_prime = torch.matmul(iab_lin, self.M_Iab_inv.T)
        response = lms_prime / self.gain
        res_abs = torch.abs(response)
        res_sign = torch.sign(response)
        res_abs = torch.clamp(res_abs, 0.0, 0.9999)
        s_g = torch.pow(self.sigma, self.gamma)
        numerator = res_abs * s_g
        denominator = 1.0 - res_abs
        v_g = numerator / (denominator + eps)
        v = torch.pow(v_g, 1.0 / self.gamma)
        lms = v * res_sign

        xyz_norm = torch.matmul(lms, self.M_HPE_inv.T)
        xyz = xyz_norm * self.XYZ_white_from_unit_rgb
        return xyz


S_UCS_MODEL = LocalSUCS()


def compute_luminance(rgb: np.ndarray) -> np.ndarray:
    """Return scene-referred luminance using Rec.709 primaries."""
    weights = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    return np.tensordot(rgb, weights, axes=([-1], [0]))


def safe_color_ratio(rgb: np.ndarray, luminance: np.ndarray, eps: float = 1e-6) -> np.ndarray:
    """Normalize RGB by luminance with a small epsilon."""
    return rgb / (luminance[..., None] + eps)


def global_reinhard_tmo(rgb: np.ndarray, key: float = 0.18, white: float = 6.0) -> np.ndarray:
    """
    Classic global Reinhard tone mapping operator.

    Args:
        rgb: scene-referred linear RGB (float32, arbitrary range)
        key: overall brightness adaptation parameter (typ. 0.18)
        white: white point to anchor highlights (scene-relative)
    """
    luminance = compute_luminance(rgb)
    log_avg = math.exp(np.mean(np.log(np.maximum(luminance, 1e-6))))
    scaled = (key / log_avg) * luminance

    if white is not None:
        white2 = white * white
        mapped_lum = (scaled * (1.0 + scaled / white2)) / (1.0 + scaled)
    else:
        mapped_lum = scaled / (1.0 + scaled)

    chroma = safe_color_ratio(rgb, luminance)
    out = chroma * mapped_lum[..., None]
    return np.clip(out, 0.0, 1.0)


XYZ_TO_SRGB = np.array(
    [
        [3.2404542, -1.5371385, -0.4985314],
        [-0.9692660, 1.8760108, 0.0415560],
        [0.0556434, -0.2040259, 1.0572252],
    ],
    dtype=np.float32,
)
WHITEPOINT_C = xy_to_XYZ(CCS_ILLUMINANTS["CIE 1931 2 Degree Standard Observer"]["C"]).astype(np.float32)
WHITEPOINT_D65 = xy_to_XYZ(CCS_ILLUMINANTS["CIE 1931 2 Degree Standard Observer"]["D65"]).astype(np.float32)
HUE_BASE_ORDER: List[str] = ["R", "YR", "Y", "GY", "G", "BG", "B", "PB", "P", "RP"]


def munsell_xyY_to_linear_srgb(xyY: Sequence[float]) -> np.ndarray:
    """
    Convert Munsell xyY (Illuminant C) samples to scene-linear sRGB (D65).

    The renotation data assumes Illuminant C and is scaled by 1/0.975. We undo
    the scaling to match the ASTM tables, adapt to D65, and convert to sRGB.
    """
    xyY_arr = np.array([xyY[0], xyY[1], xyY[2] * 0.975], dtype=np.float32)
    XYZ_C = xyY_to_XYZ(xyY_arr)
    XYZ_D65 = chromatic_adaptation(XYZ_C, WHITEPOINT_C, WHITEPOINT_D65, method="Von Kries")
    linear_rgb = np.matmul(XYZ_D65, XYZ_TO_SRGB.T)
    return linear_rgb.astype(np.float32)


def _hue_sort_key(hue: str) -> Tuple[int, float]:
    """Order Munsell hues along the standard 40-hue circle."""
    if hue.startswith("N"):
        return (-1, 0.0)

    idx = 0
    while idx < len(hue) and (hue[idx].isdigit() or hue[idx] == "."):
        idx += 1
    number = float(hue[:idx]) if idx > 0 else 0.0
    base = hue[idx:]
    base_idx = HUE_BASE_ORDER.index(base)
    return (base_idx, number)


def build_munsell_value_plane(target_value: float = 5.0, tile: int = 18) -> np.ndarray:
    """
    Arrange the Munsell Renotation data (value slice) as a patch mosaic.
    """
    samples: List[Tuple[str, float, np.ndarray]] = []
    for spec, xyY in MUNSELL_COLOURS["real"]:
        hue, value, chroma = spec
        if hue == "N":
            continue  # skip neutrals for this plane
        if abs(value - target_value) > 1e-6:
            continue
        rgb = np.clip(munsell_xyY_to_linear_srgb(xyY), 0.0, None)
        samples.append((hue, chroma, rgb))

    if not samples:
        raise RuntimeError(f"No Munsell entries found for value {target_value}")

    hues = sorted({h for h, _, _ in samples}, key=_hue_sort_key)
    chromas = sorted({c for _, c, _ in samples})

    mosaic = np.zeros((len(hues) * tile, len(chromas) * tile, 3), dtype=np.float32)
    filler = np.full(3, 0.02, dtype=np.float32)

    for row, hue in enumerate(hues):
        for col, chroma in enumerate(chromas):
            patch = next((rgb for h, c, rgb in samples if h == hue and c == chroma), None)
            block = patch if patch is not None else filler
            r0, r1 = row * tile, (row + 1) * tile
            c0, c1 = col * tile, (col + 1) * tile
            mosaic[r0:r1, c0:c1, :] = block

    # Slight gain to emphasize the HDR nature of the dataset when tone mapping.
    return mosaic


def sucs_pipeline_tmo(
    rgb: np.ndarray,
    key: float = 0.5,
    white: float | None = None,
    chroma_strength: float = 0.0,
) -> np.ndarray:
    """
    Tone-map in the sUCS latent space: RGB->XYZ->sUCS, compress J, map back.
    """
    h, w, _ = rgb.shape
    flat = torch.from_numpy(rgb.reshape(-1, 3)).to(S_UCS_MODEL.device, dtype=S_UCS_MODEL.M_sRGB_to_XYZ.dtype)

    with torch.no_grad():
        xyz = torch.matmul(flat, S_UCS_MODEL.M_sRGB_to_XYZ.T)
        sucs = S_UCS_MODEL.xyz_to_sucs(xyz)
        J = sucs[:, :1]
        ab = sucs[:, 1:]

        eps = 1e-6
        J_safe = torch.clamp(J, min=eps)
        log_avg = torch.exp(torch.mean(torch.log(J_safe)))
        scaled = (key / log_avg) * J_safe

        if white is None:
            white_level = torch.quantile(J_safe, 0.99)
            white_level = torch.clamp(white_level, min=0.3)
            white_ref = float(white_level.item())
        else:
            white_ref = max(float(white), 1e-3)

        mapped_J = torch.log1p(scaled) / math.log1p(white_ref)
        mapped_J = torch.clamp(mapped_J, min=0.0, max=1.0)

        c_ratio = torch.clamp(mapped_J / (J_safe + eps), 0.0, 1.0)
        # chroma_gain = 1.0 - chroma_strength * (1.0 - c_ratio)
        chroma_gain = 1
        ab_mapped = ab * chroma_gain

        sucs_mapped = torch.cat([mapped_J, ab_mapped], dim=1)
        xyz_mapped = S_UCS_MODEL.sucs_to_xyz(sucs_mapped)
        rgb_linear = torch.matmul(xyz_mapped, S_UCS_MODEL.M_XYZ_to_sRGB.T)

    mapped = rgb_linear.detach().cpu().numpy().reshape(h, w, 3)
    return np.clip(mapped, 0.0, 1.0)


def synthesize_scenes() -> Dict[str, np.ndarray]:
    """Generate a few HDR test scenes procedurally."""
    scenes: Dict[str, np.ndarray] = {}

    # 1. Horizontal gradient with HDR sun.
    h, w = 256, 512
    xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
    gradient = np.tile(xs, (h, 1))
    base = np.stack(
        [
            0.05 + 0.9 * gradient,
            0.08 + 1.0 * gradient,
            0.1 + 1.6 * gradient,
        ],
        axis=-1,
    )
    # Add a bright sun disk (~30 000 nits equivalent).
    yy, xx = np.mgrid[0:h, 0:w]
    sun = np.exp(-(((xx - 0.7 * w) ** 2 + (yy - 0.35 * h) ** 2) / (2 * (0.07 * w) ** 2)))
    sky = base + sun[..., None] * np.array([70.0, 55.0, 25.0], dtype=np.float32)
    scenes["sunset_gradient"] = sky.astype(np.float32)

    # 2. Indoor room with bright window.
    room = np.zeros((h, w, 3), dtype=np.float32)
    room[..., :] = np.array([0.02, 0.015, 0.01], dtype=np.float32)  # dim interior
    room[:, :, 0] += 0.01 * np.sin(xs * 10)[None, :]
    room[:, :, 1] += 0.015 * np.sin(xs * 7)[None, :]
    window = np.zeros_like(room)
    window[:, int(0.4 * w) : int(0.6 * w), :] = np.array([15.0, 18.0, 20.0], dtype=np.float32)
    room += window
    scenes["interior_window"] = room

    # 3. Specular checkered surface.
    stripes = (np.sign(np.sin(xs * 30.0)) + 1.0) / 2.0
    stripes = np.tile(stripes[None, :], (h, 1))
    base = 0.2 + 0.3 * stripes
    base = base[..., None]
    specular = 10.0 * np.exp(-(((xx - 0.5 * w) ** 2 + (yy - 0.5 * h) ** 2) / (2 * (0.05 * w) ** 2)))
    specular = specular[..., None]
    checker = np.concatenate(
        [
            base * (1.0 + 0.5 * specular),
            0.8 * base * (1.0 + 0.7 * specular),
            0.5 * base * (1.0 + specular),
        ],
        axis=-1,
    )
    scenes["specular_checker"] = checker.astype(np.float32)

    # 4. Munsell value-5 plane from the Munsell Color Science Lab data.
    scenes["munsell_value5"] = build_munsell_value_plane()

    return scenes


def preview_hdr(rgb: np.ndarray) -> np.ndarray:
    """Naive clipped preview to show the unclamped HDR content."""
    luminance = compute_luminance(rgb)
    white = np.percentile(luminance, 99.7)
    if white <= 0:
        white = luminance.max() + 1e-3
    scaled = rgb / max(white, 1e-3)
    return np.clip(scaled, 0.0, 1.0)


def save_comparisons(
    scenes: Dict[str, np.ndarray],
    tmfns: Dict[str, Callable[[np.ndarray], np.ndarray]],
    output_dir: Path,
) -> None:
    """Apply tone mappers and create multi-panel figures."""
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, hdr in scenes.items():
        fig, axes = plt.subplots(1, len(tmfns) + 1, figsize=(4 * (len(tmfns) + 1), 3))
        ax = axes[0]
        ax.imshow(preview_hdr(hdr))
        ax.set_title("HDR preview (clipped)")
        ax.axis("off")

        for ax, (label, fn) in zip(axes[1:], tmfns.items()):
            mapped = fn(hdr)
            ax.imshow(mapped)
            ax.set_title(label)
            ax.axis("off")

        fig.suptitle(f"TMO comparison: {name}")
        fig.tight_layout()
        out_path = output_dir / f"{name}_tmo_compare.png"
        fig.savefig(out_path, dpi=150)
        plt.close(fig)


def main() -> Tuple[Path, Dict[str, np.ndarray]]:
    scenes = synthesize_scenes()
    workspace = Path(__file__).resolve().parent
    output_dir = workspace / "outputs"

    tone_mappers: Dict[str, Callable[[np.ndarray], np.ndarray]] = {
        "sUCS pipeline": sucs_pipeline_tmo,
        "Reinhard global": global_reinhard_tmo,
    }
    save_comparisons(scenes, tone_mappers, output_dir)

    return output_dir, scenes


if __name__ == "__main__":
    output_dir, scenes = main()
    print(f"Generated comparisons for {len(scenes)} scenes in {output_dir}")
