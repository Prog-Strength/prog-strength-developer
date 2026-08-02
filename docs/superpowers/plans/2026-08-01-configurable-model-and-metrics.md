# Configurable Claude Model + Per-Model Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the model the autonomous worker runs configurable (defaulting to `claude-fable-5`), and record the model each run actually used so the dashboard can break SOW/DX runs down by model the way it already does by instance type.

**Architecture:** The dispatch workflow resolves a model (per-run input → `vars.DEVELOPER_CLAUDE_MODEL` → `claude-fable-5` floor), validates it against an allowlist in the `fleet` package, and threads it into both the run-history row and the worker's userdata. The worker passes it to `claude --model` and, after Claude exits, overwrites a state file with the model it actually observed in Claude Code's session JSONL — so a rate-limit fallback is recorded honestly. `fleet.metrics.aggregate` gains a `model` label on the three series that already carry `compute_type`.

**Tech Stack:** Python 3.14 (stdlib + boto3), pytest + moto, GitHub Actions, Terraform, Bash (cloud-init userdata), Grafana JSON dashboards.

**Spec:** [`docs/superpowers/specs/2026-08-01-configurable-model-and-metrics-design.md`](../specs/2026-08-01-configurable-model-and-metrics-design.md)

> **One deliberate refinement over the spec:** the spec named the validation subcommand `check-model`. This plan implements `resolve-model` instead — it both applies the default floor and validates, so `claude-fable-5` lives in exactly one place (Python) rather than being duplicated into the workflow YAML. Task 11 updates the spec to match.

**Run tests with:** `uv run pytest` from the repo root. The full suite is currently 108 tests and passes on `main`.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `fleet/models.py` | Modify | Add `DEFAULT_MODEL`, `KNOWN_MODELS`, `validate_model()`; add `model` field to `RunHistory` |
| `fleet/registry.py` | Modify | Add `model` to the `try_acquire` / `release` abstract signatures + docstrings |
| `fleet/memory.py` | Modify | Thread `model` through the in-memory registry |
| `fleet/dynamo.py` | Modify | Persist `model` on the run-history item; default it on read |
| `fleet/cli.py` | Modify | `resolve-model` subcommand; `--model` on `acquire` and `release` |
| `fleet/metrics.py` | Modify | Add `model` to the aggregation key and the three labeled series |
| `bootstrap/ddb_exporter.py` | Modify | Add `model` to the gauge label sets + help text |
| `bootstrap/userdata.sh.tpl` | Modify | `${claude_model}` token, `--model` flag, model state file, observed-model extraction |
| `terraform/ec2.tf` | Modify | Supply `claude_model = ""` to `templatefile()` |
| `.github/workflows/dispatch-sow.yml` | Modify | `model` input, resolve step, thread into render + acquire + summary |
| `monitoring/grafana/dashboards/developer-platform.json` | Modify | "By model (all-time)" table; update section text |
| `README.md` | Modify | Document model selection |
| `tests/test_models.py` | Modify | `validate_model` tests |
| `tests/test_fleet_registry.py` | Modify | `model` contract tests |
| `tests/test_fleet_dynamo.py` | Modify | `model` persistence + legacy-row default |
| `tests/test_fleet_cli.py` | Modify | `resolve-model` + `--model` threading tests |
| `tests/test_metrics.py` | Modify | `model` label tests; update existing label assertions |
| `tests/test_ddb_exporter.py` | Modify | Update gauge label assertions |

**Note on blast radius:** adding a label to `RUNS_TOTAL`, `COMPUTE_SECONDS_TOTAL`, and `COMPUTE_COST_TOTAL` breaks every existing assertion in `tests/test_metrics.py` and `tests/test_ddb_exporter.py` that names those metrics, because both suites match label dicts exactly. Tasks 6 and 7 update them explicitly — this is expected churn, not a regression.

---

### Task 1: Model allowlist and validation

**Files:**
- Modify: `fleet/models.py` (append after `doc_type_for_path`, currently ends line 86)
- Test: `tests/test_models.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_models.py` currently has a single import line, `from fleet.models import doc_type_for_path`. Replace that line with:

```python
import pytest

from fleet.models import DEFAULT_MODEL, KNOWN_MODELS, doc_type_for_path, validate_model
```

Then append the new tests to the end of the file:

```python
def test_default_model_is_in_the_allowlist():
    assert DEFAULT_MODEL in KNOWN_MODELS


def test_validate_model_accepts_every_known_model():
    for model in KNOWN_MODELS:
        assert validate_model(model) == model


def test_validate_model_falls_back_to_the_default_when_blank():
    # The dispatch workflow passes "" when neither the per-run input nor
    # the repo variable is set; that must mean "use the default", not "fail".
    assert validate_model("") == DEFAULT_MODEL
    assert validate_model(None) == DEFAULT_MODEL


def test_validate_model_strips_surrounding_whitespace():
    assert validate_model("  claude-opus-5  ") == "claude-opus-5"


def test_validate_model_rejects_an_unknown_model():
    with pytest.raises(ValueError) as exc:
        validate_model("claude-opus-9")
    # The message must list the valid options — it is what the operator
    # sees in the failed workflow run.
    assert "claude-opus-9" in str(exc.value)
    assert "claude-fable-5" in str(exc.value)
```

Note `tests/test_models.py` currently has no imports beyond `from fleet.models import doc_type_for_path`. Put the `import pytest` line at the top of the file with the other imports rather than mid-file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL with `ImportError: cannot import name 'DEFAULT_MODEL' from 'fleet.models'`

- [ ] **Step 3: Implement**

Append to `fleet/models.py`, after `doc_type_for_path` and before the `RunHistory` dataclass:

```python
#: The model the worker runs when nothing else is configured. Chosen as
#: the platform's floor because the workload is long-horizon agentic work.
DEFAULT_MODEL = "claude-fable-5"

#: Models this platform is willing to dispatch. The gate is deliberately
#: fail-closed: a typo'd model would otherwise boot a t3.xlarge that dies
#: ~4 minutes in when `claude` rejects the flag, burning a dispatch cycle
#: and churning the SOW lock. Adding a new model is a one-line PR here.
#:
#: This asserts "a real model ID we are willing to run" — NOT "this
#: subscription serves it". The worker authenticates with Claude Code
#: OAuth credentials, so availability is subscription-gated; smoke-test a
#: model on one dispatch before making it the repo default.
KNOWN_MODELS = frozenset(
    {
        "claude-fable-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-sonnet-5",
    }
)


def validate_model(model: str | None) -> str:
    """Resolve and validate a dispatch's model.

    Blank or None means "unset" — the dispatch workflow passes an empty
    string when neither the per-run input nor the repo variable is set —
    and resolves to :data:`DEFAULT_MODEL`. Anything outside
    :data:`KNOWN_MODELS` raises :class:`ValueError`.
    """
    resolved = (model or "").strip() or DEFAULT_MODEL
    if resolved not in KNOWN_MODELS:
        options = ", ".join(sorted(KNOWN_MODELS))
        raise ValueError(f"unknown model {resolved!r}; expected one of: {options}")
    return resolved
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_models.py -v`
Expected: PASS (all tests, including the pre-existing `doc_type_for_path` ones)

- [ ] **Step 5: Commit**

```bash
git add fleet/models.py tests/test_models.py
git commit -m "feat(fleet): add model allowlist and validation"
```

---

### Task 2: `fleet resolve-model` subcommand

**Files:**
- Modify: `fleet/cli.py` (parser at lines 35-68, dispatch at lines 71-158)
- Test: `tests/test_fleet_cli.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fleet_cli.py`:

```python
def test_resolve_model_prints_the_resolved_model(capsys):
    reg = FakeRunRegistry()
    code = run(["resolve-model", "--model", "claude-opus-5"], reg)
    assert code == OK
    assert capsys.readouterr().out.strip() == "claude-opus-5"


def test_resolve_model_falls_back_to_the_default_when_blank(capsys):
    reg = FakeRunRegistry()
    code = run(["resolve-model", "--model", ""], reg)
    assert code == OK
    assert capsys.readouterr().out.strip() == "claude-fable-5"


def test_resolve_model_rejects_an_unknown_model(capsys):
    reg = FakeRunRegistry()
    code = run(["resolve-model", "--model", "claude-opus-9"], reg)
    assert code == ERROR
    # Error goes to stderr so stdout stays clean for shell capture.
    captured = capsys.readouterr()
    assert captured.out.strip() == ""
    assert "claude-opus-9" in captured.err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fleet_cli.py -k resolve_model -v`
Expected: FAIL — argparse exits with `SystemExit: 2` and "invalid choice: 'resolve-model'"

- [ ] **Step 3: Implement**

In `fleet/cli.py`, add `validate_model` to the models import (line 27):

```python
from fleet.models import RunStatus, validate_model
```

In `_build_parser()`, add after the `lst` subparser block (currently lines 65-66) and before `return p`:

```python
    rm = sub.add_parser(
        "resolve-model",
        help="resolve + validate a model, printing it for the dispatch workflow to capture",
    )
    rm.add_argument(
        "--model",
        default="",
        help="candidate model; blank resolves to the default",
    )
```

In `run()`, add this branch before the final `return EXIT_ERROR` (line 158):

```python
    if args.command == "resolve-model":
        # stdout carries ONLY the resolved model so the workflow can capture
        # it with $(...); the failure message goes to stderr.
        try:
            print(validate_model(args.model))
        except ValueError as exc:
            print(f"resolve-model failed: {exc}", file=sys.stderr)
            return EXIT_ERROR
        return EXIT_OK
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fleet_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add fleet/cli.py tests/test_fleet_cli.py
git commit -m "feat(fleet): add resolve-model CLI subcommand"
```

---

### Task 3: `RunHistory.model` and the registry contract

**Files:**
- Modify: `fleet/models.py` (`RunHistory` dataclass, currently lines 89-113)
- Modify: `fleet/registry.py` (`try_acquire` lines 25-45, `release` lines 63-81)
- Modify: `fleet/memory.py` (`try_acquire` lines 29-64, `release` lines 89-119)
- Test: `tests/test_fleet_registry.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fleet_registry.py`:

```python
def test_acquire_records_the_dispatched_model_on_history():
    reg = FakeRunRegistry()
    reg.try_acquire(
        "sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=1000, model="claude-fable-5"
    )
    # Written at acquire so an in-flight (working) row carries a real
    # model rather than landing in the "unknown" bucket.
    assert reg.list_history("sows/foo.md")[0].model == "claude-fable-5"


def test_acquire_without_a_model_defaults_to_unknown():
    reg = FakeRunRegistry()
    reg.try_acquire("sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=1000)
    assert reg.list_history("sows/foo.md")[0].model == "unknown"


def test_release_overwrites_the_model_with_the_observed_one():
    reg = FakeRunRegistry()
    reg.try_acquire(
        "sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=1000, model="claude-fable-5"
    )
    reg.attach_instance("sows/foo.md", dispatch_id="d1", instance_id="i-1", now=110)
    # The worker observed a different model than was dispatched — e.g. a
    # fallback under rate limits. The row must record what actually ran.
    reg.release(
        "sows/foo.md", instance_id="i-1", outcome="success", now=460, model="claude-opus-5"
    )
    assert reg.list_history("sows/foo.md")[0].model == "claude-opus-5"


def test_release_without_a_model_leaves_the_acquire_value():
    reg = FakeRunRegistry()
    reg.try_acquire(
        "sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=1000, model="claude-fable-5"
    )
    reg.attach_instance("sows/foo.md", dispatch_id="d1", instance_id="i-1", now=110)
    reg.release("sows/foo.md", instance_id="i-1", outcome="success", now=460)
    assert reg.list_history("sows/foo.md")[0].model == "claude-fable-5"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fleet_registry.py -k model -v`
Expected: FAIL with `TypeError: try_acquire() got an unexpected keyword argument 'model'`

- [ ] **Step 3: Implement**

In `fleet/models.py`, add the field to `RunHistory` immediately after `compute_type` (line 107):

```python
    compute_type: str = "ec2"
    #: The model that authored this run. Written at acquire with the
    #: dispatched value and overwritten at release with the model actually
    #: observed in Claude Code's session log. Rows written before model
    #: capture existed read "unknown".
    model: str = "unknown"
```

Also extend the class docstring's second paragraph (currently lines 94-99) so it mentions the model. Replace:

```python
    Created at acquire with ``status=working`` and the dispatch metadata,
    patched with ``instance_id`` at attach, and finalized at release with
    the terminal ``outcome``/``finished_at``/``duration_seconds``/
    ``prs_opened``. A row left at ``working`` with no ``finished_at`` is a
    run that died or was superseded before releasing.
```

with:

```python
    Created at acquire with ``status=working``, the dispatch metadata, and
    the dispatched ``model``; patched with ``instance_id`` at attach; and
    finalized at release with the terminal ``outcome``/``finished_at``/
    ``duration_seconds``/``prs_opened`` and the ``model`` the worker
    actually observed. A row left at ``working`` with no ``finished_at``
    is a run that died or was superseded before releasing.
```

In `fleet/registry.py`, change the `try_acquire` signature (lines 26-35) to add `model`:

```python
    @abstractmethod
    def try_acquire(
        self,
        sow: str,
        dispatch_id: str,
        now: int,
        ttl_seconds: int,
        dispatched_by: str | None = None,
        doc_type: str | None = None,
        compute_type: str = "ec2",
        model: str = "unknown",
    ) -> AcquireResult:
```

and extend its docstring's last paragraph (lines 42-45) to:

```python
        On success, also appends an immutable run-history row (status
        ``working``) carrying ``doc_type`` (derived from the ticket path
        when None), ``compute_type``, and the dispatched ``model`` for the
        durable record.
        """
```

Change the `release` signature (lines 64-72) to add `model`:

```python
    @abstractmethod
    def release(
        self,
        sow: str,
        instance_id: str | None,
        outcome: str,
        now: int,
        force: bool = False,
        prs_opened: int | None = None,
        model: str | None = None,
    ) -> bool:
```

and extend its docstring's last paragraph (lines 78-81) to:

```python
        When the lock is actually released, the matching run-history row
        is finalized with the ``outcome``, ``finished_at``,
        ``duration_seconds``, and ``prs_opened``. When ``model`` is given
        it replaces the value written at acquire — the worker reports the
        model it actually observed, which can differ from the dispatched
        one. A no-op release leaves history untouched, so a superseded
        run's row stays ``working``."""
```

In `fleet/memory.py`, add `model: str = "unknown"` to the `try_acquire` signature (after `compute_type`, line 37) and pass it into the `RunHistory(...)` construction (after `compute_type=compute_type`, line 61):

```python
            compute_type=compute_type,
            model=model,
```

Add `model: str | None = None` to the `release` signature (after `prs_opened`, line 96), and inside the history-finalize block (lines 109-118) replace:

```python
        hist = self._history.get((sow, existing.dispatch_id))
        if hist is not None:
            self._history[(sow, existing.dispatch_id)] = replace(
                hist,
                status=RunStatus.from_outcome(outcome),
                outcome=outcome,
                finished_at=now,
                duration_seconds=now - hist.started_at,
                prs_opened=prs_opened,
                updated_at=now,
            )
```

with:

```python
        hist = self._history.get((sow, existing.dispatch_id))
        if hist is not None:
            # An omitted model leaves the value written at acquire.
            extra = {"model": model} if model is not None else {}
            self._history[(sow, existing.dispatch_id)] = replace(
                hist,
                status=RunStatus.from_outcome(outcome),
                outcome=outcome,
                finished_at=now,
                duration_seconds=now - hist.started_at,
                prs_opened=prs_opened,
                updated_at=now,
                **extra,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fleet_registry.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add fleet/models.py fleet/registry.py fleet/memory.py tests/test_fleet_registry.py
git commit -m "feat(fleet): record the model on run-history rows"
```

---

### Task 4: Persist `model` in DynamoDB

**Files:**
- Modify: `fleet/dynamo.py` (`try_acquire` lines 95-150, `release` lines 194-245, `_history_item` lines 282-297, `_to_history` lines 300-318)
- Test: `tests/test_fleet_dynamo.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fleet_dynamo.py`:

```python
def test_acquire_persists_the_dispatched_model(registry):
    registry.try_acquire(
        "sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=TTL, model="claude-fable-5"
    )
    assert registry.list_history("sows/foo.md")[0].model == "claude-fable-5"


def test_release_persists_the_observed_model(registry):
    registry.try_acquire(
        "sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=TTL, model="claude-fable-5"
    )
    registry.attach_instance("sows/foo.md", dispatch_id="d1", instance_id="i-1", now=110)
    registry.release(
        "sows/foo.md", instance_id="i-1", outcome="success", now=460, model="claude-opus-5"
    )
    assert registry.list_history("sows/foo.md")[0].model == "claude-opus-5"


def test_release_without_a_model_leaves_the_acquire_value(registry):
    registry.try_acquire(
        "sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=TTL, model="claude-fable-5"
    )
    registry.attach_instance("sows/foo.md", dispatch_id="d1", instance_id="i-1", now=110)
    registry.release("sows/foo.md", instance_id="i-1", outcome="success", now=460)
    assert registry.list_history("sows/foo.md")[0].model == "claude-fable-5"


def test_legacy_history_row_without_model_reads_unknown(registry):
    """Rows written before model capture existed must still deserialize."""
    registry._table.put_item(
        Item={
            "sow": "sows/legacy.md",
            "sk": "RUN#00000000000000000100#d0",
            "dispatch_id": "d0",
            "doc_type": "sow",
            "compute_type": "ec2:t3.xlarge",
            "status": "done",
            "started_at": 100,
            "updated_at": 460,
            "outcome": "success",
            "finished_at": 460,
            "duration_seconds": 360,
        }
    )
    row = registry.list_history("sows/legacy.md")[0]
    assert row.model == "unknown"
    assert row.compute_type == "ec2:t3.xlarge"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fleet_dynamo.py -k model -v`
Expected: FAIL with `TypeError: try_acquire() got an unexpected keyword argument 'model'`

- [ ] **Step 3: Implement**

In `fleet/dynamo.py`, add `model: str = "unknown"` to the `try_acquire` signature (after `compute_type`, line 103) and pass it into the `RunHistory(...)` construction (after `compute_type=compute_type`, line 146):

```python
            compute_type=compute_type,
            model=model,
```

Add `model: str | None = None` to the `release` signature (after `prs_opened`, line 201). In the history-finalize `update_item` (lines 229-244), replace:

```python
        self._table.update_item(
            Key={"sow": sow, "sk": _run_sk(started_at, lock["dispatch_id"])},
            UpdateExpression=(
                "SET #status = :status, outcome = :outcome, "
                "finished_at = :now, duration_seconds = :dur, "
                "prs_opened = :prs, updated_at = :now"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": status.value,
                ":outcome": outcome,
                ":now": now,
                ":dur": now - started_at,
                ":prs": prs_opened,
            },
        )
```

with:

```python
        # The observed model, if the worker reported one, replaces the
        # dispatched value written at acquire. Omitted → keep acquire's.
        #
        # Names and values are built together and conditionally: DynamoDB
        # rejects an ExpressionAttributeNames entry the expression never
        # references, so "#model" may only be declared when the SET clause
        # actually uses it. ("model" is not a reserved word today, but
        # aliasing costs nothing and survives that changing.)
        history_update = (
            "SET #status = :status, outcome = :outcome, "
            "finished_at = :now, duration_seconds = :dur, "
            "prs_opened = :prs, updated_at = :now"
        )
        history_names = {"#status": "status"}
        history_values: dict = {
            ":status": status.value,
            ":outcome": outcome,
            ":now": now,
            ":dur": now - started_at,
            ":prs": prs_opened,
        }
        if model is not None:
            history_update += ", #model = :model"
            history_names["#model"] = "model"
            history_values[":model"] = model
        self._table.update_item(
            Key={"sow": sow, "sk": _run_sk(started_at, lock["dispatch_id"])},
            UpdateExpression=history_update,
            ExpressionAttributeNames=history_names,
            ExpressionAttributeValues=history_values,
        )
```

In `_history_item` (lines 282-297), add `model` to the item dict after `compute_type` (line 290):

```python
        "compute_type": history.compute_type,
        "model": history.model,
```

In `_to_history` (lines 300-318), add the defaulted read after `compute_type` (line 311):

```python
        compute_type=item.get("compute_type", "ec2"),
        model=item.get("model", "unknown"),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fleet_dynamo.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add fleet/dynamo.py tests/test_fleet_dynamo.py
git commit -m "feat(fleet): persist the run model in DynamoDB"
```

---

### Task 5: `--model` on the `acquire` and `release` CLI commands

**Files:**
- Modify: `fleet/cli.py` (`acq` parser lines 39-44, `rel` parser lines 56-63, `acquire` branch lines 74-82, `release` branch lines 120-128)
- Test: `tests/test_fleet_cli.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fleet_cli.py`:

```python
def test_acquire_threads_model_into_history():
    reg = FakeRunRegistry()
    run(
        ["acquire", "--sow", "sows/foo.md", "--dispatch-id", "d1",
         "--model", "claude-fable-5"],
        reg,
    )
    assert reg.list_history("sows/foo.md")[0].model == "claude-fable-5"


def test_acquire_without_model_records_unknown():
    reg = FakeRunRegistry()
    run(["acquire", "--sow", "sows/foo.md", "--dispatch-id", "d1"], reg)
    assert reg.list_history("sows/foo.md")[0].model == "unknown"


def test_release_threads_observed_model_into_history():
    reg = FakeRunRegistry()
    run(
        ["acquire", "--sow", "sows/foo.md", "--dispatch-id", "d1",
         "--model", "claude-fable-5"],
        reg,
        now=100,
    )
    run(["attach", "--sow", "sows/foo.md", "--dispatch-id", "d1", "--instance-id", "i-1"], reg)
    code = run(
        ["release", "--sow", "sows/foo.md", "--instance-id", "i-1",
         "--outcome", "success", "--model", "claude-opus-5"],
        reg,
        now=460,
    )
    assert code == OK
    assert reg.list_history("sows/foo.md")[0].model == "claude-opus-5"


def test_release_without_model_keeps_the_acquire_value():
    reg = FakeRunRegistry()
    run(
        ["acquire", "--sow", "sows/foo.md", "--dispatch-id", "d1",
         "--model", "claude-fable-5"],
        reg,
        now=100,
    )
    run(["attach", "--sow", "sows/foo.md", "--dispatch-id", "d1", "--instance-id", "i-1"], reg)
    run(["release", "--sow", "sows/foo.md", "--instance-id", "i-1"], reg, now=460)
    assert reg.list_history("sows/foo.md")[0].model == "claude-fable-5"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fleet_cli.py -k "model" -v`
Expected: FAIL — argparse exits `SystemExit: 2` with "unrecognized arguments: --model"

- [ ] **Step 3: Implement**

In `_build_parser()`, add to the `acq` subparser (after `acq.add_argument("--ttl", ...)`, line 43):

```python
    acq.add_argument(
        "--model",
        default="unknown",
        help="model this dispatch will run; recorded on the run-history row",
    )
```

Add to the `rel` subparser (after the `--prs-opened` argument, line 63):

```python
    rel.add_argument(
        "--model",
        default=None,
        help="model the worker actually observed; replaces the value recorded at acquire",
    )
```

In the `acquire` branch, add `model=args.model` to the `try_acquire` call (after `dispatched_by=args.dispatched_by`, line 81):

```python
            dispatched_by=args.dispatched_by,
            model=args.model,
        )
```

In the `release` branch, add `model=args.model` to the `release` call (after `prs_opened=args.prs_opened`, line 127):

```python
            prs_opened=args.prs_opened,
            model=args.model,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fleet_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add fleet/cli.py tests/test_fleet_cli.py
git commit -m "feat(fleet): thread model through acquire and release"
```

---

### Task 6: `model` label on the aggregate metrics

**Files:**
- Modify: `fleet/metrics.py` (module docstring line 11-12, `aggregate` lines 92-136)
- Test: `tests/test_metrics.py`

This task changes the label set on three series, so every existing assertion naming them needs `model="unknown"` added. Do the helper + existing-assertion updates and the new tests in one step, since the suite cannot be green in between.

- [ ] **Step 1: Update the test helpers and existing assertions, and add new tests**

In `tests/test_metrics.py`, replace the two helpers (lines 22-47) with:

```python
def _terminal(doc_type, outcome, dur, prs, started=100, compute_type="ec2", model="unknown"):
    return RunHistory(
        sow="t",
        dispatch_id="d",
        doc_type=doc_type,
        status=RunStatus.from_outcome(outcome),
        started_at=started,
        updated_at=started + dur,
        compute_type=compute_type,
        model=model,
        outcome=outcome,
        finished_at=started + dur,
        duration_seconds=dur,
        prs_opened=prs,
    )


def _working(doc_type, started=100, compute_type="ec2", model="unknown"):
    return RunHistory(
        sow="t",
        dispatch_id="d",
        doc_type=doc_type,
        status=RunStatus.WORKING,
        started_at=started,
        updated_at=started,
        compute_type=compute_type,
        model=model,
    )
```

Then replace each of these test functions in full. `test_runs_counted_by_doc_type_outcome_and_compute_type` (lines 50-60):

```python
def test_runs_counted_by_doc_type_outcome_and_compute_type():
    rows = [
        _terminal("sow", "success", 10, 1),
        _terminal("sow", "success", 20, 1),
        _terminal("sow", "error", 30, 0),
        _terminal("dx", "success", 40, 1),
    ]
    s = aggregate(rows)
    assert _val(s, metrics.RUNS_TOTAL, doc_type="sow", compute_type="ec2", model="unknown", outcome="success") == 2
    assert _val(s, metrics.RUNS_TOTAL, doc_type="sow", compute_type="ec2", model="unknown", outcome="error") == 1
    assert _val(s, metrics.RUNS_TOTAL, doc_type="dx", compute_type="ec2", model="unknown", outcome="success") == 1
```

`test_runs_split_by_instance_type` (lines 63-70):

```python
def test_runs_split_by_instance_type():
    rows = [
        _terminal("sow", "success", 10, 0, compute_type="ec2:t3.xlarge"),
        _terminal("sow", "success", 10, 0, compute_type="ec2:t3.2xlarge"),
    ]
    s = aggregate(rows)
    assert _val(s, metrics.RUNS_TOTAL, doc_type="sow", compute_type="ec2:t3.xlarge", model="unknown", outcome="success") == 1
    assert _val(s, metrics.RUNS_TOTAL, doc_type="sow", compute_type="ec2:t3.2xlarge", model="unknown", outcome="success") == 1
```

`test_canonical_outcomes_emitted_even_when_zero` (lines 73-76):

```python
def test_canonical_outcomes_emitted_even_when_zero():
    s = aggregate([_terminal("sow", "success", 10, 1)])
    assert _val(s, metrics.RUNS_TOTAL, doc_type="sow", compute_type="ec2", model="unknown", outcome="timeout") == 0
    assert _val(s, metrics.RUNS_TOTAL, doc_type="sow", compute_type="ec2", model="unknown", outcome="working") == 0
```

`test_working_rows_bucketed_and_excluded_from_duration` (lines 79-84):

```python
def test_working_rows_bucketed_and_excluded_from_duration():
    rows = [_working("sow"), _terminal("sow", "success", 50, 2)]
    s = aggregate(rows)
    assert _val(s, metrics.RUNS_TOTAL, doc_type="sow", compute_type="ec2", model="unknown", outcome="working") == 1
    assert _val(s, metrics.COMPUTE_SECONDS_TOTAL, doc_type="sow", compute_type="ec2", model="unknown") == 50
    assert _val(s, metrics.DURATION_AVG, doc_type="sow") == 50
```

`test_compute_seconds_summed_by_doc_type_and_compute_type` (lines 98-104):

```python
def test_compute_seconds_summed_by_doc_type_and_compute_type():
    rows = [
        _terminal("sow", "success", 10, 0, compute_type="ec2:t3.xlarge"),
        _terminal("sow", "error", 25, 0, compute_type="ec2:t3.xlarge"),
    ]
    s = aggregate(rows)
    assert _val(s, metrics.COMPUTE_SECONDS_TOTAL, doc_type="sow", compute_type="ec2:t3.xlarge", model="unknown") == 35
```

`test_cost_is_duration_times_hardcoded_instance_rate` (lines 122-126):

```python
def test_cost_is_duration_times_hardcoded_instance_rate():
    # t3.xlarge us-east-2 on-demand = $0.1664/hr; one full hour = $0.1664.
    rows = [_terminal("sow", "success", 3600, 0, compute_type="ec2:t3.xlarge")]
    s = aggregate(rows)
    assert _val(s, metrics.COMPUTE_COST_TOTAL, doc_type="sow", compute_type="ec2:t3.xlarge", model="unknown") == pytest.approx(0.1664)
```

`test_cost_is_zero_for_unpriced_compute_type` (lines 129-133):

```python
def test_cost_is_zero_for_unpriced_compute_type():
    # A coarse "ec2" (attach never resolved the type) has no rate → no cost.
    rows = [_terminal("sow", "success", 3600, 0, compute_type="ec2")]
    s = aggregate(rows)
    assert _val(s, metrics.COMPUTE_COST_TOTAL, doc_type="sow", compute_type="ec2", model="unknown") == 0
```

`test_cost_excludes_working_rows` (lines 136-139):

```python
def test_cost_excludes_working_rows():
    rows = [_working("sow", compute_type="ec2:t3.xlarge")]
    s = aggregate(rows)
    assert _val(s, metrics.COMPUTE_COST_TOTAL, doc_type="sow", compute_type="ec2:t3.xlarge", model="unknown") == 0
```

Finally, append the new model-specific tests:

```python
def test_runs_split_by_model():
    rows = [
        _terminal("sow", "success", 10, 0, model="claude-fable-5"),
        _terminal("sow", "success", 10, 0, model="claude-fable-5"),
        _terminal("sow", "success", 10, 0, model="claude-opus-5"),
        _terminal("dx", "success", 10, 0, model="claude-opus-5"),
    ]
    s = aggregate(rows)
    assert _val(s, metrics.RUNS_TOTAL, doc_type="sow", compute_type="ec2", model="claude-fable-5", outcome="success") == 2
    assert _val(s, metrics.RUNS_TOTAL, doc_type="sow", compute_type="ec2", model="claude-opus-5", outcome="success") == 1
    assert _val(s, metrics.RUNS_TOTAL, doc_type="dx", compute_type="ec2", model="claude-opus-5", outcome="success") == 1


def test_compute_seconds_and_cost_split_by_model():
    rows = [
        _terminal("sow", "success", 3600, 0, compute_type="ec2:t3.xlarge", model="claude-fable-5"),
        _terminal("sow", "success", 1800, 0, compute_type="ec2:t3.xlarge", model="claude-opus-5"),
    ]
    s = aggregate(rows)
    assert _val(s, metrics.COMPUTE_SECONDS_TOTAL, doc_type="sow", compute_type="ec2:t3.xlarge", model="claude-fable-5") == 3600
    assert _val(s, metrics.COMPUTE_SECONDS_TOTAL, doc_type="sow", compute_type="ec2:t3.xlarge", model="claude-opus-5") == 1800
    assert _val(s, metrics.COMPUTE_COST_TOTAL, doc_type="sow", compute_type="ec2:t3.xlarge", model="claude-fable-5") == pytest.approx(0.1664)
    assert _val(s, metrics.COMPUTE_COST_TOTAL, doc_type="sow", compute_type="ec2:t3.xlarge", model="claude-opus-5") == pytest.approx(0.0832)


def test_only_observed_compute_type_model_pairs_are_emitted():
    # Two rows differing in BOTH dimensions must not produce the 2x2
    # cross product — cardinality follows what was actually observed.
    rows = [
        _terminal("sow", "success", 10, 0, compute_type="ec2:t3.xlarge", model="claude-fable-5"),
        _terminal("sow", "success", 10, 0, compute_type="ec2:t3.2xlarge", model="claude-opus-5"),
    ]
    s = aggregate(rows)
    pairs = {
        (x.labels["compute_type"], x.labels["model"])
        for x in s
        if x.name == metrics.COMPUTE_SECONDS_TOTAL
    }
    assert pairs == {("ec2:t3.xlarge", "claude-fable-5"), ("ec2:t3.2xlarge", "claude-opus-5")}


def test_duration_stats_are_not_split_by_model():
    # Durations stay keyed by doc_type only, so the "all" and per-doc_type
    # panels keep working unchanged.
    rows = [
        _terminal("sow", "success", 10, 0, model="claude-fable-5"),
        _terminal("sow", "success", 30, 0, model="claude-opus-5"),
    ]
    s = aggregate(rows)
    assert _val(s, metrics.DURATION_AVG, doc_type="sow") == 20
    assert _val(s, metrics.DURATION_MAX, doc_type="all") == 30
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: FAIL — many assertions return `None` (no sample matches the new label dict), e.g. `assert None == 2`

- [ ] **Step 3: Implement**

In `fleet/metrics.py`, update the module docstring's last sentence (lines 11-12). Replace:

```python
Labels are deliberately low-cardinality: ``doc_type`` (sow/dx/``all``) and
``outcome`` (success/error/timeout/working).
```

with:

```python
Labels are deliberately low-cardinality: ``doc_type`` (sow/dx/``all``),
``outcome`` (success/error/timeout/working), ``compute_type`` (the EC2
instance type), and ``model`` (the Claude model that authored the run).
Only observed (compute_type, model) pairs are emitted, so the label space
follows reality rather than the cross product.
```

In `aggregate()`, replace the compute-type loop (lines 116-128):

```python
        # Runs / compute-time / cost are additionally broken down by the
        # resolved instance type (compute_type).
        for compute_type in sorted({r.compute_type for r in dt_rows}):
            ct_rows = [r for r in dt_rows if r.compute_type == compute_type]
            base = {"doc_type": doc_type, "compute_type": compute_type}
            for outcome in OUTCOMES:
                n = sum(1 for r in ct_rows if _bucket(r) == outcome)
                samples.append(MetricSample(RUNS_TOTAL, {**base, "outcome": outcome}, float(n)))

            terminal = [r.duration_seconds for r in ct_rows if r.duration_seconds is not None]
            seconds = float(sum(terminal))
            samples.append(MetricSample(COMPUTE_SECONDS_TOTAL, base, seconds))
            samples.append(MetricSample(COMPUTE_COST_TOTAL, base, seconds / 3600 * _hourly_rate(compute_type)))
```

with:

```python
        # Runs / compute-time / cost are additionally broken down by the
        # resolved instance type and the model that authored the run.
        # Iterating observed pairs (not the cross product) keeps the series
        # count proportional to what actually ran.
        for compute_type, model in sorted({(r.compute_type, r.model) for r in dt_rows}):
            ct_rows = [
                r for r in dt_rows if r.compute_type == compute_type and r.model == model
            ]
            base = {"doc_type": doc_type, "compute_type": compute_type, "model": model}
            for outcome in OUTCOMES:
                n = sum(1 for r in ct_rows if _bucket(r) == outcome)
                samples.append(MetricSample(RUNS_TOTAL, {**base, "outcome": outcome}, float(n)))

            terminal = [r.duration_seconds for r in ct_rows if r.duration_seconds is not None]
            seconds = float(sum(terminal))
            samples.append(MetricSample(COMPUTE_SECONDS_TOTAL, base, seconds))
            samples.append(MetricSample(COMPUTE_COST_TOTAL, base, seconds / 3600 * _hourly_rate(compute_type)))
```

Also update the `aggregate` docstring's first paragraph (lines 93-101) — replace "Counts and PR sums are emitted for every (observed doc_type × canonical outcome) pair" with:

```python
    """Compute the v1 metric samples from every run-history row.

    PR sums are emitted for every (observed doc_type × canonical outcome)
    pair, and run counts for every (observed doc_type × compute_type ×
    model × canonical outcome) — including zeros — so a bucket that empties
    (a ``working`` row finalizing) resets its gauge instead of leaving a
    stale series. Duration/compute stats consider terminal rows only; the
    ``doc_type="all"`` duration series is always emitted (0 when empty) so
    its panel never reads "No data".
    """
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add fleet/metrics.py tests/test_metrics.py
git commit -m "feat(fleet): break run metrics down by model"
```

---

### Task 7: Exporter label sets

**Files:**
- Modify: `bootstrap/ddb_exporter.py` (`_LABELED` lines 36-44, `_HELP` lines 46-54)
- Test: `tests/test_ddb_exporter.py`

- [ ] **Step 1: Update the existing assertions and add a model assertion**

In `tests/test_ddb_exporter.py`, replace `_registry_with_runs` (lines 17-25) so the two runs use different models:

```python
def _registry_with_runs():
    reg = FakeRunRegistry()
    reg.try_acquire(
        "sows/a.md", dispatch_id="d1", now=100, ttl_seconds=TTL, model="claude-fable-5"
    )
    reg.attach_instance(
        "sows/a.md", dispatch_id="d1", instance_id="i-1", now=110, compute_type="ec2:t3.xlarge"
    )
    reg.release(
        "sows/a.md", instance_id="i-1", outcome="success", now=460, prs_opened=2,
        model="claude-fable-5",
    )
    # In flight: coarse "ec2", and a different model.
    reg.try_acquire(
        "dx/b.md", dispatch_id="d2", now=500, ttl_seconds=TTL, model="claude-opus-5"
    )
    return reg
```

Replace `test_refresh_publishes_aggregated_gauges` (lines 28-39) in full:

```python
def test_refresh_publishes_aggregated_gauges():
    prom, gauges = ddb_exporter.build_metrics()
    ddb_exporter.refresh(gauges, _registry_with_runs(), now=999)

    sv = prom.get_sample_value
    assert sv("developer_history_runs_total", {"doc_type": "sow", "compute_type": "ec2:t3.xlarge", "model": "claude-fable-5", "outcome": "success"}) == 1
    assert sv("developer_history_runs_total", {"doc_type": "dx", "compute_type": "ec2", "model": "claude-opus-5", "outcome": "working"}) == 1
    assert sv("developer_history_prs_opened_total", {"doc_type": "sow", "outcome": "success"}) == 2
    assert sv("developer_history_compute_seconds_total", {"doc_type": "sow", "compute_type": "ec2:t3.xlarge", "model": "claude-fable-5"}) == 360
    assert sv("developer_history_run_duration_seconds_max", {"doc_type": "all"}) == 360
    # cost = 360s / 3600 * $0.1664/hr
    assert sv("developer_history_compute_cost_dollars_total", {"doc_type": "sow", "compute_type": "ec2:t3.xlarge", "model": "claude-fable-5"}) == pytest.approx(0.01664)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ddb_exporter.py -v`
Expected: FAIL with `ValueError: Incorrect label names` — the gauge is declared with three labels but `refresh` now passes four.

- [ ] **Step 3: Implement**

In `bootstrap/ddb_exporter.py`, replace the `_LABELED` dict (lines 36-44):

```python
_LABELED = {
    metrics.RUNS_TOTAL: ["doc_type", "compute_type", "model", "outcome"],
    metrics.PRS_OPENED_TOTAL: ["doc_type", "outcome"],
    metrics.COMPUTE_SECONDS_TOTAL: ["doc_type", "compute_type", "model"],
    metrics.COMPUTE_COST_TOTAL: ["doc_type", "compute_type", "model"],
    metrics.DURATION_AVG: ["doc_type"],
    metrics.DURATION_P90: ["doc_type"],
    metrics.DURATION_MAX: ["doc_type"],
}
```

and the three changed entries in `_HELP` (lines 47, 49, 50):

```python
    metrics.RUNS_TOTAL: "Run-history rows by doc_type, instance type, model, and outcome (outcome=working is in-flight).",
    metrics.PRS_OPENED_TOTAL: "PRs opened, summed by doc_type and outcome.",
    metrics.COMPUTE_SECONDS_TOTAL: "Cumulative worker compute-time (sum of run durations) by doc_type, instance type, and model.",
    metrics.COMPUTE_COST_TOTAL: "Estimated on-demand cost (USD): run duration x hardcoded us-east-2 hourly rate, by doc_type, instance type, and model. Worker cost only — excludes token spend.",
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS — the entire suite, not just this file. This is the first point where every Python change is in place.

- [ ] **Step 5: Commit**

```bash
git add bootstrap/ddb_exporter.py tests/test_ddb_exporter.py
git commit -m "feat(monitoring): expose the model label on history gauges"
```

---

### Task 8: Worker userdata — pass and observe the model

**Files:**
- Modify: `bootstrap/userdata.sh.tpl` (`release_sow_lock` lines 96-113, state-file init line 158, claude invocation lines 695-700)
- Modify: `terraform/ec2.tf` (`templatefile` call lines 17-26)

No unit tests — this is cloud-init Bash. Step 4 renders the template and greps it, which is the verification available without a dispatch.

- [ ] **Step 1: Add the model state file and thread it into the lock release**

In `bootstrap/userdata.sh.tpl`, after line 158 (`echo 0 > /var/run/developer-worker/prs_opened`), add:

```bash
# Seed the model state file with the dispatched model BEFORE the ERR trap
# has any chance to fire, so every release path — including a boot failure
# long before Claude starts — reports something meaningful. Overwritten
# after Claude exits with the model actually observed in its session log.
echo "${claude_model}" > /var/run/developer-worker/model
```

In `release_sow_lock()`, after the `prs_count` read (lines 105-106), add:

```bash
  # The model the run actually used — seeded with the dispatched value at
  # boot, overwritten with the observed one after Claude exits.
  local model
  model=$(cat /var/run/developer-worker/model 2>/dev/null || echo unknown)
  [ -n "$model" ] || model=unknown
```

and add the flag to the `fleet release` invocation (lines 108-112), which becomes:

```bash
  AWS_REGION="${aws_region}" PYTHONPATH="$repo" python3 -m fleet release \
    --sow "${sow_path}" \
    --instance-id "$${INSTANCE_ID:-none}" \
    --outcome "$outcome" \
    --prs-opened "$prs_count" \
    --model "$model" || log "fleet release errored (TTL will reclaim the lock)"
```

Also update the `log` line just above it (line 107) to include the model:

```bash
  log "Releasing SOW lock for ${sow_path} (outcome=$outcome, prs=$prs_count, model=$model)"
```

- [ ] **Step 2: Pass `--model` to Claude and capture what actually ran**

Replace the Claude invocation and exit capture (lines 695-700):

```bash
sudo -i -u developer -- \
  bash -c "cd $WORKDIR && ANTHROPIC_LOG=debug claude --print --dangerously-skip-permissions < $WORKDIR/prompt.md" \
  > "$LOG_DIR/claude.log" 2>&1

CLAUDE_EXIT=$?
log "Claude exited with status $CLAUDE_EXIT"
```

with:

```bash
sudo -i -u developer -- \
  bash -c "cd $WORKDIR && ANTHROPIC_LOG=debug claude --model '${claude_model}' --print --dangerously-skip-permissions < $WORKDIR/prompt.md" \
  > "$LOG_DIR/claude.log" 2>&1

CLAUDE_EXIT=$?
log "Claude exited with status $CLAUDE_EXIT"

# Record the model Claude ACTUALLY ran, which can differ from the one we
# asked for (e.g. a fallback under rate limits) — and that divergence is
# precisely what the per-model dashboard exists to surface. Assistant
# events in the session JSONL carry message.model; take the last one.
# Best-effort: on any failure the seeded dispatch value stays in place.
OBSERVED_MODEL=$(jq -r 'select(.type == "assistant") | .message.model // empty' \
  /home/developer/.claude/projects/*/*.jsonl 2>/dev/null | tail -1 || true)
if [ -n "$OBSERVED_MODEL" ]; then
  log "Observed model: $OBSERVED_MODEL"
  echo "$OBSERVED_MODEL" > /var/run/developer-worker/model
else
  log "Could not observe a model in the session log; keeping dispatched value"
fi
```

> **Template-escaping note:** this file carries two placeholder kinds. `${name}` is substituted at render time (Terraform's `templatefile()` and the workflow's Python render both do this). `$${...}` is the Terraform escape for a literal `${...}` that Bash must see at runtime. Bare `$VAR` needs no escaping. The snippets above follow that: `${claude_model}` is a render-time substitution, `$${INSTANCE_ID:-none}` is a runtime Bash expansion, and `$OBSERVED_MODEL` / `$model` are plain Bash variables.

- [ ] **Step 3: Supply the new key to the Terraform render**

In `terraform/ec2.tf`, add to the `templatefile()` map (after `sow_path = ""`, line 19):

```hcl
    sow_path               = ""
    claude_model           = "" # overridden by the workflow render
```

`templatefile()` errors on any `${...}` variable the map does not supply, so this is required even though the launch template's baked userdata never runs in production.

- [ ] **Step 4: Verify the template renders with the new token**

Run:

This mirrors the dispatch workflow's own render (substitute `${name}`, then unescape `$${` → `${`). Use a quoted heredoc so the shell does no expansion of its own:

```bash
uv run python3 - <<'PY'
import re

src = open("bootstrap/userdata.sh.tpl").read()
subs = {
    "aws_region": "us-east-2",
    "sow_path": "sows/demo.md",
    "github_org": "Prog-Strength",
    "log_group_name": "/aws/ec2/prog-strength-developer",
    "max_runtime_hours": "6",
    "claude_secret_name": "x",
    "github_app_secret_name": "y",
    "manager_private_ip": "10.0.0.1",
    "claude_model": "claude-fable-5",
}
for k, v in subs.items():
    src = src.replace("${" + k + "}", v)
src = src.replace("$${", "${")

leftover = set(re.findall(r"(?<!\$)\$\{[a-z_]+\}", src)) - {"${INSTANCE_ID:-none}"}
assert not any(t.strip("${}") in subs for t in leftover), f"unsubstituted: {leftover}"
assert "claude --model 'claude-fable-5' --print" in src
assert 'echo "claude-fable-5" > /var/run/developer-worker/model' in src
assert '--model "$model"' in src
assert "OBSERVED_MODEL=$(jq -r" in src
print("render OK")
PY
```

Expected: `render OK`

The `leftover` check is deliberately narrow: it only asserts that no *render-time* token (a key in `subs`) survived. Runtime Bash expansions like `${INSTANCE_ID:-none}` are supposed to be present after the `$${` unescape — flagging those would be a false positive.

Then verify the Terraform side parses:

Run: `terraform -chdir=terraform fmt -check && terraform -chdir=terraform validate`
Expected: exit 0. If `terraform validate` needs `terraform init` and the backend is unreachable locally, `terraform -chdir=terraform fmt -check` plus the plan workflow on the PR is sufficient — note that in the PR description rather than blocking.

- [ ] **Step 5: Commit**

```bash
git add bootstrap/userdata.sh.tpl terraform/ec2.tf
git commit -m "feat(worker): run a configurable model and record the observed one"
```

---

### Task 9: Dispatch workflow — resolve, validate, and thread the model

**Files:**
- Modify: `.github/workflows/dispatch-sow.yml` (inputs lines 5-13, steps from line 36)

- [ ] **Step 1: Add the `model` workflow input**

After the `sow_path` input block (ends line 13), add:

```yaml
      # Optional per-run override. Blank means "use the repo default"
      # (vars.DEVELOPER_CLAUDE_MODEL, else the floor in fleet/models.py).
      # Change the repo variable in Settings → Variables to shift every
      # future dispatch — no PR, no deploy.
      model:
        description: "Claude model override (blank = repo default)"
        required: false
        default: ""
        type: string
```

- [ ] **Step 2: Add the resolve step**

Insert a new step immediately after "Sync fleet dependencies" (ends line 46) and before "Configure AWS credentials". It needs no AWS credentials, so placing it here fails a bad model in seconds — before the fleet-cap check, before the lock, before any instance:

```yaml
      # Resolve the model once: per-run input, else the repo variable, else
      # the floor in fleet/models.py. Validation is fail-closed against the
      # KNOWN_MODELS allowlist — a typo here would otherwise boot a
      # t3.xlarge that dies ~4 minutes in when `claude` rejects the flag.
      - name: Resolve model
        id: model
        env:
          CANDIDATE: ${{ inputs.model || vars.DEVELOPER_CLAUDE_MODEL }}
        run: |
          set +e
          RESOLVED=$(uv run python -m fleet resolve-model --model "$CANDIDATE")
          code=$?
          set -e
          if [ "$code" -ne 0 ]; then
            echo "::error::Unrecognized model '$CANDIDATE'. Set inputs.model or vars.DEVELOPER_CLAUDE_MODEL to a model in fleet/models.py::KNOWN_MODELS, or add it there in a one-line PR."
            exit 1
          fi
          echo "Resolved model: $RESOLVED"
          echo "model=$RESOLVED" >> "$GITHUB_OUTPUT"
```

`${{ inputs.model || vars.DEVELOPER_CLAUDE_MODEL }}` yields `''` when both are unset, and `resolve-model` turns `''` into the default — so no `claude-fable-5` literal appears in this YAML.

- [ ] **Step 3: Thread it into acquire, the userdata render, and the summary**

In "Acquire SOW lock" (lines 71-87), add to the step's `env` block:

```yaml
        env:
          SOW_PATH: ${{ inputs.sow_path }}
          MODEL: ${{ steps.model.outputs.model }}
```

and add the flag to the `fleet acquire` invocation:

```bash
          uv run python -m fleet acquire \
            --sow "$SOW_PATH" \
            --dispatch-id "$GITHUB_RUN_ID" \
            --dispatched-by "$GITHUB_ACTOR" \
            --model "$MODEL"
```

In "Render userdata" (lines 110-145), add to the step's `env` block:

```yaml
        env:
          SOW_PATH: ${{ inputs.sow_path }}
          MGR_PRIVATE_IP: ${{ steps.infra.outputs.manager_private_ip }}
          CLAUDE_MODEL: ${{ steps.model.outputs.model }}
```

and add to the `subs` dict inside the heredoc'd Python (after the `manager_private_ip` entry, line 136):

```python
              "manager_private_ip": os.environ["MGR_PRIVATE_IP"],
              "claude_model": os.environ["CLAUDE_MODEL"],
```

In "Summary" (lines 188-232), add to the step's `env` block:

```yaml
        env:
          SOW_PATH: ${{ inputs.sow_path }}
          IID: ${{ steps.run.outputs.instance_id }}
          MODEL: ${{ steps.model.outputs.model }}
```

and add a line to the summary body, after the Instance ID line (line 201):

```bash
            echo "- **Instance ID:** \`$IID\`"
            echo "- **Model:** \`$MODEL\` (the run records the model actually observed, which can differ under rate limits)"
```

- [ ] **Step 4: Verify the workflow parses**

Run:

```bash
uv run python -c "
import sys
try:
    import yaml
except ImportError:
    sys.exit('SKIP: pyyaml not installed; rely on GitHub Actions to parse')
d = yaml.safe_load(open('.github/workflows/dispatch-sow.yml'))
on = d.get('on') or d.get(True)
assert 'model' in on['workflow_dispatch']['inputs'], 'model input missing'
steps = d['jobs']['dispatch']['steps']
names = [s.get('name') for s in steps]
assert 'Resolve model' in names, names
# Must run before the lock is taken and before any instance launches.
assert names.index('Resolve model') < names.index('Acquire SOW lock')
assert names.index('Resolve model') < names.index('Fleet cap check')
print('workflow OK')
"
```

Expected: `workflow OK` (or the SKIP message, in which case rely on the PR's Actions run to parse it).

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/dispatch-sow.yml
git commit -m "feat(dispatch): resolve and validate the model before launching a worker"
```

---

### Task 10: "By model" dashboard table

**Files:**
- Modify: `monitoring/grafana/dashboards/developer-platform.json` (text panel id 141, new table panel)

- [ ] **Step 1: Add the panel**

Add a new panel object to the `panels` array, directly after the panel with `"id": 59` ("By instance type (all-time)"). It mirrors that panel's structure, swapping `compute_type` for `model` and sitting at `y: 102` (id 59 occupies y 94-101):

```json
    {
      "id": 60,
      "type": "table",
      "title": "By model (all-time)",
      "gridPos": { "h": 8, "w": 24, "x": 0, "y": 102 },
      "fieldConfig": {
        "defaults": {},
        "overrides": [
          {
            "matcher": { "id": "byName", "options": "Compute-time" },
            "properties": [{ "id": "unit", "value": "s" }]
          },
          {
            "matcher": { "id": "byName", "options": "Est. cost" },
            "properties": [
              { "id": "unit", "value": "currencyUSD" },
              { "id": "decimals", "value": 2 }
            ]
          }
        ]
      },
      "transformations": [
        {
          "id": "joinByField",
          "options": { "byField": "model", "mode": "outer" }
        },
        {
          "id": "organize",
          "options": {
            "excludeByName": {
              "Time": true,
              "Time 1": true,
              "Time 2": true,
              "Time 3": true
            },
            "indexByName": {
              "model": 0,
              "Value #A": 1,
              "Value #B": 2,
              "Value #C": 3
            },
            "renameByName": {
              "model": "Model",
              "Value #A": "Runs",
              "Value #B": "Compute-time",
              "Value #C": "Est. cost"
            }
          }
        }
      ],
      "targets": [
        {
          "expr": "sum by (model) (developer_history_runs_total{outcome=~\"success|error|timeout\"})",
          "format": "table",
          "instant": true,
          "refId": "A"
        },
        {
          "expr": "sum by (model) (developer_history_compute_seconds_total)",
          "format": "table",
          "instant": true,
          "refId": "B"
        },
        {
          "expr": "sum by (model) (developer_history_compute_cost_dollars_total)",
          "format": "table",
          "instant": true,
          "refId": "C"
        }
      ]
    }
```

- [ ] **Step 2: Update the section's text panel**

In the panel with `"id": 141`, the `options.content` markdown string ends with a bullet beginning `- **By document type / instance type**`. Replace that final bullet with:

```
- **By document type / instance type / model** — the same metrics split by `sow` vs `dx`, by EC2 instance type, and by the Claude model that authored the run (`dx` runs open draft, non-merge PRs, so PRs/run reads differently there). The model recorded is the one the worker *observed* Claude using, so a fallback under rate limits shows up honestly; runs predating model capture read `unknown`. Note **Est. cost** here is worker cost (EC2 wall-clock), not token spend.
```

Remember this is a JSON string value: the newlines in `content` are `\n` escapes and the backticks are literal. Edit the string in place rather than reformatting the JSON.

- [ ] **Step 3: Verify the dashboard is valid JSON with no duplicate panel ids or overlapping layout**

Run:

```bash
uv run python -c "
import json
d = json.load(open('monitoring/grafana/dashboards/developer-platform.json'))
ids = [p['id'] for p in d['panels']]
assert len(ids) == len(set(ids)), f'duplicate panel ids: {[i for i in ids if ids.count(i) > 1]}'
by_model = [p for p in d['panels'] if p.get('title') == 'By model (all-time)']
assert len(by_model) == 1, 'By model panel missing'
p = by_model[0]
assert p['gridPos'] == {'h': 8, 'w': 24, 'x': 0, 'y': 102}, p['gridPos']
assert {t['refId'] for t in p['targets']} == {'A', 'B', 'C'}
assert all('by (model)' in t['expr'] for t in p['targets'])
# The instance-type table it sits below must not overlap it.
inst = [x for x in d['panels'] if x.get('title') == 'By instance type (all-time)'][0]
assert inst['gridPos']['y'] + inst['gridPos']['h'] <= p['gridPos']['y']
print('dashboard OK')
"
```

Expected: `dashboard OK`

- [ ] **Step 4: Commit**

```bash
git add monitoring/grafana/dashboards/developer-platform.json
git commit -m "feat(monitoring): add a by-model breakdown to the dashboard"
```

---

### Task 11: Documentation

**Files:**
- Modify: `README.md` (add a subsection after "Fleet control")
- Modify: `docs/superpowers/specs/2026-08-01-configurable-model-and-metrics-design.md` (status + subcommand name)

- [ ] **Step 1: Document model selection in the README**

Add this section to `README.md`. Place it after the "Fleet control" section, since it is operator-facing configuration in the same register:

```markdown
## Model selection

Each worker runs Claude Code against one model, resolved at dispatch time —
most specific wins:

1. the **`model` input** on the "Dispatch ticket" workflow (blank = skip);
2. the **`DEVELOPER_CLAUDE_MODEL` repository variable** (Settings → Secrets and
   variables → Actions → Variables);
3. the floor in `fleet/models.py` — currently `claude-fable-5`.

The repository variable is the knob to turn when rate limits bite: edit it in
the GitHub UI and every subsequent dispatch picks it up, with no PR and no
deploy. Use the per-run input to try a model on one ticket without changing the
default.

The resolved value is validated against `KNOWN_MODELS` in `fleet/models.py`
**before** the fleet-cap check, the SOW lock, or any instance launch, so a typo
fails the workflow in seconds instead of booting a worker that dies four minutes
later when `claude` rejects the flag. Adopting a new model means adding it to
that set — a one-line PR.

`KNOWN_MODELS` asserts "a real model ID we are willing to run", not "this
subscription serves it": the worker authenticates with the Claude Code OAuth
credentials in Secrets Manager, so availability is subscription-gated.
**Smoke-test a model on one dispatch before making it the repo default.**

**What gets recorded is what actually ran.** The worker seeds a state file with
the dispatched model at boot, then overwrites it after Claude exits with the
model observed in Claude Code's session log. If Claude Code falls back to a
different model under rate limits, the run-history row and the dashboard show
the fallback — not the request. Runs dispatched before this existed read
`unknown`.

The **By model (all-time)** dashboard table breaks runs, compute-time, and
estimated cost down by model. That cost column is *worker* cost (EC2 wall-clock
× hourly rate), not token spend, which this platform cannot see.
```

- [ ] **Step 2: Reconcile the spec with what was built**

In `docs/superpowers/specs/2026-08-01-configurable-model-and-metrics-design.md`:

Change the status line from `**Status:** designed` to `**Status:** implemented`.

In the "Validation gate" section, replace the first paragraph:

```
`fleet/models.py` gains a `KNOWN_MODELS` frozenset and a `validate_model()`
function; `fleet/cli.py` gains a `check-model` subcommand that exits non-zero on
an unknown value. The dispatch workflow runs it immediately after `uv sync` —
before the fleet-cap check, before the lock, before any instance.
```

with:

```
`fleet/models.py` gains `DEFAULT_MODEL`, a `KNOWN_MODELS` frozenset, and a
`validate_model()` function; `fleet/cli.py` gains a `resolve-model` subcommand
that applies the default floor, validates, and prints the resolved ID (exiting
non-zero on an unknown value). The dispatch workflow captures its stdout
immediately after `uv sync` — before the fleet-cap check, before the lock,
before any instance. Folding the floor into the same command keeps
`claude-fable-5` in exactly one place rather than duplicating it into the
workflow YAML.
```

- [ ] **Step 3: Run the full suite one more time**

Run: `uv run pytest`
Expected: PASS. This plan adds 24 tests (5 + 3 + 4 + 4 + 4 + 4) to the 108 on `main`, so expect 132 passing and 0 failures. A lower count means a task's tests were skipped or dropped during a rewrite — go back and find which.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/superpowers/specs/2026-08-01-configurable-model-and-metrics-design.md
git commit -m "docs: document model selection and per-model metrics"
```

---

## Manual verification (after merge)

Automated tests cover the Python. These two dispatches cover the seams they cannot reach — the userdata render, the `claude --model` flag, the JSONL extraction, and the dashboard query.

- [ ] Dispatch a small ticket with the `model` input **blank**. Confirm the workflow's "Resolve model" step logs `claude-fable-5` (or your repo variable), the run completes, and `uv run python -m fleet list` plus the run-history row show the expected model.
- [ ] Dispatch the same ticket with the `model` input set to `claude-opus-5`. Confirm the worker's `userdata` CloudWatch stream logs `Observed model: claude-opus-5` and the **By model (all-time)** dashboard table gains a second row.
- [ ] Dispatch with a deliberately bogus model (e.g. `claude-opus-9`) and confirm the workflow fails at "Resolve model" **without** launching an instance or taking the SOW lock — check that no `prog-strength-developer-worker` instance was created and `uv run python -m fleet list` is unchanged.
