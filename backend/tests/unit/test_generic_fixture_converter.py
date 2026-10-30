# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Reproducibility and execution-level mapping of the reference converter."""

import subprocess
import sys
from pathlib import Path

import pytest

from app.utils.parsers.generic import GenericParser
from app.utils.parsers.ibkr_xml import IBKRXMLParser
from app.utils.parsers.schwab_json import SchwabJSONParser

_ROOT = Path(__file__).resolve().parents[3]
_DATA = _ROOT / "tests" / "test_data"
_CASES = [
    ("ibkr", "TraderJournal_anonymized_combined.xml", "ibkr_combined"),
    ("ibkr", "combo_positions.xml", "ibkr_combo_positions"),
    ("schwab", "schwab_aggregation.json", "schwab_aggregation"),
    ("schwab", "schwab_short_roundtrip.json", "schwab_short_roundtrip"),
]


@pytest.mark.unit
@pytest.mark.parametrize(("broker", "source_name", "generic_stem"), _CASES, ids=[case[2] for case in _CASES])
def test_converter_reproduces_committed_fixtures_byte_for_byte(tmp_path: Path, broker: str, source_name: str, generic_stem: str) -> None:
    """Both deterministic outputs must also preserve every native DTO field."""
    source = _DATA / broker / source_name
    native_parser = IBKRXMLParser() if broker == "ibkr" else SchwabJSONParser()
    native, actions = native_parser.parse_raw(source.read_bytes(), source.name)
    assert not actions
    excluded = {"id", "broker_type", "raw_data", "user_id", "account_id"}
    expected = [row.model_dump(exclude=excluded) for row in sorted(native, key=lambda row: (row.executed_at, row.id))]
    first_outputs: dict[str, bytes] = {}
    for _run in range(2):
        subprocess.run(
            [
                sys.executable,
                str(_ROOT / "tools" / "convert_to_generic.py"),
                str(source),
                "--broker",
                broker,
                "--output-stem",
                str(tmp_path / generic_stem),
            ],
            check=True,
            capture_output=True,
            cwd=_ROOT,
        )
        for encoding in ("csv", "json"):
            filename = f"{generic_stem}.{encoding}"
            generated = (tmp_path / filename).read_bytes()
            assert generated == (_DATA / "generic" / filename).read_bytes()
            assert generated == first_outputs.setdefault(encoding, generated)
            parser = GenericParser()
            converted, converted_actions = parser.parse_raw(generated, filename)
            assert not parser.warnings
            assert not converted_actions
            assert [row.model_dump(exclude=excluded) for row in converted] == expected
