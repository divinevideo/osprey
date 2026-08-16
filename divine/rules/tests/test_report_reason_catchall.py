"""Guards that every report reason reaches a moderator through some rule.

A kind 1984 report whose reason matches no rule declares no verdict, so
`COOPSink.will_do_work` returns false and nothing is submitted: the report is
recorded in ClickHouse and is invisible to every moderator. That is what
`copyright` did, and what any reason a client adds tomorrow would do.

`FirstOtherReport` is the catch-all that closes it. It matches by EXCLUSION --
every reason except the ones another rule already owns -- so a new client-side
reason is caught by default rather than silently dropped.

The exclusion list has to be exactly right in both directions:
  * too few excluded -> two rules fire on one report and both declare
    flag_for_review, producing a duplicate Coop item;
  * too many excluded -> the reason falls through to nothing again, which is
    the bug this rule exists to prevent.

So this file does not restate the list. It derives it from the live rules plus
the bridge's ownership table and asserts the rule agrees, which means adding a
rule for a reason without removing it from the catch-all fails here.

Pure stdlib: no osprey engine, no plugins, no network.
Run: `python3 -m pytest divine/rules/tests/`
"""

import ast
import re
from pathlib import Path

_REPORT_RULES = Path(__file__).resolve().parent.parent / 'rules' / 'reports'
_BRIDGE_MAIN = Path(__file__).resolve().parent.parent.parent / 'nostr-kafka-bridge' / 'main.py'

_EQ = re.compile(r"ReportReason\s*==\s*'([^']+)'")
_IN = re.compile(r'ReportReason\s+in\s*\[([^\]]+)\]')
_NOT_IN = re.compile(r'ReportReason\s+not\s+in\s*\[([^\]]+)\]')
_MEMBER = re.compile(r"'([^']+)'")


def _rule_text() -> str:
    """All report rules, line comments stripped.

    Comments are stripped so a reason named in prose cannot inject a phantom
    token -- these files discuss reasons at length.
    """
    parts = []
    for path in sorted(_REPORT_RULES.glob('*.sml')):
        parts.extend(line.split('#', 1)[0] for line in path.read_text().splitlines())
    return '\n'.join(parts)


def _positively_matched() -> set[str]:
    """Reasons some rule matches by name, i.e. that already have an owner."""
    text = _rule_text()
    tokens = set(_EQ.findall(text))
    for group in _IN.findall(text):
        tokens.update(_MEMBER.findall(group))
    return tokens


def _excluded() -> set[str]:
    """Reasons the catch-all deliberately does not claim."""
    tokens: set[str] = set()
    for group in _NOT_IN.findall(_rule_text()):
        tokens.update(_MEMBER.findall(group))
    return tokens


def _canonical_reasons() -> dict[str, str]:
    """The bridge's reason -> downstream-owner table, read from source.

    Parsed with `ast` rather than imported: importing the bridge pulls in kafka
    and websocket, which this suite deliberately does not depend on.
    """
    module = ast.parse(_BRIDGE_MAIN.read_text())
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == 'CANONICAL_REASONS' for t in node.targets):
            continue
        return ast.literal_eval(node.value)
    raise AssertionError(f'CANONICAL_REASONS not found in {_BRIDGE_MAIN}')


def _owned_elsewhere() -> set[str]:
    """Reasons a system other than Osprey owns; Osprey must not queue them."""
    return {token for token, owner in _canonical_reasons().items() if owner != 'osprey-rule'}


def test_rule_parse_finds_the_named_rules() -> None:
    """Guard the guard: an empty parse would make every check below vacuous."""
    matched = _positively_matched()
    assert {'csam', 'harassment', 'nudity', 'violence'} <= matched, (
        f'ReportReason parse found {matched}; either the rules moved or the regexes no longer match'
    )


def test_canonical_reasons_parse_finds_the_ownership_table() -> None:
    """Guard the guard, for the half of the derivation that lives in the bridge."""
    reasons = _canonical_reasons()
    assert reasons.get('other') == 'osprey-rule', f'parsed CANONICAL_REASONS unexpectedly: {reasons}'
    assert 'relay-manager' in reasons.values(), (
        f'no externally-owned reason parsed, so the exclusion check cannot detect one: {reasons}'
    )


def test_a_catch_all_exclusion_exists() -> None:
    assert _excluded(), (
        'No `ReportReason not in [...]` condition in divine/rules/rules/reports/. '
        'Without it, a reason no rule names declares no verdict, so COOPSink '
        'submits nothing and the report reaches no moderator.'
    )


def test_catch_all_excludes_exactly_the_reasons_another_owner_handles() -> None:
    """The whole correctness of the catch-all is this one set equality.

    Anything excluded but unowned is a silent drop; anything owned but not
    excluded is a duplicate Coop item from two rules firing on one report.
    """
    expected = _positively_matched() | _owned_elsewhere()
    excluded = _excluded()
    assert excluded == expected, (
        f'catch-all exclusion list is wrong.\n'
        f'  excluded but nothing else handles them (silent drop): {sorted(excluded - expected)}\n'
        f'  handled elsewhere but not excluded (duplicate item): {sorted(expected - excluded)}'
    )


def test_uncategorised_reasons_are_caught() -> None:
    """The reasons that motivated the widening, plus one nobody has invented yet.

    `copyright` is live from divine-web today; `hate` is divine-mobile #7636.
    Neither has a rule, so both must fall to the catch-all rather than out of it.
    """
    excluded = _excluded()
    for reason in ('other', 'copyright', 'hate', 'a_reason_no_client_has_shipped_yet'):
        assert reason not in excluded, f"'{reason}' is excluded from the catch-all but no rule handles it"
