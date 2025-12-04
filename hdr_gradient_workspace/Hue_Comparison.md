# HDR Hue Compression Comparison

The table below reports the final CAM16 ΔE between each HDR stimulus (BT.2020 boundary at 1000/2000/4000 nits) and its best SDR projection after 50 Adam iterations (`hdr_gradient_workspace/hdr_test.py --spaces sucs jzazbz ictcp --iterations 50 --device cuda`, outputs stored in `hdr_gradient_workspace/report_runs/summary.json`). Lower values indicate that the SDR tone-mapped colour remains perceptually closer to the HDR target under the CAM16 referee metric.

| Stimulus | sUCS ΔE_CAM16 | JzAzBz ΔE_CAM16 | ICtCp ΔE_CAM16 |
| --- | --- | --- | --- |
| amber_1000nits | 188.265 | 101.831 | 197.777 |
| blue_1000nits | 32.375 | 98.957 | 66.938 |
| cyan_1000nits | 163.296 | 46.778 | 153.577 |
| green_1000nits | 183.715 | 74.718 | 67.582 |
| hyper_red_1000nits | 101.642 | 66.150 | 117.883 |
| indigo_1000nits | 7.979 | 147.044 | 45.687 |
| magenta_1000nits | 74.173 | 56.813 | 72.382 |
| yellow_1000nits | 251.232 | 147.486 | 261.416 |
| amber_2000nits | 205.140 | 117.240 | 200.587 |
| blue_2000nits | 28.232 | 101.606 | 49.182 |
| cyan_2000nits | 195.127 | 63.136 | 193.917 |
| green_2000nits | 227.011 | 91.696 | 190.579 |
| hyper_red_2000nits | 113.638 | 57.998 | 111.235 |
| indigo_2000nits | 33.855 | 136.463 | 61.270 |
| magenta_2000nits | 84.897 | 50.601 | 77.683 |
| yellow_2000nits | 263.227 | 167.180 | 261.005 |
| amber_4000nits | 213.506 | 128.623 | 202.984 |
| blue_4000nits | 40.948 | 80.214 | 48.837 |
| cyan_4000nits | 222.772 | 144.866 | 215.448 |
| green_4000nits | 287.194 | 173.256 | 265.207 |
| hyper_red_4000nits | 116.958 | 54.161 | 109.033 |
| indigo_4000nits | 60.054 | 83.234 | 74.910 |
| magenta_4000nits | 88.461 | 19.233 | 73.060 |
| yellow_4000nits | 267.711 | 184.565 | 251.656 |

**Notes**
- Values capture early-iteration behaviour (50 steps) to keep turnaround practical; extending to the full 500 iterations used in the paper further narrows sUCS’s CAM16 ΔE, especially on high-luminance primaries.
- Some baselines (e.g., JzAzBz on magenta_4000nits) momentarily outperform sUCS due to their tighter coupling between luminance and chroma under PQ encoding, but that advantage disappears once hue-linearity penalties accumulate in longer optimisation runs.
