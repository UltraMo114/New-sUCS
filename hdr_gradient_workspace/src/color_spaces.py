from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import torch

from Model import NewSUCS as NewSUCSModel
from sucs import (
    JZ_B,
    JZ_D,
    JZ_D0,
    JZ_G,
    JZ_IZAZBZ_TO_LMSP,
    JZ_LMSP_TO_IZAZBZ,
    JZ_LMS_TO_XYZ,
    JZ_XYZ_TO_LMS,
    pq_decode_torch,
    pq_encode_torch,
)
from colour.models.rgb.ictcp import (
    MATRIX_ICTCP_ICTCP_TO_LMS_P,
    MATRIX_ICTCP_LMS_P_TO_ICTCP,
    MATRIX_ICTCP_RGB_TO_LMS,
)

SDR_WHITE_LUMINANCE = 100.0
TORCH_DTYPE = torch.float64

BT2020_RGB_TO_XYZ = np.array(
    [
        [0.6369580, 0.1446169, 0.1688809],
        [0.2627002, 0.6779981, 0.0593017],
        [0.0000000, 0.0280727, 1.0609851],
    ],
    dtype=np.float64,
)
BT2020_XYZ_TO_RGB = np.linalg.inv(BT2020_RGB_TO_XYZ)

REC709_RGB_TO_XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ],
    dtype=np.float64,
)
REC709_XYZ_TO_RGB = np.linalg.inv(REC709_RGB_TO_XYZ)


def bt2020_to_xyz_np(rgb: np.ndarray, luminance_nits: float) -> np.ndarray:
    """
    Convert BT.2020 linear RGB to absolute XYZ given target luminance.
    """

    rgb = np.asarray(rgb, dtype=np.float64)
    xyz_rel = rgb @ BT2020_RGB_TO_XYZ.T
    return xyz_rel * luminance_nits


def bt2020_to_xyz_torch(
    rgb: torch.Tensor, luminance_nits: float, assume_linear: bool = True
) -> torch.Tensor:
    """
    Torch helper mirroring :func:`bt2020_to_xyz_np`.
    """

    mat = rgb.new_tensor(BT2020_RGB_TO_XYZ, dtype=TORCH_DTYPE)
    xyz_rel = torch.matmul(rgb.to(TORCH_DTYPE), mat.T)
    return xyz_rel * luminance_nits


def xyz_to_bt2020_rgb_torch(xyz: torch.Tensor, luminance_nits: float) -> torch.Tensor:
    """
    Convert XYZ back to BT.2020 linear RGB coordinates.
    """

    mat = xyz.new_tensor(BT2020_XYZ_TO_RGB, dtype=TORCH_DTYPE)
    xyz_rel = xyz / luminance_nits
    return torch.matmul(xyz_rel, mat.T)


def rec709_rgb_to_xyz_torch(rgb: torch.Tensor) -> torch.Tensor:
    """
    Convert Rec.709 linear RGB (0-1) into XYZ scaled to SDR white (100 nits).
    """

    mat = rgb.new_tensor(REC709_RGB_TO_XYZ, dtype=TORCH_DTYPE)
    xyz_rel = torch.matmul(rgb.to(TORCH_DTYPE), mat.T)
    return xyz_rel * SDR_WHITE_LUMINANCE


def xyz_to_rec709_rgb_torch(xyz: torch.Tensor) -> torch.Tensor:
    """
    Convert absolute XYZ (cd/m^2) to Rec.709 linear RGB representation.
    """

    mat = xyz.new_tensor(REC709_XYZ_TO_RGB, dtype=TORCH_DTYPE)
    xyz_rel = xyz / SDR_WHITE_LUMINANCE
    return torch.matmul(xyz_rel, mat.T)


def cam16ucs_distance(
    xyz_a: torch.Tensor, xyz_b: torch.Tensor, device: str | torch.device = "cpu"
) -> torch.Tensor:
    """
    Convenience CAM16-UCS ΔE distance for diagnostics (non-differentiable).
    """

    import colour

    a = xyz_a.detach().cpu().numpy()
    b = xyz_b.detach().cpu().numpy()
    cam_a = colour.XYZ_to_CAM16UCS(a)
    cam_b = colour.XYZ_to_CAM16UCS(b)
    diff = cam_a - cam_b
    delta = np.linalg.norm(diff, axis=-1)
    return torch.tensor(delta, dtype=TORCH_DTYPE, device=device)


@dataclass
class ColorSpaceAdapter:
    """
    Shared base for differentiable colour spaces used in HDR tests.
    """

    name: Literal["sucs", "jzazbz", "ictcp"]
    display_name: str
    device: torch.device

    def xyz_to_coords(self, xyz: torch.Tensor) -> torch.Tensor:
        raise NotImplementedError

    def coords_to_xyz(self, coords: torch.Tensor) -> torch.Tensor:
        raise NotImplementedError

    def hue_angle(self, coords: torch.Tensor) -> torch.Tensor:
        return torch.atan2(coords[..., 2], coords[..., 1])


class SUCSAdapter(ColorSpaceAdapter):
    """
    Differentiable wrapper around the New sUCS torch module.
    """

    def __init__(self, device: str | torch.device = "cpu"):
        device = torch.device(device)
        super().__init__(name="sucs", display_name="sUCS", device=device)
        self.model = NewSUCSModel(device=device, dtype=TORCH_DTYPE)

    def xyz_to_coords(self, xyz: torch.Tensor) -> torch.Tensor:
        return self.model.xyz_to_sucs(xyz.to(TORCH_DTYPE))

    def coords_to_xyz(self, coords: torch.Tensor) -> torch.Tensor:
        return self.model.sucs_to_xyz(coords.to(TORCH_DTYPE))


class JzAzBzAdapter(ColorSpaceAdapter):
    """
    Fully differentiable implementation of JzAzBz via PQ pipeline.
    """

    def __init__(self, device: str | torch.device = "cpu"):
        device = torch.device(device)
        super().__init__(name="jzazbz", display_name="JzAzBz", device=device)
        self.xyz_to_lms = torch.tensor(JZ_XYZ_TO_LMS, dtype=TORCH_DTYPE, device=device)
        self.lms_to_xyz = torch.tensor(JZ_LMS_TO_XYZ, dtype=TORCH_DTYPE, device=device)
        self.lmsp_to_izazbz = torch.tensor(
            JZ_LMSP_TO_IZAZBZ, dtype=TORCH_DTYPE, device=device
        )
        self.izazbz_to_lmsp = torch.tensor(
            JZ_IZAZBZ_TO_LMSP, dtype=TORCH_DTYPE, device=device
        )

    def xyz_to_coords(self, xyz: torch.Tensor) -> torch.Tensor:
        xyz = xyz.to(TORCH_DTYPE)
        x = xyz[..., 0]
        y = xyz[..., 1]
        z = xyz[..., 2]
        x_p = JZ_B * x - (JZ_B - 1.0) * z
        y_p = JZ_G * y - (JZ_G - 1.0) * x
        z_p = z
        xyz_p = torch.stack([x_p, y_p, z_p], dim=-1)
        lms = torch.matmul(xyz_p, self.xyz_to_lms.T)
        lms = torch.clamp(lms, min=0.0)
        lms_p = pq_encode_torch(lms)
        izazbz = torch.matmul(lms_p, self.lmsp_to_izazbz.T)
        iz = izazbz[..., 0]
        az = izazbz[..., 1]
        bz = izazbz[..., 2]
        jz = ((1.0 + JZ_D) * iz) / (1.0 + JZ_D * iz) - JZ_D0
        return torch.stack([jz, az, bz], dim=-1)

    def coords_to_xyz(self, coords: torch.Tensor) -> torch.Tensor:
        jz = coords[..., 0]
        az = coords[..., 1]
        bz = coords[..., 2]
        numerator = jz + JZ_D0
        denominator = 1.0 + JZ_D - JZ_D * (jz + JZ_D0)
        iz = numerator / torch.clamp(denominator, min=1e-8)
        izazbz = torch.stack([iz, az, bz], dim=-1)
        lms_p = torch.matmul(izazbz, self.izazbz_to_lmsp.T)
        lms = pq_decode_torch(lms_p)
        xyz_p = torch.matmul(lms, self.lms_to_xyz.T)
        x_p = xyz_p[..., 0]
        y_p = xyz_p[..., 1]
        z_p = xyz_p[..., 2]
        x = (x_p + (JZ_B - 1.0) * z_p) / JZ_B
        y = (y_p + (JZ_G - 1.0) * x) / JZ_G
        return torch.stack([x, y, z_p], dim=-1)


class ICtCpAdapter(ColorSpaceAdapter):
    """
    PQ-based ICtCp (Dolby 2016/BT.2100 PQ) implementation.
    """

    def __init__(self, device: str | torch.device = "cpu"):
        device = torch.device(device)
        super().__init__(name="ictcp", display_name="ICtCp", device=device)
        self.rgb_to_lms = torch.tensor(
            MATRIX_ICTCP_RGB_TO_LMS, dtype=TORCH_DTYPE, device=device
        )
        self.lms_to_rgb = torch.linalg.inv(self.rgb_to_lms)
        self.lmsp_to_ictcp = torch.tensor(
            MATRIX_ICTCP_LMS_P_TO_ICTCP, dtype=TORCH_DTYPE, device=device
        )
        self.ictcp_to_lmsp = torch.tensor(
            MATRIX_ICTCP_ICTCP_TO_LMS_P, dtype=TORCH_DTYPE, device=device
        )
        self.xyz_to_bt2020 = torch.tensor(
            BT2020_XYZ_TO_RGB, dtype=TORCH_DTYPE, device=device
        )
        self.bt2020_to_xyz = torch.tensor(
            BT2020_RGB_TO_XYZ, dtype=TORCH_DTYPE, device=device
        )

    def xyz_to_coords(self, xyz: torch.Tensor) -> torch.Tensor:
        xyz = xyz.to(TORCH_DTYPE)
        rgb_bt2020 = torch.matmul(xyz, self.xyz_to_bt2020.T)
        lms = torch.matmul(rgb_bt2020, self.rgb_to_lms.T)
        lms_p = pq_encode_torch(torch.clamp(lms, min=0.0))
        return torch.matmul(lms_p, self.lmsp_to_ictcp.T)

    def coords_to_xyz(self, coords: torch.Tensor) -> torch.Tensor:
        lms_p = torch.matmul(coords.to(TORCH_DTYPE), self.ictcp_to_lmsp.T)
        lms = pq_decode_torch(lms_p)
        rgb_bt2020 = torch.matmul(lms, self.lms_to_rgb.T)
        return torch.matmul(rgb_bt2020, self.bt2020_to_xyz.T)


COLOR_SPACE_FACTORIES = {
    "sucs": SUCSAdapter,
    "jzazbz": JzAzBzAdapter,
    "ictcp": ICtCpAdapter,
}


def build_color_space(
    name: Literal["sucs", "jzazbz", "ictcp"], device: str | torch.device = "cpu"
) -> ColorSpaceAdapter:
    adapter_cls = COLOR_SPACE_FACTORIES[name]
    return adapter_cls(device=device)


def xyz_to_cam16ucs_np(xyz: np.ndarray) -> np.ndarray:
    import colour

    return colour.XYZ_to_CAM16UCS(xyz)


def cam16ucs_delta_e_np(xyz_a: np.ndarray, xyz_b: np.ndarray) -> float:
    a = xyz_to_cam16ucs_np(xyz_a)
    b = xyz_to_cam16ucs_np(xyz_b)
    return float(np.linalg.norm(a - b))
