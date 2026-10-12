"""Keep pytest working files in the checkout or the configured TMPDIR."""
import os
from pathlib import Path
from uuid import uuid4


def pytest_configure(config):
    if config.option.basetemp is None:
        root = Path(os.environ.get("TMPDIR", Path(__file__).resolve().parents[1] / ".tmp"))
        root.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(root / f"pytest-{uuid4().hex[:10]}")
