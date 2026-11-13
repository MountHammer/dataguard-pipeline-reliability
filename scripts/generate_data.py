"""Generate DataGuard synthetic CSV files.

Usage (from the project root):
    python scripts/generate_data.py --preset sample --out data/sample
    python scripts/generate_data.py --preset perf-100k --out data/raw --overwrite
"""

import sys
from pathlib import Path

# Make the src/ folder importable without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataguard.generator.generate import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
