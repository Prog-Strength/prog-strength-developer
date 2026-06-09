"""
worker_exporter — Prometheus exporter for prog-strength-developer-worker.

Exposes on :9101:
  developer_worker_info{sow,instance_id,started_at}      gauge=1
  developer_worker_state{state}                          gauge=1 for active state
  developer_worker_uptime_seconds                        gauge
  developer_claude_tool_calls_total{tool}                counter
  developer_claude_messages_total{role}                  counter
  developer_prs_opened_total                             counter

State source: /var/run/developer-worker/state (single token: booting,
cloning, working, opening_prs, terminating). Updated by the worker
userdata at each lifecycle transition. Unknown values fall back to
"booting" so a stale or garbled state file does not poison the gauge.

Tool-call and message counters: tail every
~/.claude/projects/<slug>/<uuid>.jsonl on disk and increment per
matching event. The renderer sidecar at
/var/log/prog-strength-developer/claude-pretty.log is the operator-
facing rendering of the SAME data; this exporter is the metric-facing
rendering.

Designed to be importable for tests (parse_jsonl_events, read_state_file,
read_prs_opened) and runnable as a script (python3 worker_exporter.py).
"""

from __future__ import annotations

import glob
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    start_http_server,
)


STATE_VALUES = ("booting", "cloning", "working", "opening_prs", "terminating")
JSONL_GLOB = "/home/developer/.claude/projects/*/*.jsonl"
STATE_FILE = Path("/var/run/developer-worker/state")
PRS_FILE = Path("/var/run/developer-worker/prs_opened")


@dataclass
class ExporterState:
    tool_calls: Dict[str, int] = field(default_factory=dict)
    messages: Dict[str, int] = field(default_factory=dict)
    file_offsets: Dict[str, int] = field(default_factory=dict)


def parse_jsonl_events(state: ExporterState, line: str) -> None:
    """Parse one JSONL event line and update counters in-place."""
    line = line.strip()
    if not line:
        return
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return
    role = event.get("type")
    if role not in ("assistant", "user"):
        return
    state.messages[role] = state.messages.get(role, 0) + 1
    msg = event.get("message")
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "tool_use":
            tool = item.get("name") or "unknown"
            state.tool_calls[tool] = state.tool_calls.get(tool, 0) + 1


def read_state_file(path: Path) -> str:
    try:
        value = Path(path).read_text().strip()
    except FileNotFoundError:
        return "booting"
    return value if value in STATE_VALUES else "booting"


def read_prs_opened(path: Path) -> int:
    try:
        return int(Path(path).read_text().strip() or "0")
    except (FileNotFoundError, ValueError):
        return 0


def tail_jsonl(state: ExporterState) -> None:
    """Read any new bytes appended to known JSONL files since last call."""
    for filepath in glob.glob(JSONL_GLOB):
        try:
            offset = state.file_offsets.get(filepath, 0)
            with open(filepath) as f:
                f.seek(offset)
                for line in f:
                    parse_jsonl_events(state, line)
                state.file_offsets[filepath] = f.tell()
        except OSError:
            continue


def main() -> None:
    sow = os.environ.get("SOW_PATH", "unknown")
    instance_id = os.environ.get("INSTANCE_ID", "unknown")
    started_at = os.environ.get("STARTED_AT", str(int(time.time())))

    registry = CollectorRegistry()
    info = Gauge(
        "developer_worker_info",
        "Worker identity. Always 1.",
        labelnames=["sow", "instance_id", "started_at"],
        registry=registry,
    )
    state_g = Gauge(
        "developer_worker_state",
        "1 for the active worker lifecycle state, 0 otherwise.",
        labelnames=["state"],
        registry=registry,
    )
    uptime = Gauge(
        "developer_worker_uptime_seconds",
        "Seconds since the worker booted.",
        registry=registry,
    )
    tool_calls = Counter(
        "developer_claude_tool_calls_total",
        "Claude tool invocations, grouped by tool name.",
        labelnames=["tool"],
        registry=registry,
    )
    messages = Counter(
        "developer_claude_messages_total",
        "Claude messages, grouped by role.",
        labelnames=["role"],
        registry=registry,
    )
    prs_opened = Counter(
        "developer_prs_opened_total",
        "Pull requests opened so far by this worker.",
        registry=registry,
    )

    info.labels(sow=sow, instance_id=instance_id, started_at=started_at).set(1)

    start_http_server(9101, registry=registry)
    print("worker_exporter listening on :9101", file=sys.stderr)

    state_obj = ExporterState()
    last = {"tool_calls": {}, "messages": {}, "prs_opened": 0}
    started = float(started_at)

    while True:
        current = read_state_file(STATE_FILE)
        for s in STATE_VALUES:
            state_g.labels(state=s).set(1 if s == current else 0)

        uptime.set(time.time() - started)

        tail_jsonl(state_obj)

        for tool, count in state_obj.tool_calls.items():
            prev = last["tool_calls"].get(tool, 0)
            if count > prev:
                tool_calls.labels(tool=tool).inc(count - prev)
        last["tool_calls"] = dict(state_obj.tool_calls)

        for role, count in state_obj.messages.items():
            prev = last["messages"].get(role, 0)
            if count > prev:
                messages.labels(role=role).inc(count - prev)
        last["messages"] = dict(state_obj.messages)

        prs_now = read_prs_opened(PRS_FILE)
        if prs_now > last["prs_opened"]:
            prs_opened.inc(prs_now - last["prs_opened"])
        last["prs_opened"] = prs_now

        time.sleep(5)


if __name__ == "__main__":
    main()
