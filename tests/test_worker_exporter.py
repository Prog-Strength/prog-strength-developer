from pathlib import Path

from bootstrap.worker_exporter import (
    ExporterState,
    parse_jsonl_events,
    read_prs_opened,
    read_state_file,
)


FIXTURE = Path(__file__).parent / "fixtures" / "sample.jsonl"


def test_parse_jsonl_events_counts_tool_uses():
    state = ExporterState()
    for line in FIXTURE.read_text().splitlines():
        parse_jsonl_events(state, line)
    assert state.tool_calls == {"Read": 2, "Bash": 1}


def test_parse_jsonl_events_counts_messages_by_role():
    state = ExporterState()
    for line in FIXTURE.read_text().splitlines():
        parse_jsonl_events(state, line)
    assert state.messages == {"assistant": 4, "user": 1}


def test_parse_jsonl_events_ignores_malformed_lines():
    state = ExporterState()
    parse_jsonl_events(state, "not json")
    parse_jsonl_events(state, '{"type":"assistant"}')  # missing message; role still counts
    parse_jsonl_events(state, "")
    assert state.tool_calls == {}
    assert state.messages == {"assistant": 1}


def test_state_file_read_returns_default_on_missing(tmp_path):
    assert read_state_file(tmp_path / "missing") == "booting"


def test_state_file_read_returns_contents(tmp_path):
    p = tmp_path / "state"
    p.write_text("working\n")
    assert read_state_file(p) == "working"


def test_state_file_unknown_value_falls_back_to_booting(tmp_path):
    p = tmp_path / "state"
    p.write_text("garbage\n")
    assert read_state_file(p) == "booting"


def test_read_prs_opened_returns_zero_on_missing(tmp_path):
    assert read_prs_opened(tmp_path / "missing") == 0


def test_read_prs_opened_returns_int(tmp_path):
    p = tmp_path / "prs"
    p.write_text("3\n")
    assert read_prs_opened(p) == 3
