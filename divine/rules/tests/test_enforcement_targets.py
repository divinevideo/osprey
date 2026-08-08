"""Guards which pubkey the SML rules are allowed to enforce against.

`BanNostrEvent` takes an event id and, optionally, a pubkey. The pubkey argument
is the irreversible half: `RelayManagerSink` only issues the `banpubkey` RPC when
it is non-empty (see divine/plugins/src/services/relay_manager_sink.py).

Only a pubkey the relay has verified may go in that argument. `Pubkey` qualifies:
it is the signer of the event being evaluated, so the signature proves it. Values
carried in a kind-1984 report's tags do not qualify -- they are what the reporter
said, not what the relay checked -- so rules acting on reports pass `pubkey=''`
and let the `auto_hide` verdict take the account decision to a human via COOP.

The escalation ladder in behavioral/repeat_offender.sml turns account labels into
that same ban, so the ladder's labels are held to the same standard: they may only
be applied to an entity the signature proves. `labels.yaml` is the enforcing gate
(`valid_for`); the call sites are checked here too so a widened `valid_for` and a
new call site each fail on their own.

Parsed from the live .sml files rather than a maintained list, so a new rule that
enforces against an unverified value fails here instead of shipping.

Pure stdlib: no osprey engine, no plugins, no network.
Run: `python3 -m pytest divine/rules/tests/`
"""

import re
from pathlib import Path

_RULES_ROOT = Path(__file__).resolve().parent.parent
_LABELS_YAML = _RULES_ROOT / 'config' / 'labels.yaml'

# Values allowed in `BanNostrEvent(pubkey=...)`.
#   ''       -- event-level ban only; the account decision goes to a human.
#   Pubkey   -- the signer of the evaluated event, proven by its signature.
# Anything else is reporter- or label-supplied and must not reach an account ban.
_ALLOWED_PUBKEY_ARGS = {"''", '""', 'Pubkey'}

# Labels behavioral/repeat_offender.sml escalates on, ending in BanNostrEvent.
_LADDER_LABELS = {'warned', 'suspended', 'banned'}

_BAN_CALL = re.compile(r'BanNostrEvent\s*\((.*?)\)', re.DOTALL)
_PUBKEY_ARG = re.compile(r'\bpubkey\s*=\s*([^,)]+)')
_LABEL_ADD = re.compile(r'LabelAdd\s*\(\s*entity\s*=\s*(\w+)\s*,\s*label\s*=\s*\'([^\']+)\'')


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
    return re.search(r'Kind\s*==\s*1984', text) is not None


def _ban_calls():
    """(path, pubkey_argument, acts_on_reports) for every BanNostrEvent call."""
    calls = []
    for path, text in _sources():
        for args in _BAN_CALL.findall(text):
            match = _PUBKEY_ARG.search(args)
            calls.append((path, match.group(1).strip() if match else None, _acts_on_reports(text)))
    return calls


def _label_adds():
    """(path, entity, label) for every LabelAdd call."""
    return [(path, entity, label) for path, text in _sources() for entity, label in _LABEL_ADD.findall(text)]


def test_rules_are_present():
    """Guard the parsers themselves: a path typo must not silently pass every test here."""
    assert _sml_files(), f'no .sml rules found under {_RULES_ROOT}'
    assert _ban_calls(), 'no BanNostrEvent calls found -- parser is out of step with the rules'
    assert _label_adds(), 'no LabelAdd calls found -- parser is out of step with the rules'
    assert any(acts_on_reports for _, _, acts_on_reports in _ban_calls()), (
        'no report-driven BanNostrEvent found -- kind-1984 detection is out of step with the rules'
    )


def test_ban_pubkey_argument_is_always_supplied():
    """`pubkey` is required, and omitting it would read as an intentional no-op."""
    missing = [str(_rel(path)) for path, arg, _ in _ban_calls() if arg is None]
    assert not missing, f'BanNostrEvent called without an explicit pubkey argument in: {missing}'


def test_account_bans_only_target_verified_pubkeys():
    offenders = [f'{_rel(path)}: pubkey={arg}' for path, arg, _ in _ban_calls() if arg not in _ALLOWED_PUBKEY_ARGS]
    assert not offenders, (
        'BanNostrEvent may only ban the signer of the evaluated event (Pubkey) or no '
        f"account at all (pubkey=''); found {offenders}"
    )


def test_report_driven_rules_never_ban_an_account():
    """Rules that fire on kind-1984 reports hide the event; a human decides the account."""
    offenders = [
        f'{_rel(path)}: pubkey={arg}'
        for path, arg, acts_on_reports in _ban_calls()
        if acts_on_reports and arg not in ("''", '""')
    ]
    assert not offenders, f"rules acting on kind-1984 reports must pass pubkey='' to BanNostrEvent; found {offenders}"


def test_escalation_labels_are_not_applied_to_reported_entities():
    """The ladder ends in an account ban, so its labels need the same provenance."""
    offenders = [
        f'{_rel(path)}: LabelAdd(entity={entity}, label={label!r})'
        for path, entity, label in _label_adds()
        if label in _LADDER_LABELS and entity.startswith('Reported')
    ]
    assert not offenders, (
        f'escalation-ladder labels may only be applied to an entity the signature proves; found {offenders}'
    )


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
