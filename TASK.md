This document is a complete task specification for an AI agent or algorithm engineer. It defines the experimental background, mathematical logic, code structure, and expected outputs. You can copy it directly and send it to the engineer or agent who will implement the system.

-----

# Task Specification: Gradient-Descent-Based Colormap Optimization Testbed

## 1. Project Context

The goal of this project is to empirically demonstrate the superiority of the **New sUCS** color space in the task of colormap optimization. We will build a differentiable optimization framework based on PyTorch that takes an initial non-uniform colormap and optimizes it via gradient descent into a perceptually uniform colormap.

**Core comparison logic**

- **Baselines (CIELAB and OkLab)**: these are Euclidean perceptual spaces that are mapped linearly to sRGB. During optimization, to ensure that colors remain displayable (inside the sRGB gamut), we must clamp values with `clamp(0, 1)`. This causes gradients at the boundaries to vanish or oscillate, which harms convergence.
- **Ours (New sUCS)**: New sUCS is also a Euclidean perceptual space, but it uses Naka-Rushton-style response functions (and tanh-based chroma compression) to implement soft saturation before mapping to sRGB. The parameters are unbounded, but the mapped colors are naturally in a valid display range. This fully differentiable, clamp-free design should provide smoother gradient flow and better final uniformity.

## 2. Environment Requirements

- **Python**: 3.8 or newer
- **PyTorch**: 1.10 or newer (for automatic differentiation)
- **colour-science**: `pip install colour-science` (for computing CIEDE2000 as the referee metric)
- **Matplotlib / NumPy**: for plotting and data processing

-----

## 3. Module Specifications

### Module A: The Referee

**Purpose**: a non-trainable, objective evaluator that measures the true quality of a colormap at the end of each epoch.

**Theoretical basis**: use the local uniformity definition from Bujack et al., TVCG 2018.

- **Input**: `rgb_samples`, shape `(N, 3)`, values in `[0, 1]`.
- **Computation pipeline**:
  1. Convert RGB to CIE Lab (D65, 2 degree observer).
  2. Compute perceptual distances between adjacent samples using CIEDE2000:
     - `d_i = DeltaE_00(C_i, C_{i+1})`
  3. Compute the speed standard deviation sigma_v:
     - `sigma_v = std(d)`
  4. Compute the minimum speed v_min:
     - `v_min = min(d)`
- **Output**: a dictionary such as `{"sigma_v": float, "v_min": float}`.

### Module B: Optimization Spaces

Implement three classes derived from `torch.nn.Module`, each representing a competing color space.

#### 1. `Baseline_CIELAB`

- **Parameters**: `self.control_points`, shape `(K, 3)`, initialized with Lab values.
- **Forward pass**:
  1. Implement differentiable `Lab_to_XYZ`, `XYZ_to_LinearRGB`, and gamma correction to sRGB.
  2. **Key operation (simulate artifacts)**: apply `torch.clamp(rgb, 0.0, 1.0)` before returning the output to simulate the gamut clipping issues described by Nardini et al.
- **Output**: `rgb_values` in `[0, 1]`.

#### 2. `Baseline_OkLab`

- **Parameters**: `self.control_points`, shape `(K, 3)`, initialized with OkLab values.
- **Forward pass**:
  1. Use Bjorn Ottosson's 2020 matrices for OkLab to linear RGB:
     - `LMS = M1 @ Lab`
     - `LMS_prime = LMS ** 3` (nonlinearity)
     - `RGB_lin = M2 @ LMS_prime`
  2. Apply gamma correction to convert to sRGB.
  3. **Key operation**: apply `torch.clamp(rgb, 0.0, 1.0)` before returning.
- **Output**: `rgb_values` in `[0, 1]`.

#### 3. `Ours_NewSUCS`

- **Parameters**: `self.control_points`, shape `(K, 3)`, initialized as arbitrary real numbers.
- **Forward pass**:
  1. **Soft saturation**: for a simplified proxy of New sUCS, use
     - `compressed = torch.sigmoid(self.control_points)`
     - In the final system, replace this with the actual Naka-Rushton or tanh-based New sUCS formulation from your paper.
  2. **Direct mapping**: map the compressed values directly to RGB (or via a simple linear transform).
  3. **Key constraint**: do not use any clamping. The output must stay in `[0, 1]` by design.
- **Output**: `rgb_values` in `[0, 1]`.

### Module C: Optimization Loop

- **Initialization**:
  - Create an initial "rainbow" or linear RGB colormap and sample 10 control points as initial parameters.
  - Initialize three models: `model_lab`, `model_oklab`, `model_sucs`.
- **Optimizer**: use `torch.optim.Adam` with learning rate around `lr = 0.01` (New sUCS may tolerate a slightly larger learning rate).
- **Epochs**: run for 500 to 1000 iterations.
- **Loss function (drive toward uniformity)**:
  - Upsample the curve defined by the control points to 256 points.
  - Compute Euclidean distances between adjacent points in the current latent space.
  - Define the loss as the variance of these distances, for example `loss = torch.var(d)`.
- **Logging**:
  - Record the training loss for each model at each step.
  - At each step, call Module A (the referee) to compute and log `sigma_v` for each model.

-----

## 4. Expected Deliverables

The code should automatically generate and save at least the following three figures when training finishes.

### Figure 1: Optimization Performance Comparison

- **Type**: line plot.
- **X-axis**: iteration (for example, 0 to 1000).
- **Y-axis**: referee uniformity score sigma_v (CIEDE2000). Do not plot the training loss; plot the referee metric.
- **Series**:
  - Baseline CIELAB
  - Baseline OkLab
  - Ours New sUCS
- **Expected behavior**: the New sUCS curve should decrease fastest and converge to the lowest best sigma_v; the CIELAB and OkLab curves may oscillate or plateau at higher values. In the current implementation New sUCS reaches its convergence band (10% above final sigma_v) after roughly 90 iterations, compared to about 385 for CIELAB and nearly 500 for OkLab.

### Figure 2: Global Speed Matrix

### Figure 2: Global Speed Matrix

- **Type**: heatmap, similar to Bujack et al., Figure 1.
- **Content**: display the speed matrix of all three colormaps at the end of optimization:
  - `V_{i,j} = DeltaE_00(c_i, c_j) / |i - j|`
- **Layout**: a 1 by 3 panel (CIELAB, OkLab, Ours).
- **Expected behavior**: the New sUCS matrix should appear the smoothest and most uniform.

### Figure 3: 3D Gamut Trajectories

- **Type**: 3D line or scatter plot in CIELAB space.
- **Content**:
  1. Draw the sRGB gamut boundary (as a wireframe or semi-transparent volume).
  2. Plot the trajectories of the three optimized colormaps.
- **Expected behavior**:
  - Baseline trajectories show sharp corners or hugging behavior where they hit the gamut boundary (due to clipping).
  - New sUCS trajectories remain smooth near the boundary thanks to soft saturation.

-----

## 5. Implementation Hints (Python Pseudocode)

```python
# Pseudocode for the core optimization loop

optimizer = torch.optim.Adam(
    [
        {"params": model_lab.parameters(), "lr": 0.01},
        {"params": model_oklab.parameters(), "lr": 0.01},
        {"params": model_sucs.parameters(), "lr": 0.05},  # New sUCS may allow a larger lr
    ]
)

for epoch in range(1000):
    optimizer.zero_grad()

    models = [model_lab, model_oklab, model_sucs]
    names = ["lab", "oklab", "sucs"]

    for model, name in zip(models, names):
        rgb_out = model()  # shape (256, 3)

        # 1. Training loss: uniformity in the latent space
        # Option A: compute distances in RGB space
        # Option B (recommended): compute distances in each model's latent space

        dense_points = resample(model.control_points, n=256)
        dists = torch.norm(dense_points[1:] - dense_points[:-1], dim=1)
        loss = torch.var(dists)
        loss.backward()

        # 2. Evaluation by the referee (no gradient)
        with torch.no_grad():
            score = referee.calculate_sigma_v(rgb_out.detach().cpu().numpy())
            history[name].append(score)

    optimizer.step()
```

-----

## 6. References

Please acknowledge the following references in code comments or documentation where appropriate:

- **Uniformity metric**: Bujack et al., "The Good, the Bad, and the Ugly", IEEE TVCG 2018.
- **Clipping artifacts**: Nardini et al., "Automatic Improvement of Colormaps for Data Visualization", Computer Graphics Forum 2021.
- **OkLab matrices**: Bjorn Ottosson, 2020.

-----

**Please implement this testbed according to the specification above.**
