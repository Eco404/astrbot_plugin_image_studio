from __future__ import annotations

import sys
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
ASTRBOT_ROOT = WORKSPACE_ROOT / "AstrBot"
for root in (WORKSPACE_ROOT, ASTRBOT_ROOT):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
