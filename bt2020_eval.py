import numpy as np
import matplotlib
# 如果在服务器无头模式下运行请保留这行，如果在本地IDE运行可以注释掉
matplotlib.use('Agg') 
import matplotlib.pyplot as plt
import colour

# ==========================================
# 第一部分：移植 sUCS 模型 (纯 NumPy 实现)
# ==========================================
class NewSUCS_NumPy:
    """
    New sUCS Color Space 的纯 NumPy 实现，移除 PyTorch 依赖。
    参数来源: 您提供的 Model.py
    """
    def __init__(self):
        # 1. 训练参数 (Dual-Log Calibration)
        self.gamma_val = 0.7174
        self.sigma_val = 0.6464
        self.t1_val    = 59.5784
        self.t2_val    = 39.8886
        
        # M_Raw 矩阵
        m_raw_np = np.array([
            [0.4002, 0.7075, -0.0807],
            [-0.2280, 1.1500, 0.0612],
            [0.0000, 0.0000, 0.9184]
        ])
        
        # 行归一化
        row_sums = m_raw_np.sum(axis=1, keepdims=True)
        self.M_HPE = m_raw_np / row_sums
        
        # 2. 固定参数
        self.Gain = 1.0 + (self.sigma_val ** self.gamma_val)
        
        # M_Iab 构建
        w_l = np.array([2.0, 1.0, 0.05])
        row_i = w_l / w_l.sum()
        rgyb_fixed = np.array([
            [4.30, -4.70, 0.40],
            [0.49, 0.49, -0.98]
        ])
        # 组合并缩放
        self.M_Iab = np.vstack([row_i, rgyb_fixed]) * 100.0
        
        # 预计算逆矩阵
        self.M_Iab_inv = np.linalg.inv(self.M_Iab)
        self.M_HPE_inv = np.linalg.inv(self.M_HPE)
        
        # sRGB 到 XYZ 矩阵 (D65)
        self.M_sRGB_to_XYZ = np.array([
            [0.4124564, 0.3575761, 0.1804375],
            [0.2126729, 0.7151522, 0.0721750],
            [0.0193339, 0.1191920, 0.9503041],
        ])
        
        # 计算 sUCS 需要的白点归一化系数 (对应 sRGB [1,1,1])
        self.XYZ_white = self.M_sRGB_to_XYZ @ np.array([1.0, 1.0, 1.0])

    def sucs_to_xyz(self, sucs):
        """
        逆变换: sUCS (J, a', b') -> XYZ
        """
        # sucs shape: (..., 3)
        J = sucs[..., 0:1]
        a_prime = sucs[..., 1:2]
        b_prime = sucs[..., 2:3]
        
        # 1. 逆色度压缩 (Dual-log inverse)
        eps = 1e-8
        C_out = np.sqrt(a_prime**2 + b_prime**2 + eps)
        
        # C_in = T2 * (exp(C_out / T1) - 1)
        C_lin = self.t2_val * (np.exp(C_out / self.t1_val) - 1.0)
        
        scale = C_lin / C_out
        a_lin = a_prime * scale
        b_lin = b_prime * scale
        
        # 组合 Iab 线性坐标
        iab_lin = np.concatenate([J, a_lin, b_lin], axis=-1)
        
        # 2. 逆线性变换: Iab -> LMS_prime
        # 利用 einsum 处理任意维度: ...ij, kj -> ...ik (因为 M 在右边通常是转置乘，这里直接用 dot 逻辑)
        # Model.py 中是: lms_prime = iab_lin @ M_Iab_inv.T
        lms_prime = iab_lin @ self.M_Iab_inv.T
        
        # 3. 去除增益
        response = lms_prime / self.Gain
        
        # 4. 逆 Naka-Rushton
        res_abs = np.abs(response)
        res_sign = np.sign(response)
        
        # 安全截断，防止 R=1 导致奇点
        res_abs = np.clip(res_abs, 0.0, 0.9999)
        
        s_g = self.sigma_val ** self.gamma_val
        numerator = res_abs * s_g
        denominator = 1.0 - res_abs
        
        v_g = numerator / (denominator + 1e-10)
        
        # 逆幂函数
        v = np.power(v_g, 1.0 / self.gamma_val)
        lms = v * res_sign
        
        # 5. 逆线性变换: LMS -> XYZ
        xyz_norm = lms @ self.M_HPE_inv.T
        
        # 反归一化
        xyz = xyz_norm * self.XYZ_white
        return xyz

# ==========================================
# 第二部分：可视化逻辑
# ==========================================

def get_bt2020_matrices():
    """获取 BT.2020 和 sRGB 的转换矩阵 (D65)"""
    bt2020 = colour.RGB_COLOURSPACES['ITU-R BT.2020']
    srgb = colour.RGB_COLOURSPACES['sRGB']
    return bt2020.matrix_XYZ_to_RGB, srgb.matrix_XYZ_to_RGB

def xyz_to_rgb_linear(xyz, M):
    """将 XYZ 转换为线性 RGB"""
    # xyz: (H, W, 3), M: (3, 3)
    # 相当于对每个像素做 M @ xyz.T
    return np.einsum('ijk,lk->ijl', xyz, M)

def apply_gamma_srgb(linear_rgb):
    """应用 sRGB Gamma 校正用于显示"""
    # 简单的 Gamma 2.2 近似或者标准 sRGB 曲线，colour 库有现成的
    return colour.models.eotf_inverse_sRGB(np.clip(linear_rgb, 0, 1))

def main():
    print("Initializing NumPy model...")
    model = NewSUCS_NumPy()
    
    # 2. 准备网格
    I_val = 50.0
    ab_range = 80.0
    resolution = 1000  # 分辨率可以调高点，毕竟 NumPy 很快
    
    print(f"Generating grid ({resolution}x{resolution})...")
    a_vals = np.linspace(-ab_range, ab_range, resolution)
    b_vals = np.linspace(-ab_range, ab_range, resolution)
    aa, bb = np.meshgrid(a_vals, b_vals)
    
    # 构造输入 (H, W, 3)
    J_grid = np.full_like(aa, I_val)
    sucs_input = np.stack([J_grid, aa, bb], axis=-1)
    
    # 3. 计算: sUCS -> XYZ
    print("Converting sUCS to XYZ...")
    xyz = model.sucs_to_xyz(sucs_input)
    
    # 4. 转换到 RGB 空间
    M_xyz_to_bt2020, M_xyz_to_srgb = get_bt2020_matrices()
    
    rgb_bt2020_lin = xyz_to_rgb_linear(xyz, M_xyz_to_bt2020)
    rgb_srgb_lin = xyz_to_rgb_linear(xyz, M_xyz_to_srgb)
    
    # 5. 创建 Mask (色域判断)
    tol = 1e-4
    # BT.2020 色域内
    mask_bt2020 = np.all((rgb_bt2020_lin >= -tol) & (rgb_bt2020_lin <= 1+tol), axis=-1)
    # sRGB 色域内
    mask_srgb = np.all((rgb_srgb_lin >= -tol) & (rgb_srgb_lin <= 1+tol), axis=-1)
    
    # 6. 上色 (用于显示背景颜色)
    # 背景设为深灰色，突出主体
    display_img = np.ones((resolution, resolution, 3)) * 0.15
    
    # 计算显示用的 RGB (sRGB 空间，Gamma 校正)
    # 注意：这里我们显示的是该坐标点对应的真实颜色（如果在 sRGB 内）
    # 如果在 sRGB 外，display_rgb 会被 clip，也就是显示出“被截断”的颜色
    display_rgb = apply_gamma_srgb(rgb_srgb_lin)
    
    # 仅填充 BT.2020 区域内的颜色
    display_img[mask_bt2020] = display_rgb[mask_bt2020]
    
    # 7. 绘图
    print("Plotting...")
    fig, ax = plt.subplots(figsize=(10, 10))
    
    # 显示底图
    ax.imshow(display_img, extent=[-ab_range, ab_range, -ab_range, ab_range], origin='lower')
    
    # 画 BT.2020 边界 (Cyan色实线，在深色背景上更明显)
    ax.contour(aa, bb, mask_bt2020.astype(int), levels=[0.5], colors='cyan', linewidths=2, linestyles='-')
    
    # 画 sRGB 边界 (白色虚线)
    ax.contour(aa, bb, mask_srgb.astype(int), levels=[0.5], colors='white', linewidths=2, linestyles='--')
    
    # 辅助线
    ax.axhline(0, color='white', linestyle=':', alpha=0.3)
    ax.axvline(0, color='white', linestyle=':', alpha=0.3)
    
    ax.set_title(f'Gamut Shapes in New sUCS ab-plane (I={I_val})\nNumPy Version - No PyTorch Required', fontsize=14, color='black')
    ax.set_xlabel("a'", fontsize=12)
    ax.set_ylabel("b'", fontsize=12)
    
    # 图例
    from matplotlib.lines import Line2D
    custom_lines = [Line2D([0], [0], color='cyan', lw=2),
                    Line2D([0], [0], color='white', lw=2, linestyle='--')]
    ax.legend(custom_lines, ['BT.2020 Boundary', 'sRGB Boundary'], loc='upper right')
    
    save_path = 'bt2020_srgb_shapes_numpy.png'
    plt.savefig(save_path, dpi=150)
    print(f"Success! Image saved to {save_path}")

if __name__ == "__main__":
    main()