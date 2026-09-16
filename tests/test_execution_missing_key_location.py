"""Locate a reported missing key in model-owned source without releasing its value."""

import pytest

from labbioagentos import DockerExecutor
from test_phase6_docker_execution import _synthetic_traceback


def test_real_multi_key_failure_identifies_only_the_missing_source_literal(monkeypatch):
    source = (
        "import pandas as pd\n"
        "frame = pd.DataFrame({'label': [1, 2, 3]})\n"
        "frame.groupby(['bucket', 'label']).size()\n"
    )
    stderr = _synthetic_traceback(source, monkeypatch)
    item = DockerExecutor._safe_python_diagnostics(stderr, script_content=source)[0]
    assert item.missing_key_type == "str"
    assert len(item.missing_key_source_locations) == 1
    location = item.missing_key_source_locations[0]
    assert location.line_number == 3
    line = source.splitlines()[location.line_number - 1]
    assert line[location.start_column:location.end_column] == "'bucket'"
    assert 'bucket' not in item.model_dump_json()
    assert 'label' not in item.model_dump_json()


@pytest.mark.parametrize("source", [
    "key = 'PRIVATE_UNKNOWN'\n{}[key]\n",
    "data = {'allowed': 1}\ndata['different']\n",
])
def test_key_not_literal_in_verified_failure_expression_has_no_location(monkeypatch, source):
    stderr = _synthetic_traceback(source, monkeypatch)
    if "different" in source:
        # A forged/different displayed line cannot release a source match.
        source = source.replace("different", "PRIVATE_CHANGED")
    item = DockerExecutor._safe_python_diagnostics(stderr, script_content=source)[0]
    assert item.missing_key_source_locations == ()
    assert 'PRIVATE' not in item.model_dump_json()


def test_literal_disambiguation_is_not_limited_to_groupby_or_string_keys(monkeypatch):
    source = "first = {7: 'ok'}\n(first[7], first[42])\n"
    item = DockerExecutor._safe_python_diagnostics(
        _synthetic_traceback(source, monkeypatch), script_content=source,
    )[0]
    location, = item.missing_key_source_locations
    assert source.splitlines()[1][location.start_column:location.end_column] == "42"
    assert item.missing_key_type == "int"
