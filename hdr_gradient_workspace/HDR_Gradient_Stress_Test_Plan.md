# HDR Gradient Stress Test Plan

## Objectives
- Demonstrate that sUCS maintains hue linearity and numerical stability while compressing 4000-nit, BT.2020-edge primaries into the SDR (Rec.709) gamut.
- Benchmark the optimisation behaviour of sUCS against JzAzBz and ICtCp using identical HDR stimuli and photometric constraints.
- Produce two publication-grade artefacts: a hue linearity trajectory plot and a gradient-landscape contour map that exposes optimiser stability.

## Experimental Design
1. **Stimulus synthesis**  
   - Generate ≥8 HDR primaries spanning complementary hues on the BT.2020 boundary, each with luminance anchors at 1000, 2000, and 4000 nits.  
   - Convert to XYZ via BT.2020 primaries, then to each colour space (sUCS, JzAzBz, ICtCp) with consistent absolute luminance handling.
2. **Optimisation protocol**  
   - Run gradient descent per stimulus to find the closest Rec.709 representation under ΔE\* (CAM16-UCS referee metric) with luminance penalty λ tuned to enforce SDR white (≈100 nits).  
   - For gradient-landscape diagnostics, also minimise each space’s native metric (Distance\_sUCS, Distance\_JzAzBz, Distance\_ICtCp) to expose how their own losses behave.  
   - Use shared optimiser hyper-parameters (Adam, lr 5e-3, 500 iters) and log loss gradients, Hessian approximations, and convergence curves.
3. **Visual analysis**  
   - Hue linearity: track the chromaticity path as HDR points collapse into SDR, plotting hue angle vs. iteration to show sUCS straightness versus JzAzBz curvature.  
   - Gradient landscape: evaluate the 2D loss surface around the optimum (ΔL vs. ΔC) for each colour space under its own metric; quantify condition numbers to contrast convexity vs. cliffs.  
   - Hue-shift ramp: apply a shared global sigmoid tone mapper to high-saturation HDR ramps (e.g., blue and red) in each space, project back to sRGB, and illustrate how sUCS preserves hue while JzAzBz/ICtCp drift (Abney effect).

## Implementation Steps
1. Build `hdr_test.py` scaffold with CLI arguments for HDR target definition, colour-space selection, optimiser settings, and output paths.
2. Implement conversion utilities (BT.2020/Rec.709 XYZ matrices, luminance scaling, sUCS/JzAzBz/ICtCp forward and inverse transforms).
3. Encode the loss: SDR gamut penalty + hue preservation term + luminance anchoring; expose hooks to dump gradients per iteration.
4. Write plotting helpers for trajectory overlays, contour heatmaps, and sigmoid tone-mapping ramp comparisons; ensure reproducible colour annotations (e.g., Matplotlib with fixed palettes).
5. Validate numerics using lower-luminance smoke tests before escalating to 4000-nit cases; confirm no NaNs/Inf via assertions.
6. Automate experiment batches (e.g., `python hdr_test.py --space sucs --all-stimuli`) and collect JSON/CSV summaries for the paper.

## Risk & Mitigation
- **Extreme dynamic-range overflow**: clamp intermediate luminance after matrix transforms; use `float64` to reduce rounding artefacts.
- **Optimiser divergence**: apply gradient clipping (‖g‖₂ ≤ 50) and fallback line search if loss increases for >5 iterations.
- **Visualization ambiguity**: normalise axes across spaces and embed identical reference lines to ensure fair qualitative comparison.

## Deliverables & Effort
- `hdr_test.py` script, supporting utilities, CSV/JSON logs, and two figures (hue linearity, gradient landscape) suitable for Evaluation 3.  
- Estimated effort: ~0.5–0.75 workday for a first complete pass, assuming existing sUCS/JzAzBz primitives are reusable.
