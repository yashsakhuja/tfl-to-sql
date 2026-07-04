import sys
from pathlib import Path

_CODE_DIR = Path(__file__).parent.parent / "Code"
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))

_APP_DIR = Path(__file__).parent.parent / "App"
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

FLOWS_DIR = Path(__file__).parent.parent / "Tableau Prep Flows"
GOLDEN_DIR = Path(__file__).parent / "golden"


import pytest  # noqa: E402
import tfl_to_sql as t  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_engine_globals():
    """Every test starts from a clean slate — PARSE_WARNINGS/OVERRIDES/SCHEMA
    are module-level state so one test's --overrides or a parse failure can't
    bleed into the next test."""
    t.PARSE_WARNINGS.clear()
    t.OVERRIDES['expressions'].clear()
    t.OVERRIDES['parameters'].clear()
    t.OVERRIDES['bulk_renames'].clear()
    t.SCHEMA.clear()
    t.TRANSLATE_ATTEMPTS = 0
    yield
