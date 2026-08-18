"""Guards that every label the SML rules use is registered in labels.yaml.

The worker validates sources at startup and REFUSES to compile the execution
graph when a LabelAdd names a label that labels.yaml does not declare (or an
entity type the label is not valid_for) -- the engine validator raises
ValidationFailed and the process dies before processing a single event. None of
the CI jobs load the rules through the engine: the integration workflow checks
file reachability, and the rules tests are stdlib text tests. That is how an
unregistered label passed every check while crashing the worker on boot.

This file closes that gap from the text side: parse every LabelAdd/HasLabel call
in the live .sml tree and fail unless labels.yaml registers the label and admits
the entity's type. It covers HasLabel reads inside when_all guards too, which
the engine's own ValidateLabels does not visit -- so it is deliberately stricter
than the crash check it backs up.

Parsed from the live files rather than a maintained list, so the next rule that
ships without its label fails here instead of in a crash loop.
Pure stdlib: no osprey engine, no plugins, no network.
Run: `python3 -m pytest divine/rules/tests/`
"""

import re
from pathlib import Path

_RULES_ROOT = Path(__file__).resolve().parent.parent
_LABELS_YAML = _RULES_ROOT / 'config' / 'labels.yaml'

# `  label_name:\n    valid_for: [A, B]` -- same shape the enforcing gate in
# test_enforcement_targets.py parses, so a labels.yaml format change fails both
# files rather than silently emptying one.
_ENTRY = re.compile(r'^  (\w+):\s*\n\s+valid_for:\s*\[([^\]]*)\]', re.MULTILINE)

# LabelAdd(entity=Name, label='lit') and HasLabel(entity=Name, label='lit').
_LABEL_CALL = re.compile(r"\b(LabelAdd|HasLabel)\(\s*entity=(\w+)\s*,\s*label='([^']+)'")

# Entity feature declarations, both spellings:
#   Name: Entity[str] = EntityJson(type='T', ...)
#   Name: Entity[str] = Entity(type='T', id=...)
_ENTITY_DECL = re.compile(r"^(\w+):\s*Entity\[[^\]]*\]\s*=\s*Entity(?:Json)?\(\s*type='(\w+)'", re.MULTILINE)


def _strip_comments(text: str) -> str:
    """Drop `#` line comments so a label named in prose is not parsed as a call."""
    return '\n'.join(line.split('#', 1)[0] for line in text.splitlines())


def _sml_sources() -> dict[str, str]:
    """Every .sml under the rules root, comments stripped, keyed by relative path."""
    return {str(p.relative_to(_RULES_ROOT)): _strip_comments(p.read_text()) for p in sorted(_RULES_ROOT.rglob('*.sml'))}


def _entity_types() -> dict[str, str]:
    """Feature name -> entity type, from every Entity/EntityJson declaration."""
    types: dict[str, str] = {}
    for text in _sml_sources().values():
        for name, entity_type in _ENTITY_DECL.findall(text):
            types[name] = entity_type
    return types


def _label_calls() -> list[tuple[str, str, str, str]]:
    """(file, udf, entity feature name, label literal) for every label call."""
    calls = []
    for path, text in _sml_sources().items():
        for udf, entity, label in _LABEL_CALL.findall(text):
            calls.append((path, udf, entity, label))
    return calls


def _registered() -> dict[str, set[str]]:
    found = dict(_ENTRY.findall(_LABELS_YAML.read_text()))
    return {name: {t.strip() for t in valid.split(',')} for name, valid in found.items()}


def test_the_parsers_still_see_the_tree():
    """Guard the guard: a regex drift must fail here, not pass the file vacuously."""
    registered = _registered()
    calls = _label_calls()
    types = _entity_types()
    assert len(registered) >= 10, f'labels.yaml parse found only {len(registered)} entries'
    assert len(calls) >= 10, f'.sml parse found only {len(calls)} label calls'
    assert {'EventId', 'ReportedEventId', 'Pubkey', 'ReportedPubkey'} <= set(types.values()), (
        f'entity declaration parse lost known types; saw {sorted(types.values())}'
    )
    assert any(udf == 'LabelAdd' for _, udf, _, _ in calls), 'no LabelAdd found; the write-side check is vacuous'
    assert any(udf == 'HasLabel' for _, udf, _, _ in calls), 'no HasLabel found; the read-side check is vacuous'


def test_every_label_the_rules_use_is_registered():
    missing = {
        f'{path}: {udf}(entity={entity}, label={label!r})'
        for path, udf, entity, label in _label_calls()
        if label not in _registered()
    }
    assert not missing, (
        'labels.yaml does not register these labels, so the worker refuses to '
        f'compile its rules at startup: {sorted(missing)}'
    )


def test_every_label_call_targets_a_declared_entity():
    types = _entity_types()
    undeclared = {
        f'{path}: {udf}(entity={entity}, ...) -- {entity} is not a declared Entity feature'
        for path, udf, entity, _ in _label_calls()
        if entity not in types
    }
    assert not undeclared, f'label calls against undeclared entities: {sorted(undeclared)}'


def test_every_label_call_is_valid_for_its_entity_type():
    registered = _registered()
    types = _entity_types()
    offenders = [
        f'{path}: {udf}(entity={entity}, label={label!r}) -- {types[entity]} not in valid_for {sorted(registered[label])}'
        for path, udf, entity, label in _label_calls()
        if label in registered and entity in types and types[entity] not in registered[label]
    ]
    assert not offenders, f'label/entity pairs labels.yaml does not admit: {offenders}'
