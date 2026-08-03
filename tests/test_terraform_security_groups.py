"""Guards on how security-group rules are declared in terraform/.

Terraform's `aws_security_group` treats an inline `ingress`/`egress` block
as the AUTHORITATIVE list for that direction: on every apply it revokes any
rule of that direction it finds on the group but not in the config. So a
standalone `aws_security_group_rule` pointing at a group that also declares
inline rules of the same direction gets silently deleted on the next apply.

That is not hypothetical here. The manager SG's Loki :3100 ingress was
declared standalone alongside inline ingress blocks, and every apply from
2026-06-16 onward revoked it (CloudTrail RevokeSecurityGroupIngress,
sg-0ce94c743dd010f68, 3100/3100), killing the dashboard's live Claude log
tail. The rules are only safe standalone when the group declares NO inline
block for that direction — which is why the worker SG's 9100/9101 rules
survive (it declares egress inline, never ingress).
"""

import re
from pathlib import Path

TERRAFORM_DIR = Path(__file__).parent.parent / "terraform"

# `#` and `//` comments would otherwise contribute stray braces/keywords to
# the block scan; terraform has no block comments in this repo.
_COMMENT = re.compile(r"(^|\s)(#|//).*$", re.MULTILINE)
_RESOURCE = re.compile(r'resource\s+"([\w-]+)"\s+"([\w-]+)"\s*\{')


def _strip_comments(text: str) -> str:
    return _COMMENT.sub("", text)


def _block_body(text: str, open_brace_index: int) -> str:
    """Return the body of the block whose opening `{` is at the given index."""
    depth = 0
    for i in range(open_brace_index, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace_index + 1 : i]
    raise AssertionError(f"unbalanced braces starting at offset {open_brace_index}")


def _resources() -> dict[tuple[str, str], str]:
    """Map (resource_type, resource_name) -> block body across terraform/*.tf."""
    found: dict[tuple[str, str], str] = {}
    for path in sorted(TERRAFORM_DIR.glob("*.tf")):
        text = _strip_comments(path.read_text())
        for match in _RESOURCE.finditer(text):
            body = _block_body(text, match.end() - 1)
            found[(match.group(1), match.group(2))] = body
    return found


def _declares_inline(body: str, direction: str) -> bool:
    return re.search(rf"(^|\n)\s*{direction}\s*\{{", body) is not None


def _attr(body: str, name: str) -> str | None:
    match = re.search(rf'(^|\n)\s*{name}\s*=\s*"?([^"\n]+)"?', body)
    return match.group(2).strip() if match else None


def test_terraform_dir_is_parsed():
    """Guard the guard: a parser that silently finds nothing proves nothing."""
    resources = _resources()
    assert ("aws_security_group", "manager") in resources
    assert ("aws_security_group", "worker") in resources


def test_no_standalone_rule_targets_a_group_with_inline_rules():
    resources = _resources()

    inline = {
        (name, direction)
        for (rtype, name), body in resources.items()
        if rtype == "aws_security_group"
        for direction in ("ingress", "egress")
        if _declares_inline(body, direction)
    }

    violations = []
    for (rtype, rname), body in resources.items():
        if rtype != "aws_security_group_rule":
            continue
        direction = _attr(body, "type")
        target = _attr(body, "security_group_id") or ""
        target_match = re.match(r"aws_security_group\.([\w-]+)\.id", target)
        if not target_match or direction is None:
            continue
        if (target_match.group(1), direction) in inline:
            violations.append(
                f"aws_security_group_rule.{rname} ({direction}) targets "
                f"aws_security_group.{target_match.group(1)}, which declares "
                f"inline {direction} blocks — the next apply will revoke it"
            )

    assert not violations, "\n".join(violations)


def test_manager_security_group_admits_loki_pushes_from_workers():
    """The live Claude log tail's network path, pinned where it can't drift.

    Promtail on each worker pushes to Loki on the manager over :3100. Without
    this rule the pushes time out and the "Live Claude output" panel is empty
    even though every other layer is healthy.
    """
    body = _resources()[("aws_security_group", "manager")]

    # Match on offsets, not on the matched text: every `ingress {` opener is
    # the identical string, so a text-keyed lookup would score the first
    # block N times and never see the rest.
    blocks = [
        _block_body(body, match.end() - 1)
        for match in re.finditer(r"(?:^|\n)\s*ingress\s*\{", body)
    ]
    loki_ingress = [block for block in blocks if _attr(block, "from_port") == "3100"]
    assert loki_ingress, "manager SG declares no inline ingress for Loki :3100"

    block = loki_ingress[0]
    assert _attr(block, "to_port") == "3100"
    assert _attr(block, "protocol") == "tcp"
    # Source is the worker SG, never a CIDR — Loki has auth_enabled: false.
    assert "aws_security_group.worker.id" in (_attr(block, "security_groups") or "")
