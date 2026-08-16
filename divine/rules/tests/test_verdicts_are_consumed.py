"""Guards that a declared verdict has a sink that acts on it.

`DeclareVerdict(verdict='x')` is inert unless some sink matches 'x'. A verdict
no sink consumes produces a rule that looks like working enforcement, fires,
records telemetry, and changes nothing — the failure mode this whole workstream
keeps running into. `rate_limit` was exactly that: declared by RapidPosting,
consumed by nothing, and aimed at a rate-limiting capability relay-manager does
not have.

The consumed set is read from the sink modules rather than restated here, so
narrowing a sink's set fails this test instead of silently orphaning a rule.

Pure stdlib: no osprey engine, no plugins, no network.
Run: `python3 -m pytest divine/rules/tests/`
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_RULES_ROOT = Path(__file__).resolve().parent.parent
_SINKS = _ROOT / 'plugins' / 'src' / 'services'

_DECLARE = re.compile(r"DeclareVerdict\(\s*verdict\s*=\s*'([^']+)'")
# Module-level literal sets whose names end in VERDICTS. Deliberately excludes
# VERDICT_SEVERITY, which is a dict of the same strings and would mask a sink
# that ranks a verdict without acting on it.
_VERDICT_SET = re.compile(r'^[A-Z_]*VERDICTS\s*=\s*\{([^}]*)\}', re.M)
_MEMBER = re.compile(r"'([^']+)'")

# Verdicts allowed to be unconsumed, each with the reason it is tolerated.
#
# Deliberately EMPTY. An earlier revision allowlisted 'suspend' from
# repeat_offender.sml; that rule is deleted in this same change, so no declared
# verdict is unconsumed and nothing needs excusing. Keep it empty unless there
# is a written reason -- an allowlist is where a real gap goes to look handled.
_UNCONSUMED_BY_DESIGN: set[str] = set()


def _strip_comments(text: str) -> str:
    return '\n'.join(line.split('#', 1)[0] for line in text.splitlines())


def _consumed() -> set[str]:
    out: set[str] = set()
    for path in sorted(_SINKS.glob('*.py')):
        for body in _VERDICT_SET.findall(_strip_comments(path.read_text())):
            out.update(_MEMBER.findall(body))
    return out


def _declared() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path in sorted(_RULES_ROOT.rglob('*.sml')):
        for verdict in _DECLARE.findall(_strip_comments(path.read_text())):
            out.setdefault(verdict, set()).add(str(path.relative_to(_RULES_ROOT)))
    return out


def test_sink_verdict_sets_were_actually_found() -> None:
    """Guard the guard.

    If the sink parse returns nothing, every verdict below reads as unconsumed
    and the test fails loudly — but if it returned an over-broad set, everything
    would pass silently. Pin the known members so a rename fails here.
    """
    consumed = _consumed()
    assert {'flag_for_review', 'restrict', 'auto_hide', 'ban'} <= consumed, (
        f'COOPSink.ACTIONABLE_VERDICTS not found or changed shape; parsed {consumed}'
    )
    assert 'dismiss' in consumed, f'ZendeskSink.RESOLUTION_VERDICTS not found or changed shape; parsed {consumed}'


def test_rules_declare_verdicts_at_all() -> None:
    """Guard the guard: an empty declared set would pass the check below."""
    assert _declared(), (
        'No DeclareVerdict calls found. Either _DECLARE no longer matches or the '
        'rules moved, and in both cases the check below is vacuous.'
    )


def test_every_declared_verdict_is_consumed_by_a_sink() -> None:
    consumed = _consumed()
    orphans = {
        verdict: sorted(files)
        for verdict, files in _declared().items()
        if verdict not in consumed and verdict not in _UNCONSUMED_BY_DESIGN
    }
    assert not orphans, (
        f'{orphans} declare verdicts no sink acts on. The rule will fire, record '
        f'telemetry and change nothing, which reads as working enforcement. Either '
        f'add the verdict to the consuming sink, change the rule to declare one '
        f'that is consumed, or add it to _UNCONSUMED_BY_DESIGN with the reason.'
    )
