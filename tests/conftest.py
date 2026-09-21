import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar.scan import scan_repo  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_REPO = FIXTURES / "sample_repo"


@pytest.fixture(scope="session")
def sample_scan():
    return scan_repo(SAMPLE_REPO)
