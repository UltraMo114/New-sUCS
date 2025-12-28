from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import numpy as np
import torch
from torch import nn
from torch.nn import functional as F
from torch.nn.utils import clip_grad_norm_

from .color_spaces import (
    ColorSpaceAdapter,
    SDR_WHITE_LUMINANCE,
    cam16ucs_delta_e_np,
    rec709_rgb_to_xyz_torch,
    xyz_to_rec709_rgb_torch,
)


@dataclass
class OptimizationSettings:
    """
    Tunable parameters for the HDR optimisation loop.
    """

    lr: float = 5e-3
    iterations: int = 500
    lambda_metric: float = 1.0
    lambda_hue: float = 0.05
    lambda_luminance: float = 0.1
    lambda_luminance_warm: float = 0.5
    luminance_warmup_iters: int = 100
    lambda_gamut: float = 20.0
    gamut_adaptive_scale: float = 5.0
    gamut_projection_mode: str = "hue_preserving"
    gamut_projection_steps: int = 6
    gamut_projection_shrink: float = 0.85
    gradient_clip: float = 50.0
    log_every: int = 10
    device: str = "cpu"


@dataclass
class IterationRecord:
    iteration: int
    loss: float
    metric_loss: float
    hue_loss: float
    luminance_loss: float
    gamut_loss: float
    grad_norm: float
    fraction_outside: float
    luminance_weight: float
    gamut_weight: float
    xyz: np.ndarray
    rgb_rec709: np.ndarray
    coords: np.ndarray


class HDRGradientOptimizer:
    """
    Runs differentiable HDR-to-SDR projection in a specific colour space.
    """

    def __init__(
        self,
        adapter: ColorSpaceAdapter,
        target_xyz: np.ndarray,
        settings: OptimizationSettings,
    ):
        self.adapter = adapter
        self.settings = settings
        self.device = torch.device(settings.device)
        dtype = adapter.dtype
        tensor_target = torch.tensor(target_xyz, dtype=dtype, device=self.device).view(
            1, 3
        )
        self.target_xyz = tensor_target
        self.hdr_coords = adapter.xyz_to_coords(tensor_target).detach()
        # Clamp luminance to SDR white for the initial guess to avoid conflicting objectives.
        start_xyz = tensor_target.clone()
        start_xyz[..., 1] = torch.clamp(
            start_xyz[..., 1], max=SDR_WHITE_LUMINANCE
        )
        start_coords = adapter.xyz_to_coords(start_xyz).detach()
        self.param = nn.Parameter(start_coords.clone())
        self.optimizer = torch.optim.Adam([self.param], lr=settings.lr)
        self.history: List[IterationRecord] = []
        self.last_fraction_outside: float = 0.0

    def _resolve_weights(self, iteration: int | None) -> tuple[float, float]:
        """
        Determine iteration-dependent luminance/gamut weights.
        """

        lambda_luma = self.settings.lambda_luminance
        if (
            iteration is not None
            and iteration < self.settings.luminance_warmup_iters
        ):
            lambda_luma = max(
                self.settings.lambda_luminance_warm,
                self.settings.lambda_luminance,
            )
        lambda_gamut = self.settings.lambda_gamut
        if self.settings.gamut_adaptive_scale > 0.0:
            lambda_gamut = lambda_gamut * (
                1.0 + self.settings.gamut_adaptive_scale * self.last_fraction_outside
            )
        return lambda_luma, lambda_gamut

    def _hue_loss(
        self, coords: torch.Tensor, target_coords: torch.Tensor
    ) -> torch.Tensor:
        hue_current = self.adapter.hue_angle(coords)
        hue_target = self.adapter.hue_angle(target_coords)
        # Use cosine distance to avoid atan2 numerical spikes.
        cos_sim = torch.cos(hue_current - hue_target)
        return torch.mean(1.0 - cos_sim)

    def _metric_loss(
        self, coords: torch.Tensor, target_coords: torch.Tensor
    ) -> torch.Tensor:
        # Focus on chromatic alignment; allow luminance to compress freely.
        delta = coords[..., 1:] - target_coords[..., 1:]
        return torch.mean(delta**2)

    def _luminance_loss(
        self, xyz: torch.Tensor, target_xyz: torch.Tensor
    ) -> torch.Tensor:
        goal = torch.clamp(target_xyz[..., 1], max=SDR_WHITE_LUMINANCE)
        return torch.mean((xyz[..., 1] - goal) ** 2)

    def _gamut_loss(self, rgb_rec709: torch.Tensor) -> torch.Tensor:
        margin = 0.04
        lower = F.softplus(-rgb_rec709 + margin)
        upper = F.softplus(rgb_rec709 - 1.0 + margin)
        return torch.mean(lower + upper)

    def _evaluate(
        self, coords: torch.Tensor, iteration: int | None = None
    ) -> Tuple[
        torch.Tensor,
        Dict[str, torch.Tensor],
        torch.Tensor,
        torch.Tensor,
        float,
        float,
    ]:
        xyz = self.adapter.coords_to_xyz(coords)
        rgb = xyz_to_rec709_rgb_torch(xyz)
        comp_metric = self._metric_loss(coords, self.hdr_coords)
        comp_hue = self._hue_loss(coords, self.hdr_coords)
        comp_luma = self._luminance_loss(xyz, self.target_xyz)
        comp_gamut = self._gamut_loss(rgb)
        lambda_luma, lambda_gamut = self._resolve_weights(iteration)
        total = (
            self.settings.lambda_metric * comp_metric
            + self.settings.lambda_hue * comp_hue
            + lambda_luma * comp_luma
            + lambda_gamut * comp_gamut
        )
        components = {
            "metric_loss": comp_metric,
            "hue_loss": comp_hue,
            "luminance_loss": comp_luma,
            "gamut_loss": comp_gamut,
        }
        return total, components, xyz, rgb, lambda_luma, lambda_gamut

    def _project_to_rec709(self) -> float:
        """
        Clamp the current Rec.709 RGB estimate into [0, 1]^3 and map back to
        the working colour space to keep optimisation in the display gamut.
        """

        with torch.no_grad():
            xyz = self.adapter.coords_to_xyz(self.param.data)
            rgb = xyz_to_rec709_rgb_torch(xyz)
            outside = ((rgb < 0.0) | (rgb > 1.0)).float()
            fraction = float(outside.mean().item())
            mode = (self.settings.gamut_projection_mode or "none").lower()
            if fraction == 0.0 or mode == "none":
                return fraction
            if mode == "hue_preserving":
                coords = self.param.data.clone()
                shrink = float(self.settings.gamut_projection_shrink)
                shrink = min(max(shrink, 0.1), 0.99)
                steps = max(1, int(self.settings.gamut_projection_steps))
                for _ in range(steps):
                    xyz = self.adapter.coords_to_xyz(coords)
                    rgb = xyz_to_rec709_rgb_torch(xyz)
                    mask = ((rgb < 0.0) | (rgb > 1.0)).any(dim=-1, keepdim=True)
                    if not mask.any():
                        break
                    coords[..., 1:] = torch.where(
                        mask,
                        coords[..., 1:] * shrink,
                        coords[..., 1:],
                    )
                self.param.data.copy_(coords)
                return fraction
            return fraction

    def step(self, iteration: int) -> None:
        self.optimizer.zero_grad(set_to_none=True)
        (
            loss,
            components,
            xyz,
            rgb,
            lambda_luma,
            lambda_gamut,
        ) = self._evaluate(self.param, iteration)
        loss.backward()
        grad_norm = float(self.param.grad.detach().norm().item())
        if self.settings.gradient_clip > 0:
            clip_grad_norm_([self.param], self.settings.gradient_clip)
        self.optimizer.step()
        fraction_outside = self._project_to_rec709()
        self.last_fraction_outside = fraction_outside
        if iteration % self.settings.log_every == 0 or iteration == self.settings.iterations - 1:
            record = IterationRecord(
                iteration=iteration,
                loss=float(loss.item()),
                metric_loss=float(components["metric_loss"].item()),
                hue_loss=float(components["hue_loss"].item()),
                luminance_loss=float(components["luminance_loss"].item()),
                gamut_loss=float(components["gamut_loss"].item()),
                grad_norm=grad_norm,
                fraction_outside=fraction_outside,
                luminance_weight=lambda_luma,
                gamut_weight=lambda_gamut,
                xyz=xyz.detach().cpu().numpy(),
                rgb_rec709=rgb.detach().cpu().numpy(),
                coords=self.param.detach().cpu().numpy(),
            )
            self.history.append(record)

    def run(self) -> IterationRecord:
        for iteration in range(self.settings.iterations):
            self.step(iteration)
        return self.history[-1]

    def evaluate_loss_grid(
        self,
        l_span: float = 20.0,
        chroma_span: float = 0.25,
        steps: int = 50,
    ) -> dict:
        """
        Sample the loss surface in the (J, a) plane around the optimum.
        """

        center = self.param.detach().clone()
        dtype = self.adapter.dtype
        l_offsets = torch.linspace(-l_span, l_span, steps, device=self.device, dtype=dtype)
        c_offsets = torch.linspace(-chroma_span, chroma_span, steps, device=self.device, dtype=dtype)
        grid = torch.zeros((steps, steps), dtype=dtype, device=self.device)
        with torch.no_grad():
            for i, dl in enumerate(l_offsets):
                for j, dc in enumerate(c_offsets):
                    coords = center.clone()
                    coords[..., 0] += dl
                    coords[..., 1] += dc
                    delta = coords[..., 1:] - self.hdr_coords[..., 1:]
                    grid[i, j] = torch.mean(delta**2)
        return {
            "l_offsets": l_offsets.cpu().numpy(),
            "c_offsets": c_offsets.cpu().numpy(),
            "loss_grid": grid.cpu().numpy(),
        }

    def final_cam16_delta(self) -> float:
        """
        Compute ΔE_CAM16 between the SDR projection and the HDR target.
        """

        final_xyz = self.adapter.coords_to_xyz(self.param.detach()).cpu().numpy()
        target_xyz = self.target_xyz.detach().cpu().numpy()
        return cam16ucs_delta_e_np(final_xyz, target_xyz)
