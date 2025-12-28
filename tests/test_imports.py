def test_imports_smoke():
    # 仅做最小 smoke test：确保核心依赖可导入
    import numpy  # noqa: F401
    import scipy  # noqa: F401
    import matplotlib  # noqa: F401
    import torch  # noqa: F401
    import colour  # noqa: F401

