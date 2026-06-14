"""
Regression tests for the worker's end-of-run Pushgateway push
(`finalize_metrics` in bootstrap/userdata.sh.tpl).

The dashboard's run-history / completed-runs / failure-rate panels are all
backed by the `developer_run_*` metrics this function pushes. A push whose
body lacks a trailing newline is rejected by the Pushgateway with HTTP 400
("unexpected end of input stream"), so every run silently vanishes from the
dashboard. These tests render the template the same way the dispatch-sow
workflow does, then actually execute `finalize_metrics` under bash with a
`curl` shim that captures the exact request bytes — asserting the contract
the dashboard depends on, independent of HOW the newline is guaranteed.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "bootstrap" / "userdata.sh.tpl"

# Mirrors the substitutions the dispatch-sow workflow performs at render
# time (see .github/workflows/dispatch-sow.yml "Render userdata").
SUBS = {
    "aws_region": "us-east-2",
    "sow_path": "sows/example.md",
    "github_org": "Prog-Strength",
    "log_group_name": "/aws/ec2/prog-strength-developer",
    "max_runtime_hours": "6",
    "claude_secret_name": "prog-strength-developer/claude-credentials",
    "github_app_secret_name": "prog-strength-developer/github-app",
    "manager_private_ip": "10.20.2.50",
}

INSTANCE_ID = "i-testinstance0001"


def render_template() -> str:
    src = TEMPLATE.read_text()
    for key, value in SUBS.items():
        src = src.replace("${" + key + "}", value)
    # Terraform's $${ escape -> literal ${ that bash sees at runtime.
    src = src.replace("$${", "${")
    return src


def extract_function(src: str, name: str) -> str:
    """Slice a top-level shell function (`name() {` … column-0 `}`)."""
    lines = src.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    for end in range(start + 1, len(lines)):
        if lines[end] == "}":
            return "\n".join(lines[start : end + 1])
    raise AssertionError(f"no closing brace found for {name}()")


def run_finalize(tmp_path: Path, outcome: str = "success") -> tuple[bytes, str]:
    """
    Execute the rendered finalize_metrics() against a fake `curl` that
    records the request. Returns (body_bytes, argv_text).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    body_file = tmp_path / "body.bin"
    args_file = tmp_path / "args.txt"

    # Fake curl: capture argv and the request body verbatim (no newline
    # stripping), handling both `--data-binary @-` (stdin) and
    # `--data-binary <value>` (arg) so the test asserts behaviour, not the
    # specific mechanism used to terminate the body.
    (bin_dir / "curl").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            : > "$PSDEV_CURL_ARGS"
            for a in "$@"; do printf '%s\\n' "$a" >> "$PSDEV_CURL_ARGS"; done
            mode=none; val=""; prev=""
            for a in "$@"; do
              if [ "$prev" = "--data-binary" ]; then
                if [ "$a" = "@-" ]; then mode=stdin; else mode=arg; val="$a"; fi
              fi
              prev="$a"
            done
            if [ "$mode" = stdin ]; then
              cat > "$PSDEV_CURL_BODY"
            elif [ "$mode" = arg ]; then
              printf '%s' "$val" > "$PSDEV_CURL_BODY"
            else
              : > "$PSDEV_CURL_BODY"
            fi
            exit 0
            """
        )
    )
    (bin_dir / "curl").chmod(0o755)

    func = extract_function(render_template(), "finalize_metrics")
    harness = "\n".join(
        [
            "#!/usr/bin/env bash",
            'export PATH="$PSDEV_BIN:$PATH"',
            "log() { :; }",      # silence the function's log() calls
            "sleep() { :; }",    # skip the post-push scrape-settle sleep
            "STARTED_AT=100",
            f"INSTANCE_ID={INSTANCE_ID}",
            func,
            f"finalize_metrics {outcome}",
        ]
    )

    env = {
        **os.environ,
        "PSDEV_BIN": str(bin_dir),
        "PSDEV_CURL_BODY": str(body_file),
        "PSDEV_CURL_ARGS": str(args_file),
    }
    subprocess.run(["bash", "-c", harness], check=True, env=env)
    return body_file.read_bytes(), args_file.read_text()


def test_push_body_is_newline_terminated(tmp_path):
    # The core regression: the Pushgateway rejects a body whose final
    # metric line has no trailing newline.
    body, _ = run_finalize(tmp_path)
    assert body, "finalize_metrics did not send a request body"
    assert body.endswith(b"\n"), (
        "Pushgateway body must end in a newline or the push 400s and the "
        "run is dropped from the dashboard"
    )


def test_push_contains_dashboard_metrics_and_labels(tmp_path):
    body, _ = run_finalize(tmp_path, outcome="success")
    text = body.decode()
    # The three metrics the Developer Platform dashboard queries.
    assert "developer_run_duration_seconds{" in text
    assert "developer_run_finished_at_seconds{" in text
    assert "developer_run_prs_opened{" in text
    # Labels the panels filter / group / sort by.
    assert 'sow="sows/example.md"' in text
    assert 'outcome="success"' in text
    assert 'started_at="100"' in text


def test_push_targets_developer_run_job_and_instance(tmp_path):
    _, argv = run_finalize(tmp_path)
    assert (
        f"http://10.20.2.50:9091/metrics/job/developer_run/instance/{INSTANCE_ID}"
        in argv
    )
