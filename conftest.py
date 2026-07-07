"""Pytest bootstrap: put the project root on sys.path so `app` and `config` import."""
import sys
from pathlib import Path

_ROOT = Path(__file__).parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
