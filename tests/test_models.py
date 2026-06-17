"""Tests for pure value-type helpers in fleet.models."""

from fleet.models import doc_type_for_path


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
