import numpy as np
import matplotlib
matplotlib.use('Agg') # 适用于无窗口环境
import matplotlib.pyplot as plt

# ==========================================
# 1. 基础工具与色彩矩阵 (BT.2020 / D65)
# ==========================================

# BT.2020 (Linear) -> XYZ (D65)
M_BT2020_TO_XYZ = np.array([
    [0.636958, 0.144617, 0.168881],
    [0.262700, 0.677998, 0.059302],
    [0.000000, 0.028073, 1.060985]
])

# XYZ (D65) -> BT.2020 (Linear)
M_XYZ_TO_BT2020 = np.linalg.inv(M_BT2020_TO_XYZ)

# XYZ (D65) -> sRGB (Linear)
M_XYZ_TO_SRGB = np.array([
    [ 3.2404542, -1.5371385, -0.4985314],
    [-0.9692660,  1.8760108,  0.0415560],
    [ 0.0556434, -0.2040259,  1.0572252]
])

def apply_gamma_srgb(linear):
    """标准 sRGB Gamma 校正 (用于最终显示)"""
    linear = np.clip(linear, 0.0, 1.0)
    srgb = np.where(linear <= 0.0031308,
                    12.92 * linear,
                    1.055 * np.power(linear, 1.0/2.4) - 0.055)
    return srgb

# ==========================================
# 2. sUCS 模型 (NumPy 版)
# ==========================================
class NewSUCS_NumPy:
    def __init__(self):
        self.gamma = 0.7174
        self.sigma = 0.6464
        self.t1 = 59.5784
        self.t2 = 39.8886
        self.gain = 1.0 + (self.sigma ** self.gamma)
        
        # M_HPE (Normalized)
        m_raw = np.array([[0.4002, 0.7075, -0.0807],
                          [-0.2280, 1.1500, 0.0612],
                          [0.0000, 0.0000, 0.9184]])
        self.M_HPE = m_raw / m_raw.sum(axis=1, keepdims=True)
        self.M_HPE_inv = np.linalg.inv(self.M_HPE)
        
        # M_Iab
        w_l = np.array([2.0, 1.0, 0.05])
        row_i = w_l / w_l.sum()
        rgyb = np.array([[4.30, -4.70, 0.40], [0.49, 0.49, -0.98]])
        self.M_Iab = np.vstack([row_i, rgyb]) * 100.0
        self.M_Iab_inv = np.linalg.inv(self.M_Iab)
        
        # sRGB D65 White XYZ for normalization
        self.xyz_white = np.array([0.95047, 1.00000, 1.08883])

    def xyz_to_sucs(self, xyz):
        # 1. Normalize
        xyz_norm = xyz / self.xyz_white
        # 2. LMS
        lms = xyz_norm @ self.M_HPE.T
        # 3. Naka-Rushton
        v_g = np.power(np.abs(lms), self.gamma)
        s_g = self.sigma ** self.gamma
        resp = (v_g / (v_g + s_g + 1e-10)) * np.sign(lms)
        lms_p = resp * self.gain
        # 4. Iab
        iab = lms_p @ self.M_Iab.T
        I, a, b = iab[..., 0:1], iab[..., 1:2], iab[..., 2:3]
        # 5. Chroma Compression
        C_lin = np.sqrt(a**2 + b**2)
        C_out = self.t1 * np.log1p(C_lin / self.t2)
        scale = np.divide(C_out, C_lin, out=np.ones_like(C_lin), where=C_lin!=0)
        return np.concatenate([I, a*scale, b*scale], axis=-1)

    def sucs_to_xyz(self, sucs):
        J, ap, bp = sucs[..., 0:1], sucs[..., 1:2], sucs[..., 2:3]
        # 1. Inverse Chroma
        C_out = np.sqrt(ap**2 + bp**2)
        C_lin = self.t2 * (np.exp(C_out / self.t1) - 1.0)
        scale = np.divide(C_lin, C_out, out=np.ones_like(C_out), where=C_out!=0)
        iab_lin = np.concatenate([J, ap*scale, bp*scale], axis=-1)
        # 2. Inverse Iab
        lms_p = iab_lin @ self.M_Iab_inv.T
        # 3. Inverse Gain & NR
        resp = lms_p / self.gain
        res_abs = np.clip(np.abs(resp), 0, 0.9999)
        s_g = self.sigma ** self.gamma
        v_g = (res_abs * s_g) / (1.0 - res_abs + 1e-10)
        lms = np.power(v_g, 1.0/self.gamma) * np.sign(resp)
        # 4. Inverse LMS
        xyz = (lms @ self.M_HPE_inv.T) * self.xyz_white
        return xyz

# ==========================================
# 3. ICtCp 模型 (Dolby / ITU-R BT.2100)
# ==========================================
class ICtCp_NumPy:
    def __init__(self):
        # BT.2020 RGB to LMS (ITU-R BT.2100)
        self.M1 = np.array([
            [1688, 2146, 262],
            [683,  2951, 462],
            [99,   309,  3688]
        ]) / 4096.0
        
        # LMS to ICtCp
        self.M2 = np.array([
            [2048, 2048, 0],
            [6610, -13613, 7003],
            [17933, -17390, -543]
        ]) / 4096.0
        
        self.M1_inv = np.linalg.inv(self.M1)
        self.M2_inv = np.linalg.inv(self.M2)
        
        # PQ Constants
        self.c1 = 0.8359375
        self.c2 = 18.8515625
        self.c3 = 18.6875
        self.m1 = 0.1593017578125
        self.m2 = 78.84375

    def pq_eotf_inv(self, linear_nits):
        """
        Linear (Nits) -> Non-linear (PQ Code 0-1)
        必须先归一化到 10000 nits
        """
        Y = np.maximum(linear_nits, 0.0) / 10000.0  # Normalize
        Y_m1 = np.power(Y, self.m1)
        
        num = self.c1 + self.c2 * Y_m1
        den = 1.0 + self.c3 * Y_m1
        N = np.power(num / den, self.m2)
        return N

    def pq_eotf(self, N):
        """
        Non-linear (PQ Code 0-1) -> Linear (Nits)
        """
        N = np.clip(N, 0.0, 1.0)
        N_m2 = np.power(N, 1.0 / self.m2)
        
        num = np.maximum(N_m2 - self.c1, 0.0)
        den = self.c2 - self.c3 * N_m2
        
        Y_norm = np.power(np.divide(num, den, out=np.zeros_like(num), where=den!=0), 1.0 / self.m1)
        return Y_norm * 10000.0  # De-normalize

    def rgb_to_ictcp(self, rgb_linear_nits):
        # 1. Linear RGB (Nits) -> LMS
        lms_linear = rgb_linear_nits @ self.M1.T
        # 2. PQ Nonlinearity (LMS_linear -> LMS_prime)
        lms_p = self.pq_eotf_inv(lms_linear)
        # 3. LMS -> ICtCp
        return lms_p @ self.M2.T

    def ictcp_to_rgb(self, ictcp):
        # 1. Inverse Matrix
        lms_p = ictcp @ self.M2_inv.T
        # 2. Inverse PQ (LMS_prime -> LMS_linear_nits)
        lms_linear = self.pq_eotf(lms_p)
        # 3. Inverse LMS -> RGB (Nits)
        return lms_linear @ self.M1_inv.T

    def pq_eotf(self, N):
        # PQ EOTF (Non-linear to Linear)
        c1 = 0.8359375
        c2 = 18.8515625
        c3 = 18.6875
        m1 = 0.1593017578125
        m2 = 78.84375
        
        N_p = np.power(np.maximum(N, 0), 1.0/m2)
        num = np.maximum(N_p - c1, 0)
        den = c2 - c3 * N_p
        val = np.divide(num, den, out=np.zeros_like(num), where=den!=0)
        return np.power(np.maximum(val, 0), 1.0/m1)

    def rgb_to_ictcp(self, rgb_linear):
        # 1. Linear RGB (BT.2020) -> LMS
        lms = rgb_linear @ self.M1.T
        # 2. PQ Nonlinearity
        lms_p = self.pq_eotf_inv(lms)
        # 3. LMS -> ICtCp
        return lms_p @ self.M2.T

    def ictcp_to_rgb(self, ictcp):
        # 1. Inverse Matrix
        lms_p = ictcp @ self.M2_inv.T
        # 2. Inverse PQ
        lms = self.pq_eotf(lms_p)
        # 3. Inverse LMS -> RGB
        return lms @ self.M1_inv.T

# ==========================================
# 4. JzAzBz 模型
# ==========================================
class JzAzBz_NumPy:
    def __init__(self):
        # XYZ to LMS
        self.M1 = np.array([
            [0.674207838, 0.382799340, -0.047570458],
            [0.149284160, 0.739628340,  0.083327300],
            [0.070941080, 0.174768000,  0.670970020]
        ])
        # LMS to IzAzBz
        self.M2 = np.array([
            [0.5, 0.5, 0],
            [3.524000, -4.066708, 0.542708],
            [0.199076, 1.096799, -1.295875]
        ])
        self.M1_inv = np.linalg.inv(self.M1)
        self.M2_inv = np.linalg.inv(self.M2)
        
        # PQ-like constants
        self.b = 1.15
        self.g = 0.66
        self.c1 = 0.8359375
        self.c2 = 18.8515625
        self.c3 = 18.6875
        self.n = 0.1593017578125
        self.p = 134.034375
        self.d = -0.56
        self.d0 = 1.6295499532821566e-11

    def pq_like(self, x):
        x_p = np.power(np.maximum(x, 0), self.n)
        num = self.c1 + self.c2 * x_p
        den = 1 + self.c3 * x_p
        return np.power(num / den, self.p)

    def pq_like_inv(self, x):
        x_p = np.power(np.maximum(x, 0), 1.0/self.p)
        num = np.maximum(x_p - self.c1, 0)
        den = self.c2 - self.c3 * x_p
        val = np.divide(num, den, out=np.zeros_like(num), where=den!=0)
        return np.power(np.maximum(val, 0), 1.0/self.n)

    def xyz_to_jzazbz(self, xyz):
        # 1. XYZ -> LMS
        # JzAzBz expects absolute luminance. We assume input xyz is scaled to nits.
        lms = (self.b * xyz - (self.b - 1) * xyz[..., 2:3]) @ self.M1.T # Simplified adaptation
        lms = xyz @ self.M1.T # Using standard form without chromatic adaptation shift for now
        
        # 2. PQ-like
        lms_p = self.pq_like(lms)
        
        # 3. IzAzBz
        izazbz = lms_p @ self.M2.T
        Iz = izazbz[..., 0:1]
        
        # 4. Iz -> Jz
        Jz = ((1 + self.d) * Iz) / (1 + self.d * Iz) - self.d0
        
        return np.concatenate([Jz, izazbz[..., 1:]], axis=-1)

    def jzazbz_to_xyz(self, jzazbz):
        Jz = jzazbz[..., 0:1] + self.d0
        AzBz = jzazbz[..., 1:]
        
        # 1. Jz -> Iz
        Iz = Jz / (1 + self.d - self.d * Jz)
        
        # 2. IzAzBz -> LMS_p
        izazbz = np.concatenate([Iz, AzBz], axis=-1)
        lms_p = izazbz @ self.M2_inv.T
        
        # 3. Inv PQ
        lms = self.pq_like_inv(lms_p)
        
        # 4. LMS -> XYZ
        xyz = lms @ self.M1_inv.T
        return xyz

# ==========================================
# 5. 实验主逻辑
# ==========================================
def generate_strip(model_name, model_obj, target_bt2020_rgb, steps=500):
    """
    生成一条从灰度到目标颜色的等亮度均匀色带
    """
    # 1. 准备目标颜色 (Linear BT.2020)
    target_rgb = np.array(target_bt2020_rgb).reshape(1, 3)
    
    # 2. 转换到目标空间获取 Lightness 和 Max Chroma
    # 注意：ICtCp 和 JzAzBz 需要绝对亮度。假设 200 nits 作为参考白亮度。
    # sUCS 使用相对值。
    abs_scale = 200.0 if model_name in ['ICtCp', 'JzAzBz'] else 1.0
    
    # 坐标转换
    if model_name == 'sUCS':
        xyz = target_rgb @ M_BT2020_TO_XYZ.T
        coords = model_obj.xyz_to_sucs(xyz) # J, a', b'
    elif model_name == 'ICtCp':
        coords = model_obj.rgb_to_ictcp(target_rgb * abs_scale) # I, Ct, Cp
        coords[..., 0] /= 10000.0 # Normalize I for internal logic? No, keep as is.
    elif model_name == 'JzAzBz':
        xyz = target_rgb @ M_BT2020_TO_XYZ.T * abs_scale
        # JzAzBz expect X to be scaled such that Y=1 is 1 nit? Or Y=100?
        # The paper uses 0-10000. 200 nits = 200.
        coords = model_obj.xyz_to_jzazbz(xyz * 1e-4 if False else xyz) # Some impls scale, some don't.
        # Let's assume standard nits input for our JzAzBz impl.
        
    L_fixed = coords[0, 0]
    C1 = coords[0, 1]
    C2 = coords[0, 2]
    
    # 3. 插值 (在感知空间)
    # L 保持固定, (C1, C2) 从 (0,0) 线性插值到目标点
    alphas = np.linspace(0, 1, steps).reshape(-1, 1)
    
    L_seq = np.full((steps, 1), L_fixed)
    C1_seq = C1 * alphas
    C2_seq = C2 * alphas
    
    interp_coords = np.concatenate([L_seq, C1_seq, C2_seq], axis=-1)
    
    # 4. 反变换回 Linear RGB (BT.2020)
    if model_name == 'sUCS':
        xyz_out = model_obj.sucs_to_xyz(interp_coords)
        rgb_out = xyz_out @ M_XYZ_TO_BT2020.T
    elif model_name == 'ICtCp':
        # Undo implicit scaling if any
        # interp_coords[..., 0] *= 10000.0 
        rgb_out = model_obj.ictcp_to_rgb(interp_coords) / abs_scale
    elif model_name == 'JzAzBz':
        xyz_out = model_obj.jzazbz_to_xyz(interp_coords) / abs_scale
        rgb_out = xyz_out @ M_XYZ_TO_BT2020.T
        
    return rgb_out

def main():
    sucs = NewSUCS_NumPy()
    ictcp = ICtCp_NumPy()
    jzazbz = JzAzBz_NumPy()
    
    # 目标：BT.2020 的 R, G, B 顶点
    targets = {
        'Red':   [1.0, 0.0, 0.0],
        'Green': [0.0, 1.0, 0.0],
        'Blue':  [0.0, 0.0, 1.0]
    }
    
    models = {
        'sUCS': sucs,
        'ICtCp': ictcp,
        'JzAzBz': jzazbz
    }
    
    fig, axes = plt.subplots(3, 3, figsize=(12, 6), constrained_layout=True)
    
    for i, (color_name, rgb_val) in enumerate(targets.items()):
        for j, (model_name, model_obj) in enumerate(models.items()):
            
            # 生成色带数据 (Linear BT.2020)
            rgb_linear_bt2020 = generate_strip(model_name, model_obj, rgb_val)
            
            # 转换到 sRGB 用于显示 (Linear BT2020 -> XYZ -> Linear sRGB -> Gamma sRGB)
            xyz = rgb_linear_bt2020 @ M_BT2020_TO_XYZ.T
            rgb_linear_srgb = xyz @ M_XYZ_TO_SRGB.T
            img_disp = apply_gamma_srgb(rgb_linear_srgb)
            
            # 扩展成图像条
            img_strip = np.tile(img_disp[np.newaxis, :, :], (50, 1, 1))
            
            ax = axes[i, j]
            ax.imshow(img_strip, extent=[0, 1, 0, 1])
            ax.set_axis_off()
            
            if i == 0:
                ax.set_title(model_name, fontsize=12, fontweight='bold')
            if j == 0:
                ax.text(-0.1, 0.5, color_name, transform=ax.transAxes, 
                        va='center', ha='right', fontsize=12, fontweight='bold')

    plt.suptitle("Uniform Gradient Comparison (Fixed Lightness)\n0 -> BT.2020 Gamut Boundary", fontsize=14)
    plt.savefig('uniform_strips_comparison.png', dpi=150)
    print("Done! Saved to uniform_strips_comparison.png")

if __name__ == "__main__":
    main()