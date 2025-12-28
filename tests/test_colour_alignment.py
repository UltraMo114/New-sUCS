import numpy as np


def test_oklab_matches_colour_science():
    import colour
    import sucs

    rng = np.random.default_rng(1234)
    rgb = rng.random((256, 3))

    ours = sucs.rgb_to_oklab_np(rgb)
    xyz = sucs.srgb_to_linear_np(rgb) @ sucs._SRGB_TO_XYZ.T
    ref = colour.XYZ_to_Oklab(xyz)

    assert np.allclose(ours, ref, atol=2e-4, rtol=0.0)


def test_oklab_inverse_matches_colour_science():
    import colour
    import sucs

    rng = np.random.default_rng(1234)
    rgb = rng.random((256, 3))
    xyz = sucs.srgb_to_linear_np(rgb) @ sucs._SRGB_TO_XYZ.T
    oklab = colour.XYZ_to_Oklab(xyz)

    import torch

    ours_linear = sucs.oklab_to_linear_srgb_torch(torch.from_numpy(oklab)).numpy()

    xyz_ref = colour.Oklab_to_XYZ(oklab)
    ref_linear = colour.XYZ_to_RGB(
        xyz_ref,
        colour.RGB_COLOURSPACES["sRGB"],
        illuminant_XYZ=colour.CCS_ILLUMINANTS["CIE 1931 2 Degree Standard Observer"][
            "D65"
        ],
        illuminant_RGB=colour.CCS_ILLUMINANTS["CIE 1931 2 Degree Standard Observer"][
            "D65"
        ],
        chromatic_adaptation_transform=None,
        apply_cctf_encoding=False,
    )

    assert np.allclose(ours_linear, ref_linear, atol=1e-3, rtol=0.0)


def test_jzazbz_matches_colour_science():
    import colour.models.jzazbz as jz
    import sucs

    rng = np.random.default_rng(4321)
    # Avoid invalid XYZ that yields negative LMS before PQ (colour-science will
    # return extreme values there). Sample via BT.2020 RGB to stay physical.
    rgb_bt2020 = rng.random((256, 3))
    xyz = rgb_bt2020 @ sucs.BT2020_RGB_TO_XYZ.T * 1000.0

    ours = sucs.xyz_to_jzazbz_np(xyz)
    ref = jz.XYZ_to_Jzazbz(xyz)

    assert np.allclose(ours, ref, atol=1e-6, rtol=0.0)


def test_jzazbz_inverse_matches_colour_science():
    import colour.models.jzazbz as jz
    import sucs

    rng = np.random.default_rng(4321)
    rgb_bt2020 = rng.random((256, 3))
    xyz = rgb_bt2020 @ sucs.BT2020_RGB_TO_XYZ.T * 1000.0
    coords = jz.XYZ_to_Jzazbz(xyz)

    import torch

    ours = sucs.jzazbz_to_xyz_torch(torch.from_numpy(coords)).numpy()
    ref = jz.Jzazbz_to_XYZ(coords)

    assert np.allclose(ours, ref, atol=2e-4, rtol=0.0)


def test_ictcp_matches_colour_science():
    import colour
    import torch

    from hdr_gradient_workspace.src.color_spaces import ICtCpAdapter

    rng = np.random.default_rng(2025)
    # Sample via BT.2020 RGB to avoid negative LMS / PQ invalid inputs.
    rgb_bt2020 = rng.random((256, 3))
    import sucs

    xyz = rgb_bt2020 @ sucs.BT2020_RGB_TO_XYZ.T * 1000.0

    adapter = ICtCpAdapter(device="cpu")
    ours = adapter.xyz_to_coords(torch.from_numpy(xyz)).numpy()
    ref = colour.XYZ_to_ICtCp(xyz, method="Dolby 2016", L_p=10000)

    assert np.allclose(ours, ref, atol=2e-6, rtol=0.0)


def test_ictcp_inverse_matches_colour_science():
    import colour
    import torch

    from hdr_gradient_workspace.src.color_spaces import ICtCpAdapter

    rng = np.random.default_rng(2025)
    rgb_bt2020 = rng.random((256, 3))
    import sucs

    xyz = rgb_bt2020 @ sucs.BT2020_RGB_TO_XYZ.T * 1000.0
    coords = colour.XYZ_to_ICtCp(xyz, method="Dolby 2016", L_p=10000)

    adapter = ICtCpAdapter(device="cpu")
    ours = adapter.coords_to_xyz(torch.from_numpy(coords)).numpy()
    ref = colour.ICtCp_to_XYZ(coords, method="Dolby 2016", L_p=10000)

    assert np.allclose(ours, ref, atol=2e-4, rtol=0.0)
