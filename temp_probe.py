import numpy as np
import torch
from hdr_gradient_workspace.src.color_spaces import build_color_space, bt2020_to_xyz_np, xyz_to_rec709_rgb_torch
from hdr_gradient_workspace.src.visualization import _linear_to_srgb
adapter = build_color_space('sucs')
bt_rgb = np.array([0.0, 0.05, 1.0], dtype=np.float64)
levels = np.linspace(0.01, 1.0, 256)
xyz_hdr = np.stack([bt2020_to_xyz_np(bt_rgb * level, 4000.0) for level in levels])
tensor_xyz = torch.tensor(xyz_hdr, dtype=torch.float64, device=adapter.device)
coords = adapter.xyz_to_coords(tensor_xyz)
l_axis = coords[..., 0]
l_norm = (l_axis - l_axis.min()) / (l_axis.max() - l_axis.min() + 1e-6)
sigmoid_gain = 3.0
pivot = 0.5
toned = torch.sigmoid(sigmoid_gain * (l_norm - pivot))
coords_mapped = coords.clone()
coords_mapped[..., 0] = toned * (coords[..., 0].max() - coords[..., 0].min()) + coords[..., 0].min()
ratio = coords_mapped[..., 0] / (coords[..., 0] + 1e-6)
coords_mapped[..., 1:] = coords_mapped[..., 1:] * ratio.unsqueeze(-1)
xyz_sdr = adapter.coords_to_xyz(coords_mapped)
rgb_linear = xyz_to_rec709_rgb_torch(xyz_sdr)
rgb = rgb_linear.detach().cpu().numpy()
print('first sample linear rgb:', rgb[0])
print('first sample srgb:', _linear_to_srgb(np.clip(rgb[0], 0.0, 1.2)))
