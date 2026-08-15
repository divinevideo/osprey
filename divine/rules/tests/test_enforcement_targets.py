"""Guards which pubkey the SML rules are allowed to enforce against.

`BanNostrEvent` takes an event id and, optionally, a pubkey. The pubkey argument
is the irreversible half: `RelayManagerSink` only issues the `banpubkey` RPC when
it is non-empty (see divine/plugins/src/services/relay_manager_sink.py).

Only a pubkey that comes from an event record may go in that argument -- either the
evaluated event's own author (`Pubkey`) or the reported event's author resolved from
that event and bound to the requested id (`ReportedAuthorPubkey`). A value the
reporter wrote into the report's tags (`ReportedPubkey`) is a claim about someone
else and must never reach it, so rules acting on reports pass `pubkey=''` and let
the `auto_hide` verdict take the account decision to a human via COOP.

Neither permitted value is Schnorr-verified here: the bridge copies `pubkey` off the
event as the relay delivered it, and resolution of the reported author does not check
the signature either (see divine/plugins/src/reported_author.py). They are
relay-attested, not proven. The distinction this file enforces is therefore
event-record-derived versus reporter-asserted, which is the line that actually holds.

The escalation ladder in behavioral/repeat_offender.sml turns account labels into
that same ban, so the ladder's labels are held to the same standard. `labels.yaml` is
the enforcing gate (`valid_for`); the call sites are checked here too so a widened
`valid_for` and a new call site each fail on their own.

Parsed from the live .sml files rather than a maintained list, so a new rule that
enforces against a reporter-asserted value fails here instead of shipping.

Pure stdlib: no osprey engine, no plugins, no network.
Run: `python3 -m pytest divine/rules/tests/`
"""

import re
from pathlib import Path

_RULES_ROOT = Path(__file__).resolve().parent.parent
_LABELS_YAML = _RULES_ROOT / 'config' / 'labels.yaml'
_AUTO_HIDE_RULES = _RULES_ROOT / 'rules' / 'reports' / 'auto_hide.sml'
_FIRST_REPORT_RULES = _RULES_ROOT / 'rules' / 'reports' / 'first_report_review.sml'

# Values allowed in `BanNostrEvent(pubkey=...)`.
#   ''                    -- event-level ban only; the account decision goes to a human.
#   Pubkey                -- the author recorded on the evaluated event itself.
#   ReportedAuthorPubkey  -- resolved from the reported event itself rather than taken
#                            from the report, and '' whenever it could not be trusted.
# Anything else is reporter- or label-supplied and must not reach an account ban.
_ALLOWED_PUBKEY_ARGS = {"''", '""', 'Pubkey', 'ReportedAuthorPubkey'}

# Labels behavioral/repeat_offender.sml escalates on, ending in BanNostrEvent.
_LADDER_LABELS = {'warned', 'suspended', 'banned'}

_PUBKEY_ARG = re.compile(r'\bpubkey\s*=\s*([^,)]+)')
_ENTITY_ARG = re.compile(r'\bentity\s*=\s*(\w+)')
_LABEL_ARG = re.compile(r'\blabel\s*=\s*\'([^\']+)\'')

# `Kind == 1984` and `Kind in [1984]` are both idiomatic here, so match either rather
# than one spelling -- a rule that escapes this check is invisible to every test below.
_REPORT_KIND = re.compile(r'\bKind\b[^\n]*\b1984\b')


def _sml_files():
    """Every rule and model file, not just rules/reports -- placement must not matter."""
    return sorted(_RULES_ROOT.rglob('*.sml'))


def _strip_comments(text):
    """Drop `#` line comments so an example inside a comment is not parsed as code."""
    return '\n'.join(line.split('#', 1)[0] for line in text.splitlines())


def _rel(path):
    return path.relative_to(_RULES_ROOT)


def _sources():
    return [(path, _strip_comments(path.read_text())) for path in _sml_files()]


def _acts_on_reports(text):
    """A file is report-driven if it matches on kind 1984, wherever it lives."""
    return _REPORT_KIND.search(text) is not None


def _call_args(text, name):
    """Argument text of each `name(...)` call, tracking nesting so `TimeDelta(days=7)`
    does not truncate the arguments that follow it. Keyword order is not assumed."""
    found = []
    for match in re.finditer(rf'\b{name}\s*\(', text):
        depth, start = 1, match.end()
        for index in range(start, len(text)):
            if text[index] == '(':
                depth += 1
            elif text[index] == ')':
                depth -= 1
                if depth == 0:
                    found.append(text[start:index])
                    break
        else:
            raise AssertionError(f'unbalanced {name}( in rules -- parser cannot read this call')
    return found


def _ban_calls():
    """(path, pubkey_argument, acts_on_reports) for every BanNostrEvent call."""
    calls = []
    for path, text in _sources():
        for args in _call_args(text, 'BanNostrEvent'):
            match = _PUBKEY_ARG.search(args)
            calls.append((path, match.group(1).strip() if match else None, _acts_on_reports(text)))
    return calls


def _label_adds():
    """(path, entity, label) for every LabelAdd call, in whatever keyword order."""
    adds = []
    for path, text in _sources():
        for args in _call_args(text, 'LabelAdd'):
            entity, label = _ENTITY_ARG.search(args), _LABEL_ARG.search(args)
            adds.append((path, entity.group(1) if entity else None, label.group(1) if label else None))
    return adds


def test_rules_are_present():
    """Guard the parsers themselves: a path typo must not silently pass every test here."""
    assert _sml_files(), f'no .sml rules found under {_RULES_ROOT}'
    assert _ban_calls(), 'no BanNostrEvent calls found -- parser is out of step with the rules'
    assert _label_adds(), 'no LabelAdd calls found -- parser is out of step with the rules'
    assert any(acts_on_reports for _, _, acts_on_reports in _ban_calls()), (
        'no report-driven BanNostrEvent found -- kind-1984 detection is out of step with the rules'
    )
    unread = [f'{_rel(path)}: entity={entity}, label={label}' for path, entity, label in _label_adds() if not label]
    assert not unread, f'LabelAdd calls whose label could not be read: {unread}'


def test_ban_pubkey_argument_is_always_supplied():
    """`pubkey` is required, and omitting it would read as an intentional no-op."""
    missing = [str(_rel(path)) for path, arg, _ in _ban_calls() if arg is None]
    assert not missing, f'BanNostrEvent called without an explicit pubkey argument in: {missing}'


def test_account_bans_only_target_event_derived_pubkeys():
    offenders = [f'{_rel(path)}: pubkey={arg}' for path, arg, _ in _ban_calls() if arg not in _ALLOWED_PUBKEY_ARGS]
    assert not offenders, (
        'BanNostrEvent may only ban a pubkey taken from an event record -- the evaluated '
        "event's author (Pubkey), the reported event's resolved author "
        f"(ReportedAuthorPubkey), or no account at all (pubkey=''); found {offenders}"
    )


def test_report_driven_rules_never_ban_a_claimed_account():
    """Rules firing on kind-1984 reports may not ban an account the report merely named.

    Today they all pass '' and leave the account to a human. `ReportedAuthorPubkey` is
    also permitted: it is resolved from the reported event rather than read out of the
    report, so it is not a claim. `ReportedPubkey` is a claim, and is what this guards.
    """
    allowed = {"''", '""', 'ReportedAuthorPubkey'}
    offenders = [
        f'{_rel(path)}: pubkey={arg}'
        for path, arg, acts_on_reports in _ban_calls()
        if acts_on_reports and arg not in allowed
    ]
    assert not offenders, (
        'rules acting on kind-1984 reports must ban either no account or the resolved '
        f'author, never a pubkey the report claimed; found {offenders}'
    )


def test_trusted_auto_hide_dedups_later_first_report_enforcement():
    """Either report order must enforce each category once without swallowing another."""
    trusted = _strip_comments(_AUTO_HIDE_RULES.read_text())
    first = _strip_comments(_FIRST_REPORT_RULES.read_text())

    # FirstCsamReport is the only CSAM path, including trusted reporters.
    first_csam = first.split('FirstCsamReport = Rule(', 1)[1].split('FirstIllegalReport = Rule(', 1)[0]
    assert "ReportReason == 'csam'" not in trusted
    assert "not HasLabel(entity=Pubkey, label='trusted_reporter')" not in first_csam

    # Review-item state must not pre-empt the stronger trusted illegal action.
    assert "ReportReason == 'illegal'" in trusted
    assert "not HasLabel(entity=ReportedEventId, label='illegal_auto_hidden')" in trusted
    assert "LabelAdd(entity=ReportedEventId, label='illegal_auto_hidden')" in trusted
    assert "LabelAdd(entity=ReportedEventId, label='illegal_reported')" in trusted
    assert "not HasLabel(entity=ReportedEventId, label='illegal_reported')" in first

    # Enforcement state is shared across categories, so it must never suppress a
    # later allegation of a different kind.
    assert "not HasLabel(entity=ReportedEventId, label='auto_hidden')" not in first


def test_escalation_labels_are_not_applied_to_reported_entities():
    """The ladder ends in an account ban, so its labels need the same provenance."""
    offenders = [
        f'{_rel(path)}: LabelAdd(entity={entity}, label={label!r})'
        for path, entity, label in _label_adds()
        if label in _LADDER_LABELS and entity.startswith('Reported')
    ]
    assert not offenders, f'escalation-ladder labels may only be applied to an event-derived entity; found {offenders}'


def test_escalation_labels_are_not_valid_for_reported_entities():
    """labels.yaml is the enforcing gate; keep it from being widened to a reported entity."""
    text = _LABELS_YAML.read_text()
    entry = re.compile(r'^  (\w+):\s*\n\s*valid_for:\s*\[([^\]]*)\]', re.MULTILINE)
    found = dict(entry.findall(text))
    assert _LADDER_LABELS <= set(found), (
        f'labels.yaml parse is out of step: expected {sorted(_LADDER_LABELS)} in {sorted(found)}'
    )

    offenders = [
        f'{label}: valid_for includes {scope.strip()}'
        for label in sorted(_LADDER_LABELS)
        for scope in found[label].split(',')
        if scope.strip().startswith('Reported')
    ]
    assert not offenders, (
        f'escalation-ladder labels must not be valid for a reporter-supplied entity; found {offenders}'
    )
