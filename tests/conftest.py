"""Shared test configuration — adds the project root to sys.path once."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
