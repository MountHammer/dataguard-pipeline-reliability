"""Run the complete DataGuard pipeline.

Usage (from the project root):
    python scripts/run_pipeline.py
    python scripts/run_pipeline.py --source data/raw --database database/big.duckdb
    python scripts/run_pipeline.py --help
"""

import sys
from pathlib import Path

# Make the src/ folder importable without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataguard.pipeline.run import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
