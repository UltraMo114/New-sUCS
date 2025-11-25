import torch
import torch.nn as nn
import numpy as np

class NewSUCS(nn.Module):
    """
    New sUCS Color Space (Differentiable Implementation in PyTorch)
    
    Architecture:
        1. XYZ -> LMS (Normalized HPE)
        2. Non-linearity: Naka-Rushton Response (S-curve)
        3. Linear Transform: LMS -> Iab (Fixed RGYB)
        4. Chroma Compression: Dual-parameter Log (Soft Saturation)

    Trained Parameters (from dual-log optimization):
        Gamma: 0.7174
        Sigma: 0.6464
        T1:    59.5784  (log scale)
        T2:    39.8886  (log rate)
    """
    
    def __init__(self, device='cpu', dtype=torch.float32):
        super().__init__()
        self.device = device
        self.dtype = dtype
        
        # =================================================================
        # 1. Trained Parameters (Dual-Log Calibration)
        # =================================================================
        self.gamma_val = 0.7174
        self.sigma_val = 0.6464
        self.t1_val    = 59.5784  # log scale (outer)
        self.t2_val    = 39.8886  # log rate (inner)
        
        # M_Raw from optimization result
        m_raw_np = np.array([
            [0.4002, 0.7075, -0.0807],
            [-0.2280, 1.1500, 0.0612],
            [0.0000, 0.0000, 0.9184]
        ])
        
        # Row Normalization (Sum of each row = 1)
        row_sums = m_raw_np.sum(axis=1, keepdims=True)
        m_hpe_norm = m_raw_np / row_sums
        
        # =================================================================
        # 2. Fixed Parameters (Structural Definitions)
        # =================================================================
        # Auto Gain: 1 + sigma^gamma
        self.gain_val = 1.0 + (self.sigma_val ** self.gamma_val)
        
        # M_Iab Construction
        # Weight for Achromatic axis I: [2, 1, 0.05]
        w_l = np.array([2.0, 1.0, 0.05])
        row_i = w_l / w_l.sum()
        
        # Fixed RGYB Plane
        rgyb_fixed = np.array([
            [4.30, -4.70, 0.40],
            [0.49, 0.49, -0.98]
        ])
        
        # Combine into 3x3 Matrix and scale by 100
        m_iab_np = np.vstack([row_i, rgyb_fixed]) * 100.0
        
        # =================================================================
        # 3. Register as PyTorch Buffers (Non-trainable constants)
        # =================================================================
        self.register_buffer('M_HPE', torch.tensor(m_hpe_norm, device=device, dtype=dtype))
        self.register_buffer('M_Iab', torch.tensor(m_iab_np, device=device, dtype=dtype))
        self.register_buffer('M_HPE_inv', torch.tensor(np.linalg.inv(m_hpe_norm), device=device, dtype=dtype))
        self.register_buffer('M_Iab_inv', torch.tensor(np.linalg.inv(m_iab_np), device=device, dtype=dtype))

        self.register_buffer('Gamma', torch.tensor(self.gamma_val, device=device, dtype=dtype))
        self.register_buffer('Sigma', torch.tensor(self.sigma_val, device=device, dtype=dtype))
        self.register_buffer('T1',    torch.tensor(self.t1_val, device=device, dtype=dtype))
        self.register_buffer('T2',    torch.tensor(self.t2_val, device=device, dtype=dtype))
        self.register_buffer('Gain',  torch.tensor(self.gain_val, device=device, dtype=dtype))
        
        # Standard sRGB to XYZ Matrix (D65) for convenience
        # X, Y, Z = M @ [R, G, B]^T
        srgb_to_xyz_np = np.array(
            [
                [0.4124564, 0.3575761, 0.1804375],
                [0.2126729, 0.7151522, 0.0721750],
                [0.0193339, 0.1191920, 0.9503041],
            ]
        )
        self.register_buffer("M_sRGB_to_XYZ", torch.tensor(srgb_to_xyz_np, device=device, dtype=dtype))
        self.register_buffer("M_XYZ_to_sRGB", torch.tensor(np.linalg.inv(srgb_to_xyz_np), device=device, dtype=dtype))

        # XYZ of sRGB white (RGB = [1, 1, 1]). The New sUCS forward expects
        # XYZ normalized by a white reference, so we hard-code the one
        # corresponding to unit sRGB white.
        rgb_white_np = np.array([1.0, 1.0, 1.0])
        xyz_white_np = srgb_to_xyz_np @ rgb_white_np
        self.register_buffer("XYZ_white_from_unit_rgb", torch.tensor(xyz_white_np, device=device, dtype=dtype))

        # Diagnostics for sUCS -> sRGB mapping (populated in sucs_to_srgb).
        self.last_linear_range = (0.0, 0.0)
        self.last_gamma_range = (0.0, 0.0)
        self.last_fraction_outside_linear = 0.0
        self.last_fraction_outside_gamma = 0.0

    # =================================================================
    # Forward: XYZ -> New sUCS
    # =================================================================
    def xyz_to_sucs(self, xyz):
        """
        Args:
            xyz: Tensor of shape (N, 3), range usually [0, 1] or [0, 100]
                 (Assumes input is relative XYZ, D65 white point implicit if not divided)
                 Note: If input is standard range [0, 1], the math holds.
        Returns:
            sucs: Tensor of shape (N, 3) [J, a', b']
        """
        # 0. Normalize XYZ by the sRGB white corresponding to RGB = [1, 1, 1].
        # This matches the normalization used when the model parameters were
        # trained, where inputs are expressed relative to a fixed white point.
        xyz_norm = xyz / self.XYZ_white_from_unit_rgb

        # 1. Linear Transform: XYZ -> LMS
        # (N, 3) @ (3, 3).T -> (N, 3)
        lms = torch.matmul(xyz_norm, self.M_HPE.T)
        
        # 2. Naka-Rushton Response (Non-linearity)
        # R = (v^g) / (v^g + sigma^g)
        # Add epsilon to avoid division by zero or gradient explosion at 0
        eps = 1e-10
        lms_abs = torch.abs(lms)
        lms_sign = torch.sign(lms)
        
        v_g = torch.pow(lms_abs, self.Gamma)
        s_g = torch.pow(self.Sigma, self.Gamma)
        
        response = (v_g / (v_g + s_g + eps)) * lms_sign
        
        # 3. Auto Gain (Normalize White)
        lms_prime = response * self.Gain
        
        # 4. Linear Transform: LMS -> Iab_linear
        iab_lin = torch.matmul(lms_prime, self.M_Iab.T)
        
        I = iab_lin[:, 0:1]
        a = iab_lin[:, 1:2]
        b = iab_lin[:, 2:3]

        # 5. Dual-parameter log chroma compression (soft saturation)
        # C_out = T1 * log(1 + C_in / T2)
        C_lin = torch.sqrt(a ** 2 + b ** 2 + eps)
        C_out = self.T1 * torch.log1p(C_lin / self.T2)

        # Scaling factor G = C_out / C_in
        scale = C_out / C_lin

        a_out = a * scale
        b_out = b * scale

        return torch.cat([I, a_out, b_out], dim=1)

    # =================================================================
    # Inverse: New sUCS -> XYZ (Crucial for Optimization Loop)
    # =================================================================
    def sucs_to_xyz(self, sucs):
        """
        Args:
            sucs: Tensor of shape (N, 3) [J, a', b']
        Returns:
            xyz: Tensor of shape (N, 3)
        """
        J = sucs[:, 0:1]
        a_prime = sucs[:, 1:2]
        b_prime = sucs[:, 2:3]
        
        # 1. Inverse chroma compression (dual-parameter log)
        eps = 1e-8
        C_out = torch.sqrt(a_prime ** 2 + b_prime ** 2 + eps)

        # Forward: C_out = T1 * log(1 + C_in / T2)
        # Inverse: C_in = T2 * (exp(C_out / T1) - 1)
        C_lin = self.T2 * (torch.exp(C_out / self.T1) - 1.0)

        scale = C_lin / C_out
        a_lin = a_prime * scale
        b_lin = b_prime * scale
        
        iab_lin = torch.cat([J, a_lin, b_lin], dim=1)
        
        # 2. Inverse Linear Transform: Iab -> LMS
        lms_prime = torch.matmul(iab_lin, self.M_Iab_inv.T)
        
        # 3. Remove Gain
        response = lms_prime / self.Gain
        
        # 4. Inverse Naka-Rushton
        # R = v^g / (v^g + s^g)  =>  v^g = R * s^g / (1 - |R|)
        # v = ( R * s^g / (1 - |R|) )^(1/g)
        
        res_abs = torch.abs(response)
        res_sign = torch.sign(response)
        
        # Safety clamp to avoid singularity at R=1 (which means infinite light)
        res_abs = torch.clamp(res_abs, 0.0, 0.9999)
        
        s_g = torch.pow(self.Sigma, self.Gamma)
        
        numerator = res_abs * s_g
        denominator = 1.0 - res_abs
        
        v_g = numerator / (denominator + eps)
        
        # Inverse power
        v = torch.pow(v_g, 1.0 / self.Gamma)
        lms = v * res_sign
        
        # 5. Inverse Linear Transform: LMS -> XYZ
        xyz = torch.matmul(lms, self.M_HPE_inv.T)
        xyz_norm = xyz * self.XYZ_white_from_unit_rgb
        return xyz_norm

    # =================================================================
    # Helper: sUCS -> sRGB (End-to-End for Visualization)
    # =================================================================
    def sucs_to_srgb(self, sucs):
        """
        Converts sUCS coordinates directly to sRGB (Linear -> Gamma).
        Useful for the optimization loop to generate colors.
        """
        xyz = self.sucs_to_xyz(sucs)
        
        # XYZ -> Linear RGB
        rgb_linear = torch.matmul(xyz, self.M_XYZ_to_sRGB.T)
        
        # Gamma Correction (sRGB standard)
        # if x <= 0.0031308: 12.92 * x
        # else: 1.055 * x^(1/2.4) - 0.055
        # Using soft approximation or strict boolean logic
        
        mask = (rgb_linear <= 0.0031308)
        rgb_gamma = torch.zeros_like(rgb_linear)
        
        rgb_gamma[mask] = 12.92 * rgb_linear[mask]
        rgb_gamma[~mask] = 1.055 * torch.pow(torch.clamp(rgb_linear[~mask], min=1e-8), 1.0/2.4) - 0.055

        # Diagnostics: track ranges and fraction of samples outside [0, 1]
        # both in linear RGB and gamma-encoded RGB.
        with torch.no_grad():
            lin_min = float(rgb_linear.min().detach().cpu().item())
            lin_max = float(rgb_linear.max().detach().cpu().item())
            gam_min = float(rgb_gamma.min().detach().cpu().item())
            gam_max = float(rgb_gamma.max().detach().cpu().item())
            self.last_linear_range = (lin_min, lin_max)
            self.last_gamma_range = (gam_min, gam_max)

            lin_outside = (rgb_linear < 0.0) | (rgb_linear > 1.0)
            gam_outside = (rgb_gamma < 0.0) | (rgb_gamma > 1.0)
            self.last_fraction_outside_linear = float(lin_outside.float().mean().cpu().item())
            self.last_fraction_outside_gamma = float(gam_outside.float().mean().cpu().item())

        return rgb_gamma

# =================================================================
# Usage Example
# =================================================================
if __name__ == "__main__":
    # Example: Convert Red, Green, Blue to New sUCS
    model = NewSUCS()
    
    # RGB Inputs
    rgb_input = torch.tensor([
        [1.0, 0.0, 0.0], # Red
        [0.0, 1.0, 0.0], # Green
        [0.0, 0.0, 1.0], # Blue
        [1.0, 1.0, 1.0]  # White
    ])
    
    # 1. RGB -> XYZ (Linear approx for test)
    # Note: In real pipeline use sRGB gamma linearization first
    xyz_input = torch.matmul(rgb_input, model.M_sRGB_to_XYZ.T)
    
    # 2. Forward
    sucs_out = model.xyz_to_sucs(xyz_input)
    print("New sUCS Coordinates:\n", sucs_out)
    
    # 3. Inverse
    xyz_rec = model.sucs_to_xyz(sucs_out)
    rgb_rec = torch.matmul(xyz_rec, model.M_XYZ_to_sRGB.T)
    
    print("\nReconstructed RGB (Should match input):\n", rgb_rec)
    print("\nError:", (rgb_input - rgb_rec).abs().sum().item())
