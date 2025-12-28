import sys
from pathlib import Path


# Ensure repo root (containing `sucs.py`) is importable when pytest is invoked
# from environments that do not automatically add it to `sys.path`.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

