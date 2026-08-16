"""Guards what may be passed to the account-age UDF.

`NostrAccountAge` returns `now - created_at`, and its docstring
(divine/plugins/src/udfs/nostr_account_age.py) says the argument is "the
created_at timestamp from a kind 0 metadata event". The bare name `CreatedAt`
is not that: divine/rules/models/base.sml binds it to `$.created_at`, the
timestamp of the event currently being evaluated.

Passing the latter produces a number that is near zero for every freshly
published event, so a rule reading `NostrAccountAge(created_at=CreatedAt) < 3600`
matches EVERY post from EVERY account regardless of how old the account is. That
shipped, and on staging it put 449 jobs into General Review; two of the flagged
accounts were 11.1 and 16.6 days old. See
support-trust-safety/docs/superpowers/specs/2026-08-16-retire-scaffolding-behavioral-rules-design.md

The rule that did this is gone, so the live tree has no call sites at all. A
matcher that quietly stopped matching would therefore leave this file green
while checking nothing, which is why the positive control below runs the matcher
against an inline copy of the deleted rule rather than against the tree.

Pure stdlib: no osprey engine, no plugins, no network.
Run: `python3 -m pytest divine/rules/tests/`
"""

import re
from pathlib import Path

_RULES_ROOT = Path(__file__).resolve().parent.parent

# `NostrAccountAge(created_at=<name>)`, tolerant of whitespace and line breaks.
_ACCOUNT_AGE_CALL = re.compile(r'NostrAccountAge\(\s*created_at\s*=\s*([A-Za-z_]\w*)')

# Names bound to the EVALUATED event's own timestamp. Passing any of these
# measures post age, not account age.
_EVENT_OWN_TIMESTAMP = {'CreatedAt'}

# The exact shape that shipped, kept verbatim as a positive control.
_KNOWN_BAD = '    NostrAccountAge(created_at=CreatedAt) < 3600,'


def _strip_comments(text: str) -> str:
    """Drop `#` line comments so an example in prose is not read as code."""
    return '\n'.join(line.split('#', 1)[0] for line in text.splitlines())


def _violations(text: str) -> list[str]:
    return [arg for arg in _ACCOUNT_AGE_CALL.findall(_strip_comments(text)) if arg in _EVENT_OWN_TIMESTAMP]


def test_matcher_detects_the_known_bad_shape() -> None:
    """Guard the guard.

    The tree-wide check below passes trivially when nothing calls the UDF, which
    is the expected steady state. This proves the matcher can still see the
    defect, so a regex that drifted fails HERE rather than going quiet there.
    """
    assert _violations(_KNOWN_BAD) == ['CreatedAt'], (
        '_ACCOUNT_AGE_CALL no longer matches the call shape that shipped, so the tree-wide check below is vacuous.'
    )


def test_matcher_accepts_a_kind0_derived_argument() -> None:
    """Negative control: the guard must not forbid correct usage."""
    assert _violations('NostrAccountAge(created_at=AuthorProfileCreatedAt) < 3600') == []


def test_no_rule_passes_the_events_own_timestamp_to_account_age() -> None:
    offenders = {
        path.relative_to(_RULES_ROOT): args
        for path in sorted(_RULES_ROOT.rglob('*.sml'))
        if (args := _violations(path.read_text()))
    }
    assert not offenders, (
        f"{offenders} pass the evaluated event's own timestamp to NostrAccountAge, "
        f'which measures how long ago the post was made rather than the age of the '
        f'account. Every freshly published event scores near zero, so the rule '
        f'matches everything. Pass a kind-0-derived timestamp instead — note the '
        f'bridge currently carries no kind-0 data, so that needs plumbing first.'
    )
