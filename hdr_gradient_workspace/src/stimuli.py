from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Sequence

import numpy as np

from .color_spaces import bt2020_to_xyz_np


@dataclass
class HDRStimulus:
    """
    Simple container describing an HDR target anchored on the BT.2020 gamut edge.
    """

    name: str
    bt2020_rgb: np.ndarray
    luminance_nits: float
    description: str = ""

    def xyz(self) -> np.ndarray:
        return bt2020_to_xyz_np(self.bt2020_rgb, self.luminance_nits)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "bt2020_rgb": self.bt2020_rgb.tolist(),
            "luminance_nits": float(self.luminance_nits),
            "description": self.description,
        }


def _base_hue_vectors() -> list[tuple[str, np.ndarray]]:
    """
    Synthesise BT.2020 boundary vectors that roughly cover opposing hues.
    """

    raw = [
        ("hyper_red", np.array([1.0, 0.0, 0.02])),
        ("amber", np.array([1.0, 0.35, 0.0])),
        ("yellow", np.array([1.0, 0.9, 0.0])),
        ("green", np.array([0.0, 1.0, 0.1])),
        ("cyan", np.array([0.05, 0.85, 1.0])),
        ("blue", np.array([0.0, 0.05, 1.0])),
        ("indigo", np.array([0.2, 0.0, 1.0])),
        ("magenta", np.array([1.0, 0.0, 0.8])),
    ]
    return [(name, vec / np.max(vec)) for name, vec in raw]


def generate_extreme_stimuli(
    luminance_levels: Sequence[float] = (1000.0, 2000.0, 4000.0)
) -> List[HDRStimulus]:
    """
    Create a diverse suite of HDR stimuli for the stress test.
    """

    stimuli: list[HDRStimulus] = []
    for level in luminance_levels:
        for base_name, rgb in _base_hue_vectors():
            name = f"{base_name}_{int(level)}nits"
            desc = f"{base_name} @ {level:.0f} nits on BT.2020 edge"
            stimuli.append(HDRStimulus(name=name, bt2020_rgb=rgb, luminance_nits=level, description=desc))
    return stimuli


def stimuli_from_config(config: Iterable[dict]) -> List[HDRStimulus]:
    """
    Build stimuli from an iterable of dictionaries (e.g., JSON/YAML definitions).
    """

    parsed: list[HDRStimulus] = []
    for entry in config:
        parsed.append(
            HDRStimulus(
                name=entry["name"],
                bt2020_rgb=np.array(entry["bt2020_rgb"], dtype=np.float64),
                luminance_nits=float(entry["luminance_nits"]),
                description=entry.get("description", ""),
            )
        )
    return parsed
