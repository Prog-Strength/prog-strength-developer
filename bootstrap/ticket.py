"""Ticket parsing, validation, and prompt routing for the worker.

A "work type" in this platform is three things: the ticket schema parsed
from frontmatter, the prompt template rendered for Claude, and the
branch/PR/merge contract that prompt enforces. This module owns the first
two — it parses + validates a ticket's YAML frontmatter, then routes to
the right prompt template and builds the ``__KEY__`` substitution map the
render consumes. Two types exist: ``sow`` (the default, convergent — one
spec, merges) and ``dx`` (divergent — N design variants, never merges).

``bootstrap/userdata.sh.tpl`` is the only production caller, via the CLI
at the bottom (``python3 -m bootstrap.ticket repos|render``). Keeping the
routing/validation here — rather than as more inline userdata bash —
follows the ``fleet`` package precedent and is what makes the seam
unit-testable. In particular it turns a malformed DX (fewer enumerated
idioms than variants) into an instant, legible boot-time failure instead
of a wasted six-hour run that produces five near-identical cards.

Must stay importable on the worker's system Python (3.9 on AL2023): keep
``from __future__ import annotations`` and avoid 3.10+ runtime syntax.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from typing import List, Optional

import yaml

TYPE_SOW = "sow"
TYPE_DX = "dx"
KNOWN_TYPES = (TYPE_SOW, TYPE_DX)

#: Number of variants a DX builds when ``variant_count`` is omitted. The
#: idiom list is the real driver; the field stays for an explicit default.
DEFAULT_VARIANT_COUNT = 5

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


class TicketError(Exception):
    """A ticket is malformed or fails work-type validation.

    Carries a single-line message; userdata prints it and terminates the
    worker before Claude ever runs.
    """


@dataclass
class Ticket:
    """A parsed, validated ticket.

    ``type``/``repos`` apply to every ticket; the remaining fields are
    populated only for ``type: dx`` (``None`` otherwise).
    """

    type: str
    repos: List[str]
    surface: Optional[str] = None
    idioms: Optional[List[str]] = None
    references: Optional[List[str]] = None
    scope: Optional[str] = None
    variant_count: Optional[int] = None
    body: str = field(default="", repr=False)


def parse(ticket_path: str) -> Ticket:
    """Load ``ticket_path``, validate its frontmatter, return a ``Ticket``.

    Raises ``TicketError`` (single-line message) on any malformed or
    invalid ticket — this is the fail-fast boot guard.
    """
    with open(ticket_path) as f:
        text = f.read()

    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise TicketError("ticket has no YAML frontmatter block")
    meta = yaml.safe_load(m.group(1)) or {}
    # A bare scalar/list frontmatter would make every meta.get() below blow
    # up with a raw AttributeError; keep the failure a single-line TicketError
    # so userdata surfaces it cleanly instead of a Python traceback.
    if not isinstance(meta, dict):
        raise TicketError("ticket frontmatter must be a YAML mapping")
    body = text[m.end():]

    ticket_type = meta.get("type") or TYPE_SOW
    if ticket_type not in KNOWN_TYPES:
        raise TicketError(
            "ticket type must be one of {}, got {!r}".format(", ".join(KNOWN_TYPES), ticket_type)
        )

    # Shared guard (moved in from userdata): the repos list is the worker's
    # clone list — an empty one means dispatch has nothing to check out.
    repos = list(meta.get("repos") or [])
    if not repos:
        raise TicketError("ticket frontmatter has empty repos:[]")

    t = Ticket(type=ticket_type, repos=repos, body=body)

    if ticket_type == TYPE_DX:
        _populate_and_validate_dx(t, meta)

    return t


def _populate_and_validate_dx(t: Ticket, meta: dict) -> None:
    """Fill + validate the DX-only fields. Each failure is a distinct,
    single-line message so the boot log says exactly what is wrong."""
    t.surface = meta.get("surface")
    if not t.surface:
        raise TicketError("DX ticket requires a 'surface' field naming the surface being explored")

    raw_count = meta.get("variant_count")
    if raw_count is None:
        t.variant_count = DEFAULT_VARIANT_COUNT
    else:
        # YAML may type this as a string ("5") or something non-numeric;
        # coerce here so the len() comparison below can't raise a raw
        # TypeError that escapes the single-line-message contract.
        try:
            t.variant_count = int(raw_count)
        except (TypeError, ValueError):
            raise TicketError(
                "DX ticket 'variant_count' must be an integer, got {!r}".format(raw_count)
            )

    t.idioms = list(meta.get("idioms") or [])
    if not t.idioms:
        raise TicketError(
            "DX ticket requires a non-empty 'idioms' list — without enumerated "
            "directions the variants collapse into near-duplicates"
        )
    if len(t.idioms) < t.variant_count:
        raise TicketError(
            "DX ticket has {} idioms but variant_count is {}; need at least "
            "variant_count idioms (one differentiated direction per variant)".format(
                len(t.idioms), t.variant_count
            )
        )

    t.references = list(meta.get("references") or [])
    if not t.references:
        raise TicketError(
            "DX ticket requires a non-empty 'references' list — name north-star "
            "products, not bare adjectives"
        )

    t.scope = meta.get("scope") or "in-system"


def prompt_template(ticket: Ticket) -> str:
    """Basename of the prompt template for this work type. The caller joins
    it with the templates dir; both files live in ``bootstrap/``."""
    return "prompt-dx.md.tpl" if ticket.type == TYPE_DX else "prompt.md.tpl"


def substitutions(
    ticket: Ticket,
    *,
    sow_path: str,
    sow_slug: str,
    github_org: str,
    today: str,
) -> dict:
    """The ``__KEY__`` -> value map applied to the chosen prompt template.

    The path/slug/org/today tokens are shared by both types and keep the
    ``__SOW_*__`` names regardless of type, so the render mechanism and the
    CloudWatch stream slug are identical for a DX and a SOW. DX additionally
    emits the surface/idioms/references/scope/variant-count tokens. Multi-
    value fields render as a single comma-separated line (no embedded
    newlines) so a value is safe for any line-oriented substitution.
    """
    subs = {
        "__SOW_PATH__": sow_path,
        "__SOW_SLUG__": sow_slug,
        "__GITHUB_ORG__": github_org,
        "__TODAY__": today,
    }
    if ticket.type == TYPE_DX:
        subs["__SURFACE__"] = ticket.surface or ""
        subs["__IDIOMS__"] = ", ".join(ticket.idioms or [])
        subs["__REFERENCES__"] = ", ".join(ticket.references or [])
        subs["__SCOPE__"] = ticket.scope or ""
        subs["__VARIANT_COUNT__"] = str(ticket.variant_count)
    return subs


def render(
    *,
    ticket_path: str,
    sow_path: str,
    sow_slug: str,
    github_org: str,
    today: str,
    templates_dir: str,
    out_path: str,
) -> Ticket:
    """Parse the ticket, pick its template, substitute every token, write
    the rendered prompt to ``out_path``. Returns the parsed ``Ticket``."""
    t = parse(ticket_path)
    template_path = os.path.join(templates_dir, prompt_template(t))
    with open(template_path) as f:
        rendered = f.read()
    for key, value in substitutions(
        t, sow_path=sow_path, sow_slug=sow_slug, github_org=github_org, today=today
    ).items():
        rendered = rendered.replace(key, value)
    with open(out_path, "w") as f:
        f.write(rendered)
    return t


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ticket", description="parse/route a worker ticket")
    sub = p.add_subparsers(dest="command", required=True)

    rp = sub.add_parser("repos", help="validate the ticket and print its repos, one per line")
    rp.add_argument("--ticket", required=True)

    rn = sub.add_parser("render", help="render the type-routed prompt template to --out")
    rn.add_argument("--ticket", required=True)
    rn.add_argument("--sow-path", required=True)
    rn.add_argument("--sow-slug", required=True)
    rn.add_argument("--github-org", required=True)
    rn.add_argument("--today", required=True)
    rn.add_argument("--templates-dir", required=True)
    rn.add_argument("--out", required=True)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "repos":
            t = parse(args.ticket)
            for repo in t.repos:
                print(repo)
            return 0
        if args.command == "render":
            render(
                ticket_path=args.ticket,
                sow_path=args.sow_path,
                sow_slug=args.sow_slug,
                github_org=args.github_org,
                today=args.today,
                templates_dir=args.templates_dir,
                out_path=args.out,
            )
            return 0
    except TicketError as exc:
        # Single line to stderr so userdata can surface it verbatim.
        print("ticket error: {}".format(exc), file=sys.stderr)
        return 1
    return 1  # unreachable: argparse enforces a known command


if __name__ == "__main__":
    raise SystemExit(main())
