"""End-to-end CLI tests — invoke main() the way an engineer actually would,
covering flags that the unit/golden tests don't already exercise directly
(--overwrite guard, --warnings-json, --overrides file wiring, dataform+split
mode's config-block branch).
"""

import json
import subprocess
import sys
from pathlib import Path

from conftest import FLOWS_DIR

SCRIPT = Path(__file__).parent.parent / "Code" / "tfl_to_sql.py"
FLOW = FLOWS_DIR / "FST Segmentation Prep.tfl"


def run_cli(args, cwd=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, cwd=cwd,
    )


def test_missing_flow_file_exits_nonzero():
    result = run_cli(["/no/such/file.tfl"])
    assert result.returncode != 0
    assert "ERROR: File not found" in result.stdout + result.stderr


def test_overwrite_guard_skips_existing_file_without_flag(tmp_path):
    out = tmp_path / "out"
    run_cli([str(FLOW), "--mode", "bigquery", "--out", str(out)])
    result = run_cli([str(FLOW), "--mode", "bigquery", "--out", str(out)])
    assert "already exists, skipping" in result.stdout


def test_overwrite_flag_replaces_existing_file(tmp_path):
    out = tmp_path / "out"
    run_cli([str(FLOW), "--mode", "bigquery", "--out", str(out)])
    result = run_cli([str(FLOW), "--mode", "bigquery", "--out", str(out), "--overwrite"])
    assert "already exists" not in result.stdout
    assert "Generated 1 combined file" in result.stdout


def test_warnings_json_is_written_and_well_formed(tmp_path):
    out = tmp_path / "out"
    warnings_path = tmp_path / "warnings.json"
    result = run_cli([
        str(FLOW), "--mode", "bigquery", "--out", str(out), "--overwrite",
        "--warnings-json", str(warnings_path),
    ])
    assert result.returncode == 0
    assert warnings_path.exists()
    report = json.loads(warnings_path.read_text())
    assert "summary" in report and "flagged_items" in report
    assert report["summary"]["flagged_item_count"] == len(report["flagged_items"])


def test_missing_overrides_file_errors_clearly(tmp_path):
    result = run_cli([str(FLOW), "--out", str(tmp_path / "out"), "--overrides", "/no/such/overrides.json"])
    assert result.returncode != 0
    assert "Overrides file not found" in result.stdout + result.stderr


def test_overrides_file_resolves_parameters(tmp_path):
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps({
        "parameters": {
            "Parameters.192a3229-d1ff-47b9-ae47-9a0b0c718cbd": "208",
            "Parameters.382690fa-a3cc-404a-8d4e-7305f732b199": "104",
            "Parameters.03a2fddc-fdf5-4ecb-b9c2-aaf6f6d2abb1": "52",
            "Parameters.e7fc4ad8-153b-4cee-b575-c22753db54bb": "156",
            "Parameters.423040cf-51b5-466a-8017-344aa9c98a88": "260",
        }
    }))
    out = tmp_path / "out"
    result = run_cli([
        str(FLOW), "--mode", "bigquery", "--out", str(out), "--overwrite",
        "--overrides", str(overrides),
    ])
    assert result.returncode == 0
    assert "parameter reference" not in result.stdout
    generated = (out / "output_2.sql").read_text()
    assert "Parameters." not in generated


def test_dataform_split_mode_emits_config_blocks(tmp_path):
    out = tmp_path / "out"
    result = run_cli([str(FLOW), "--mode", "dataform", "--split", "--out", str(out), "--overwrite"])
    assert result.returncode == 0
    input_file = next(out.glob("01_*.sqlx"))
    assert 'type: "declaration"' in input_file.read_text()
    clean_file = next(f for f in out.glob("*.sqlx") if "clean" in f.name.lower() or "filters" in f.name.lower())
    assert 'config {' in clean_file.read_text()
