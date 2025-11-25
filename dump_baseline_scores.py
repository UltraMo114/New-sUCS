import numpy as np

import matplotlib

matplotlib.use("Agg")

from sucs import Referee, compute_referee_sigma_for_cmap


def main():
    referee = Referee(metric="DE2000")
    num_samples = 256

    scores = {}
    for name in ["viridis", "magma", "plasma"]:
        scores[name] = compute_referee_sigma_for_cmap(name, num_samples, referee)

    for name, sigma in scores.items():
        print(f"{name}: sigma_v={sigma:.6f}")


if __name__ == "__main__":
    main()

