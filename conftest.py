"""Root-level pytest conftest.

Ensures the repository root is on sys.path so ``tests/test_version_consistency.py``
can import ``scripts.check_version`` regardless of how pytest is invoked
(e.g. plain ``pytest`` without ``PYTHONPATH=.`` set).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
