This document is a complete task specification for an AI agent or algorithm engineer. It defines the experimental background, mathematical logic, code structure, and expected outputs for the **log-version** New sUCS branch. You can copy it directly and send it to the engineer or agent who will implement or extend the system.

-----

# Task Specification (log-version): Gradient-Descent-Based Colormap Optimization Testbed

## 1. Project Context

The goal of this project is to empirically demonstrate the usefulness of the **New sUCS** color space (dual-log version) in the task of colormap optimization. We build a differentiable optimization framework in PyTorch that takes an initial non-uniform colormap (Matplotlib `rainbow`) and optimizes it via gradient descent into a perceptually uniform colormap.

**Core comparison logic**

- **Baselines (CIELAB and OkLab)**: both are Euclidean perceptual spaces that are mapped linearly to sRGB. During optimization, to ensure that colors remain displayable (inside the sRGB gamut), we must clamp values with `clamp(0, 1)`. This causes gradients at the boundaries to vanish or oscillate, which harms convergence and can introduce artifacts.
- **Ours (New sUCS, dual-log)**: New sUCS is also a Euclidean perceptual space, but it uses:
  - Naka–Rushton response on HPE-normalized LMS channels (soft response curve),
  - a fixed Iab transform (RGYB plane),
  - and a dual-parameter logarithmic chroma compression
    - `C_out = T1 * log(1 + C_in / T2)`
  before mapping to sRGB.  
  Parameters `(Gamma, Sigma, T1, T2)` are calibrated from psychophysical data; the mapping is fully differentiable and designed to keep values in a numerically safe region. In the colormap optimization loop we constrain a′/b′ to a reasonable range to avoid extreme out-of-gamut excursions.

The intent of this branch is to evaluate the **dual-log New sUCS** variant in the same optimization testbed as the previous tanh-based version, and to compare it to CIELAB / OkLab as well as SOTA static colormaps (Viridis, Magma, Plasma).

## 2. Environment Requirements

- **Python**: 3.8 or newer
- **PyTorch**: 1.10 or newer (for automatic differentiation)
- **colour-science**: `pip install colour-science` (for computing CIEDE2000, CAM16-UCS, etc.)
- **Matplotlib / NumPy**: for plotting and data processing

-----

## 3. Module Specifications

### Module A: The Referee

**Purpose**: a non-trainable, objective evaluator that measures the true quality of a colormap at the end of each epoch.

**Theoretical basis**: use the local uniformity definition from Bujack et al., TVCG 2018.

- **Input**: `rgb_samples`, shape `(N, 3)`, values in `[0, 1]`.
- **Computation pipeline (DE2000 referee)**:
  1. Convert RGB to CIE Lab (D65, 2 degree observer).
  2. Compute perceptual distances between adjacent samples using CIEDE2000:
     - `d_i = DeltaE_00(C_i, C_{i+1})`.
  3. Compute the speed standard deviation sigma_v:
     - `sigma_v = std(d)`.
  4. Compute the minimum speed v_min:
     - `v_min = min(d)`.

- **Alternative metric** (optional): CAM16-UCS J'a'b' with ΔE_CAM16UCS.

- **Output**: dictionary such as:

  ```python
  {"sigma_v": float, "v_min": float}
  ```

### Module B: Optimization Spaces

Implement three classes derived from `torch.nn.Module`, each representing a competing color space.

#### 1. `BaselineCIELAB`

- **Parameters**: `self.control_points`, shape `(K, 3)`, initialized with CIELAB values.
- **Forward pass**:
  1. Differentiable `Lab_to_XYZ` (D65).
  2. Differentiable `XYZ_to_linear_sRGB`.
  3. Differentiable gamma encoding to sRGB.
  4. **Key operation (simulate artifacts)**: apply `torch.clamp(rgb, 0.0, 1.0)` before returning to simulate gamut clipping as in Nardini et al., CGF 2021.
- **Output**: `rgb_values` in `[0, 1]`.

#### 2. `BaselineOkLab`

- **Parameters**: `self.control_points`, shape `(K, 3)`, initialized with OkLab values.
- **Forward pass**:
  1. Use Björn Ottosson's 2020 matrices:
     - `LMS = M1 @ RGB_lin`
     - `LMS_prime = LMS ** (1/3)` (nonlinearity)
     - `RGB_lin = M2 @ LMS_prime`
  2. Gamma encode to sRGB.
  3. **Key operation**: apply `torch.clamp(rgb, 0.0, 1.0)` before returning.
- **Output**: `rgb_values` in `[0, 1]`.

#### 3. `OursNewSUCS` (dual-log version)

- **Parameters**: `self.control_points`, shape `(K, 3)`, living in the New sUCS latent space `(J, a', b')`.
- **Internal model**: uses `Model.NewSUCS`, which implements:
  1. Normalize XYZ by the sRGB white corresponding to RGB = [1, 1, 1].
  2. `XYZ -> LMS` via normalized HPE cone matrix.
  3. Naka–Rushton response:
     - `v_g = |v|^Gamma`
     - `s_g = Sigma^Gamma`
     - `response = (v_g / (v_g + s_g)) * sign(v)`.
  4. Auto-gain:
     - `LMS_final = response * Gain`, where `Gain = 1 + Sigma^Gamma`.
  5. Linear transform `LMS_final -> Iab` using the fixed Iab matrix.
  6. Dual-parameter logarithmic chroma compression:
     - `C_in  = sqrt(a^2 + b^2)`
     - `C_out = T1 * log(1 + C_in / T2)`
     - `G     = C_out / C_in`
     - `a' = a * G`, `b' = b * G`.
  7. Inverse pipeline in `sucs_to_xyz` implements the analytic inverse:
     - `C_in = T2 * (exp(C_out / T1) - 1)`, followed by inverse Naka–Rushton and inverse linear transforms.

- **Forward pass in the optimization loop**:
  1. Interpolate control points in `(J, a', b')` to a dense sequence.
  2. Map to sRGB using `NewSUCS.sucs_to_srgb`.
  3. Record the fraction of samples outside `[0, 1]` as a diagnostic (no hard clamp in the latent space).
  4. In the training loop, after each optimizer step, project the a'/b' coordinates back into a bounded range (e.g. ±40 or ±48 based on ab-plane analysis) to keep optimization inside the safe region of the sUCS ball.

- **Output**: `rgb_values` in `[0, 1]` (after any necessary clamping for display).

### Module C: Optimization Loop

- **Initialization**:
  - Build an initial "rainbow" colormap using Matplotlib (256 samples).
  - Extract K = 10 control points at evenly spaced positions.
  - Convert these control points to:
    - Lab → `BaselineCIELAB`,
    - OkLab → `BaselineOkLab`,
    - sUCS → `OursNewSUCS` (via `rgb -> XYZ -> xyz_to_sucs`).

- **Optimizer**: `torch.optim.Adam`, with:
  - `lr = 1e-2` for all three models.

- **Epochs**: typically run for 500 epochs.

- **Loss function (latent-space uniformity)**:
  - For each model:
    1. Upsample control points in its latent space to 256 points.
    2. Compute Euclidean distances between neighbors.
    3. Define the loss as `torch.var(dists)` (variance of distances).

- **Logging per epoch**:
  - For each model:
    - Training loss in latent space.
    - Referee sigma_v and v_min on the current RGB colormap.
    - Gradient norm of control points: `||control_points.grad||`.
    - Fraction of samples outside `[0, 1]` (for baselines, this is pre-clamp; for New sUCS, directly from the raw RGB).
  - Maintain best sigma_v and corresponding RGB colormap for each model.

- **Early stopping for analysis**:
  - At the end of training, the "final" colormap for each model is taken from the epoch with **best** referee sigma_v (not the last epoch).
  - Plots and statistics should be computed from these best checkpoints.

- **Multi-seed robustness**:
  - Optionally run the optimization for many seeds (e.g. 100) and collect best sigma_v per model to build robustness boxplots.

-----

## 4. Expected Deliverables

The code should generate and save the following figures:

### Figure 1: Optimization Performance Comparison

- **File**: `optimization_uniformity.png`
- **Type**: line plot.
- **X-axis**: iteration (0–epochs).
- **Y-axis**: referee uniformity score sigma_v (CIEDE2000).
- **Series**:
  - Baseline CIELAB
  - Baseline OkLab
  - Ours New sUCS (dual-log)
- **Expected behavior**:
  - New sUCS should decrease sigma_v fastest and converge to the lowest best sigma_v.
  - CIELAB should converge more slowly and settle at a higher sigma_v.
  - OkLab may oscillate and be numerically unstable near the gamut boundary.

### Figure 2: Global Speed Matrix

- **File**: `global_speed_matrix.png`
- **Type**: heatmap, similar to Bujack et al., Fig. 1.
- **Content**: 1×3 panel of speed matrices:
  - `V_{i,j} = ΔE(c_i, c_j) / |i - j|`.
- **Expected behavior**:
  - New sUCS should have the smoothest, most homogeneous matrix.
  - CIELAB and OkLab should show more structure and anisotropy.

### Figure 3: 3D Gamut Trajectories

- **File**: `gamut_trajectory_lab.png`
- **Type**: 3D line plot in CIELAB space.
- **Content**:
  1. sRGB gamut boundary (sampled grid).
  2. Trajectories of optimized colormaps for CIELAB, OkLab, New sUCS.
- **Expected behavior**:
  - Baseline trajectories show sharp corners or hugging behavior where they hit the gamut boundary (due to clamping).
  - New sUCS trajectories remain smooth near the boundary due to soft saturation and dual-log chroma compression.

### Figure 4: Colormap Strips and Local Speed

- **File**: `colormap_strips_comparison.png`
- **Content**:
  - Strips for: initial `rainbow`, optimized CIELAB, OkLab, and New sUCS.
  - Under each strip, the local speed curve `d_i` (referee ΔE for neighbors).
- **Expected behavior**:
  - Rainbow has highly oscillatory local speed (poor uniformity).
  - CIELAB improves but still exhibits visible variation.
  - New sUCS has the flattest local speed curve (closest to constant speed).

### Figure 5: Gradient Dynamics

- **File**: `gradient_norm_dynamics.png`
- **Content**:
  - Top: gradient norm of control points vs. iteration.
  - Bottom: fraction of samples outside `[0, 1]` vs. iteration.
- **Expected behavior**:
  - New sUCS gradient norms decay smoothly.
  - CIELAB/OkLab encounter irregularities when hitting clamping boundaries.
  - New sUCS has lower effective boundary contact thanks to its design and a'/b' constraints.

### Figure 6: Mach-Band Tests

- **File**: `mach_band_test.png`
- **Content**:
  - Sine-wave grating and pyramid scalar fields.
  - Each mapped through the three colormaps.
- **Expected behavior**:
  - New sUCS should produce the fewest Mach-band-like artifacts and smoothest level transitions.
  - CIELAB and OkLab may show spurious ridges or false edges near extrema.

### Figure 7: Benchmark Comparison with SOTA Colormaps

- **File**: `benchmark_comparison.png`
- **Content**:
  - Optimization curves for CIELAB, OkLab, New sUCS.
  - Horizontal dashed lines for static SOTA colormaps: Viridis, Magma, Plasma.
- **Expected behavior**:
  - New sUCS converges to a sigma_v band competitive with Magma and Plasma, and clearly better than Rainbow and CIELAB under the same pipeline.

### Figure 8: Robustness Boxplot

- **File**: `robustness_boxplot.png`
- **Content**: boxplots of best sigma_v over multiple random seeds.
- **Expected behavior**:
  - New sUCS exhibits the lowest median best sigma_v and relatively tight spread.
  - CIELAB is consistently worse; OkLab is unstable with large variance.

### Figure 9: ab-Plane Visualizations at Mid Lightness

- **File**: `ab_planes_L50.png`
- **Content**:
  - CIELAB (L* = 50), OkLab (L = 0.5), New sUCS (J = 50) ab-planes rendered in sRGB, with ranges scaled to cover comparable typical step counts.
- **Purpose**:
  - To visualize the geometry of each perceptual space at mid lightness and to identify the safe region of New sUCS where the mapping to sRGB is well-behaved.

### Figure 10: Nonlinearities (Gamma & Chroma)

- **Files**:
  - `gamma_curves_sucs_vs_new.png`
  - `chroma_compression_sucs_vs_new.png`
- **Content**:
  - Comparisons between the original sUCS power-law LMS gamma and the Naka–Rushton curve.
  - Comparisons between the original log chroma compression and the dual-log New sUCS variant.
- **Purpose**:
  - To document how the log-version New sUCS modifies the nonlinearity relative to the original formulation.

-----

## 5. Implementation Hints (Python Pseudocode)

The core optimization loop remains the same as in the base testbed, but `OursNewSUCS` now uses the dual-log New sUCS implementation from `Model.py`. The pseudocode below summarizes the pattern:

```python
optimizer = torch.optim.Adam(
    [
        {"params": model_lab.parameters(), "lr": 1e-2},
        {"params": model_oklab.parameters(), "lr": 1e-2},
        {"params": model_sucs.parameters(), "lr": 1e-2},
    ]
)

for epoch in range(num_epochs):
    optimizer.zero_grad()

    for name, model in models.items():
        rgb_out = model(num_samples=num_samples)

        # Latent-space uniformity loss
        dense_latent = resample_control_points(model.control_points, num_samples)
        dists = torch.norm(dense_latent[1:] - dense_latent[:-1], dim=1)
        loss = torch.var(dists)
        loss.backward()

        # Gradient norm (diagnostic)
        grad = model.control_points.grad
        grad_norm = float(grad.detach().norm().cpu().item()) if grad is not None else 0.0
        history_grad_norm[name].append(grad_norm)

        # Referee metrics (sigma_v, v_min), boundary contact rate, and best sigma_v
        with torch.no_grad():
            rgb_np = rgb_out.detach().cpu().numpy()
            metrics = referee.evaluate(rgb_np)
            history_sigma[name].append(metrics["sigma_v"])
            history_vmin[name].append(metrics["v_min"])
            frac_out = getattr(model, "last_fraction_outside", float("nan"))
            history_fraction_outside[name].append(float(frac_out))

            sigma = metrics["sigma_v"]
            if sigma < best_sigma[name] - 1e-6:
                best_sigma[name] = sigma
                best_epoch[name] = epoch
                best_colormaps[name] = rgb_np.copy()

    optimizer.step()

    # Project New sUCS control points back into a reasonable a'/b' range
    with torch.no_grad():
        if isinstance(model_sucs, OursNewSUCS):
            bound = model_sucs.ab_bound
            model_sucs.control_points.data[:, 1:].clamp_(-bound, bound)
```

-----

## 6. References

Please acknowledge the following references in code comments or documentation where appropriate:

- **Uniformity metric**: Bujack et al., *"The Good, the Bad, and the Ugly"*, IEEE TVCG 2018.
- **Clipping artifacts**: Nardini et al., *"Automatic Improvement of Continuous Colormaps in Euclidean Color Spaces"*, Computer Graphics Forum 2021.
- **OkLab matrices**: Björn Ottosson, 2020.

-----

**Please implement and extend this log-version testbed according to the specification above.**

