import numpy as np

"""
Note: Log 中的 Encoding 指的是 OETF。

输入为线性光，以反射率百分比表示，100% 为漫射白，超过 100% 的部分可以是自发光、镜面反光等，代表 HDR 元素。
输出为归一化的浮点数，范围 0-1。
"""


def encoding_oppo_log(scene_reflection):
    """
    Encoding function for Oppo Log (OETF).
    param:
        scene_reflection: Scene reflection in percentage (0-1600%).
    return:
        normalized_fp: Normalized FP values (0-1).
    """
    # check input range
    if np.any(scene_reflection < 0) or np.any(scene_reflection > 1600):
        raise ValueError("scene_reflection should be in the range of 0-1600%")
    gamma = 0.139
    beta = 0.019
    delta = 0.614
    e = 2.718281828
    linear_signal = scene_reflection / 100.0
    normalized_fp = gamma * np.log(linear_signal + beta) / np.log(e) + delta
    return normalized_fp


def encoding_mi_log(scene_reflection):
    """
    Encoding function for Mi Log (OETF).
    param:
        scene_reflection: Scene reflection in percentage (0-1152%).
    return:
        normalized_fp: Normalized FP values (0-1).
    """
    # check input range
    if np.any(scene_reflection < 0) or np.any(scene_reflection > 1152):
        raise ValueError("scene_reflection should be in the range of 0-1152%")

    # Constants from the formula
    R0 = -0.09023729
    Rt = 0.01974185
    c = 18.10531998
    gamma = 0.09271529
    beta = 0.01384578
    delta = 0.67291850

    # Convert percentage to linear signal R
    R = scene_reflection / 100.0

    # P = f(R) is a piecewise function:
    # 1. P = gamma * log2(R + beta) + delta, if R >= Rt
    # 2. P = c * (R - R0)**2,              if R0 <= R < Rt
    # 3. P = 0,                            if R < R0

    # Calculate values for the two main branches.
    log_branch = gamma * np.log2(R + beta) + delta
    quad_branch = c * np.power(R - R0, 2)

    # Apply the piecewise conditions
    normalized_fp = np.where(R >= Rt, log_branch, np.where(R >= R0, quad_branch, 0.0))

    return normalized_fp
