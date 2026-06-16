"""Tests for the ticket parser/router — the seam that turns a ticket's
frontmatter into a work type, a prompt template, and a substitution map.

This is the only divergence between the SOW and DX work types that is
worth unit-testing: routing and fail-fast validation. The userdata shell
calls this module (``python3 -m bootstrap.ticket``); keeping the logic
here is what makes a malformed DX fail at boot in milliseconds instead of
after a six-hour run that produces five identical cards.
"""

import textwrap

import pytest

from bootstrap import ticket


def _write(tmp_path, body):
    p = tmp_path / "ticket.md"
    p.write_text(body)
    return str(p)


SOW_FIXTURE = textwrap.dedent("""\
    ---
    status: ready_for_implementation
    repos:
      - prog-strength-api
      - prog-strength-docs
    ---

    # Some Feature

    Body text.
    """)

DX_FIXTURE = textwrap.dedent("""\
    ---
    type: dx
    status: draft
    surface: runs-list
    idioms:
      - editorial-data-journalism
      - brutalist-monospace
      - warm-organic
      - terminal-dense
      - linear-minimal
    references:
      - Strava
      - Whoop
      - Linear
    scope: in-system
    variant_count: 5
    repos:
      - prog-strength-web
      - prog-strength-docs
    ---

    # DX: runs-list

    Body text.
    """)


# --- parse: type defaulting ------------------------------------------------


def test_parse_sow_defaults_type_to_sow(tmp_path):
    t = ticket.parse(_write(tmp_path, SOW_FIXTURE))
    assert t.type == "sow"
    assert t.repos == ["prog-strength-api", "prog-strength-docs"]


def test_parse_dx_exposes_all_fields(tmp_path):
    t = ticket.parse(_write(tmp_path, DX_FIXTURE))
    assert t.type == "dx"
    assert t.surface == "runs-list"
    assert t.idioms == [
        "editorial-data-journalism",
        "brutalist-monospace",
        "warm-organic",
        "terminal-dense",
        "linear-minimal",
    ]
    assert t.references == ["Strava", "Whoop", "Linear"]
    assert t.scope == "in-system"
    assert t.variant_count == 5
    assert t.repos == ["prog-strength-web", "prog-strength-docs"]


# --- parse: shared validation ----------------------------------------------


def test_parse_rejects_missing_frontmatter(tmp_path):
    p = tmp_path / "ticket.md"
    p.write_text("# No frontmatter here\n")
    with pytest.raises(ticket.TicketError, match="frontmatter"):
        ticket.parse(str(p))


def test_parse_rejects_unknown_type(tmp_path):
    with pytest.raises(ticket.TicketError, match="type"):
        ticket.parse(_write(tmp_path, SOW_FIXTURE.replace("status: ready_for_implementation", "type: ds")))


def test_parse_rejects_non_mapping_frontmatter(tmp_path):
    # A bare scalar between the --- fences must surface a single-line
    # TicketError, not a raw AttributeError from meta.get().
    p = tmp_path / "ticket.md"
    p.write_text("---\njust a string\n---\n# t\n")
    with pytest.raises(ticket.TicketError, match="mapping"):
        ticket.parse(str(p))


def test_parse_dx_rejects_non_integer_variant_count(tmp_path):
    # YAML can type variant_count as a string; that must be a clean
    # TicketError, not a TypeError from the len() comparison.
    with pytest.raises(ticket.TicketError, match="variant_count"):
        ticket.parse(_write(tmp_path, DX_FIXTURE.replace("variant_count: 5", "variant_count: five")))


def test_parse_rejects_empty_repos_for_sow(tmp_path):
    body = textwrap.dedent("""\
        ---
        status: draft
        repos: []
        ---
        # Feature
        """)
    with pytest.raises(ticket.TicketError, match="repos"):
        ticket.parse(_write(tmp_path, body))


def test_parse_rejects_empty_repos_for_dx(tmp_path):
    with pytest.raises(ticket.TicketError, match="repos"):
        ticket.parse(_write(tmp_path, DX_FIXTURE.replace(
            "repos:\n  - prog-strength-web\n  - prog-strength-docs",
            "repos: []",
        )))


# --- parse: DX-specific validation (each a distinct message) ---------------


def test_parse_dx_rejects_missing_surface(tmp_path):
    with pytest.raises(ticket.TicketError, match="surface"):
        ticket.parse(_write(tmp_path, DX_FIXTURE.replace("surface: runs-list\n", "")))


def test_parse_dx_rejects_missing_idioms(tmp_path):
    body = DX_FIXTURE.replace(
        textwrap.dedent("""\
            idioms:
              - editorial-data-journalism
              - brutalist-monospace
              - warm-organic
              - terminal-dense
              - linear-minimal
            """).rstrip() + "\n",
        "",
    )
    # variant_count is now 5 with zero idioms -> idioms-missing message.
    with pytest.raises(ticket.TicketError, match="idiom"):
        ticket.parse(_write(tmp_path, body))


def test_parse_dx_rejects_fewer_idioms_than_variant_count(tmp_path):
    body = DX_FIXTURE.replace(
        textwrap.dedent("""\
            idioms:
              - editorial-data-journalism
              - brutalist-monospace
              - warm-organic
              - terminal-dense
              - linear-minimal
            """),
        textwrap.dedent("""\
            idioms:
              - editorial-data-journalism
              - brutalist-monospace
            """),
    )
    with pytest.raises(ticket.TicketError, match="variant_count"):
        ticket.parse(_write(tmp_path, body))


def test_parse_dx_rejects_empty_references(tmp_path):
    body = DX_FIXTURE.replace(
        textwrap.dedent("""\
            references:
              - Strava
              - Whoop
              - Linear
            """),
        "references: []\n",
    )
    with pytest.raises(ticket.TicketError, match="reference"):
        ticket.parse(_write(tmp_path, body))


def test_parse_dx_defaults_variant_count_to_five(tmp_path):
    # variant_count omitted -> defaults to 5; the 5-idiom fixture passes.
    body = DX_FIXTURE.replace("variant_count: 5\n", "")
    t = ticket.parse(_write(tmp_path, body))
    assert t.variant_count == 5


# --- prompt_template routing -----------------------------------------------


def test_prompt_template_routes_sow(tmp_path):
    t = ticket.parse(_write(tmp_path, SOW_FIXTURE))
    assert ticket.prompt_template(t) == "prompt.md.tpl"


def test_prompt_template_routes_dx(tmp_path):
    t = ticket.parse(_write(tmp_path, DX_FIXTURE))
    assert ticket.prompt_template(t) == "prompt-dx.md.tpl"


# --- substitutions ---------------------------------------------------------

SHARED_KEYS = {"__SOW_PATH__", "__SOW_SLUG__", "__GITHUB_ORG__", "__TODAY__"}
DX_KEYS = {"__SURFACE__", "__IDIOMS__", "__REFERENCES__", "__SCOPE__", "__VARIANT_COUNT__"}


def _subs(t):
    return ticket.substitutions(
        t,
        sow_path="dx/runs-list.md",
        sow_slug="runs-list",
        github_org="Prog-Strength",
        today="2026-06-16",
    )


def test_substitutions_sow_emits_only_shared_keys(tmp_path):
    t = ticket.parse(_write(tmp_path, SOW_FIXTURE))
    subs = _subs(t)
    assert set(subs) == SHARED_KEYS
    assert subs["__SOW_PATH__"] == "dx/runs-list.md"
    assert subs["__GITHUB_ORG__"] == "Prog-Strength"


def test_substitutions_dx_emits_shared_and_dx_keys(tmp_path):
    t = ticket.parse(_write(tmp_path, DX_FIXTURE))
    subs = _subs(t)
    assert set(subs) == SHARED_KEYS | DX_KEYS
    assert subs["__SURFACE__"] == "runs-list"
    assert subs["__VARIANT_COUNT__"] == "5"
    # Multi-value fields render single-line (sed-safe — no embedded newline).
    assert "\n" not in subs["__IDIOMS__"]
    assert "\n" not in subs["__REFERENCES__"]
    assert "editorial-data-journalism" in subs["__IDIOMS__"]
    assert "Strava" in subs["__REFERENCES__"]


# --- render (CLI glue) -----------------------------------------------------


def test_render_sow_uses_sow_template_and_substitutes(tmp_path):
    templates = tmp_path / "tpl"
    templates.mkdir()
    (templates / "prompt.md.tpl").write_text("SOW at __SOW_PATH__ slug __SOW_SLUG__ org __GITHUB_ORG__ today __TODAY__\n")
    (templates / "prompt-dx.md.tpl").write_text("DX __SURFACE__\n")
    out = tmp_path / "prompt.md"
    ticket.render(
        ticket_path=_write(tmp_path, SOW_FIXTURE),
        sow_path="sows/feature.md",
        sow_slug="feature",
        github_org="Prog-Strength",
        today="2026-06-16",
        templates_dir=str(templates),
        out_path=str(out),
    )
    text = out.read_text()
    assert text == "SOW at sows/feature.md slug feature org Prog-Strength today 2026-06-16\n"


def test_render_dx_uses_dx_template_and_substitutes(tmp_path):
    templates = tmp_path / "tpl"
    templates.mkdir()
    (templates / "prompt.md.tpl").write_text("SOW __SOW_PATH__\n")
    (templates / "prompt-dx.md.tpl").write_text(
        "explore __SURFACE__ x__VARIANT_COUNT__ idioms __IDIOMS__ refs __REFERENCES__ scope __SCOPE__ at __SOW_PATH__\n"
    )
    out = tmp_path / "prompt.md"
    ticket.render(
        ticket_path=_write(tmp_path, DX_FIXTURE),
        sow_path="dx/runs-list.md",
        sow_slug="runs-list",
        github_org="Prog-Strength",
        today="2026-06-16",
        templates_dir=str(templates),
        out_path=str(out),
    )
    text = out.read_text()
    assert "explore runs-list x5" in text
    assert "editorial-data-journalism" in text
    assert "dx/runs-list.md" in text
    assert "__SURFACE__" not in text  # all tokens replaced
