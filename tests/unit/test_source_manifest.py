import hashlib
from pathlib import Path

import pytest


@pytest.mark.unit
def test_authoritative_sources_match_manifest() -> None:
    root = Path(__file__).resolve().parents[2]
    rows = (root / "docs/source-manifest.sha256").read_text().splitlines()
    for row in rows:
        expected, relative_path = row.split(maxsplit=1)
        source = root / relative_path
        if not source.exists():
            source = root / "docs" / relative_path
        actual = hashlib.sha256(source.read_bytes()).hexdigest()
        assert actual == expected, f"Contract review required for {relative_path}"
