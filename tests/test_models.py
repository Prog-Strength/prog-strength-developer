"""Tests for pure value-type helpers in fleet.models."""

import pytest

from fleet.models import DEFAULT_MODEL, KNOWN_MODELS, doc_type_for_path, validate_model


def test_doc_type_for_sows_path_is_sow():
    assert doc_type_for_path("sows/foo.md") == "sow"


def test_doc_type_for_dx_path_is_dx():
    assert doc_type_for_path("dx/surface.md") == "dx"


def test_doc_type_for_unknown_dir_is_the_leading_segment():
    # New work types get their own top-level dir; until a mapping entry
    # exists, the leading segment is recorded verbatim rather than guessed.
    assert doc_type_for_path("audits/security.md") == "audits"


def test_doc_type_for_pathless_ticket_falls_back_to_the_whole_string():
    assert doc_type_for_path("loose.md") == "loose.md"


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


def test_validate_model_treats_whitespace_only_as_unset():
    assert validate_model("   ") == DEFAULT_MODEL


def test_validate_model_is_case_sensitive():
    # Membership is exact-match by design: the gate is fail-closed, and
    # every KNOWN_MODELS entry is canonically lowercase. Rejecting a
    # miscased ID is correct, not a usability bug.
    with pytest.raises(ValueError):
        validate_model("Claude-Opus-5")


def test_validate_model_rejects_an_unknown_model():
    with pytest.raises(ValueError) as exc:
        validate_model("claude-opus-9")
    # The message must list the valid options — it is what the operator
    # sees in the failed workflow run.
    assert "claude-opus-9" in str(exc.value)
    assert "claude-fable-5" in str(exc.value)
