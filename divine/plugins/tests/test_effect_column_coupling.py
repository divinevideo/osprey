"""Every effect feature must reach ClickHouse, which takes TWO things, not one.

An effect that reaches the output sink needs both:

  1. a column on `osprey.osprey_events`, or the insert is rejected, and
  2. an entry in `ClickHouseOutputSink._PASSTHROUGH_INTERNAL_KEYS`, or the sink
     drops the value on the floor.

Miss (1) and the failure is loud: the sink rejects the whole batch, so telemetry
disappears for every action in it, not just the one carrying the new effect.

Miss (2) and the failure is silent, which is worse. The enforcement still
happens, the column sits there empty, and the record of what Osprey did is
simply absent. That is the state `__age_restrict_nostr_event` shipped in: a
commit added its column, CI grew a check asserting the column existed, and
nothing ever wrote it. The CI check passed throughout, because it verified one
half of a two-sided coupling.

So this asserts both halves together. It parses the files rather than importing
the engine, in the style of test_enforcement_targets.py, so it runs in the
existing plugin-test step with no Osprey imports.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
UDF_DIR = REPO_ROOT / 'divine' / 'plugins' / 'src' / 'udfs'
RULES_DIR = REPO_ROOT / 'divine' / 'rules'
SCHEMA = REPO_ROOT / 'divine' / 'clickhouse-schema' / '001_osprey_events.sql'
SINK = (
    REPO_ROOT
    / 'osprey_worker'
    / 'src'
    / 'osprey'
    / 'worker'
    / 'sinks'
    / 'sink'
    / 'clickhouse_output_sink.py'
)

# CustomExtractedFeature subclasses declare their wire name in a feature_name
# classmethod. That name is prefixed with '__' by the execution context.
FEATURE_NAME_RE = re.compile(
    r"def feature_name\(cls\)[^:]*:\s*\n\s*return '([^']+)'", re.MULTILINE
)


def effect_feature_names() -> set[str]:
    names: set[str] = set()
    for path in sorted(UDF_DIR.glob('*.py')):
        names |= set(FEATURE_NAME_RE.findall(path.read_text()))
    return names


def test_there_is_at_least_one_effect_feature_to_check() -> None:
    """Guard the guard.

    Every assertion below is a loop over discovered names. If the regex stops
    matching -- a refactor to a class attribute, a change in quoting -- the
    loops become empty and the whole file passes while checking nothing. That
    is the exact failure mode this suite exists to catch, so it must not be
    possible here either.
    """
    assert effect_feature_names(), (
        f'No effect features discovered under {UDF_DIR}. Either there are none '
        f'(unlikely) or FEATURE_NAME_RE no longer matches how they are declared.'
    )


@pytest.mark.parametrize('feature', sorted(effect_feature_names()))
def test_effect_feature_has_a_clickhouse_column(feature: str) -> None:
    schema = SCHEMA.read_text()
    column = f'__{feature}'
    assert f'`{column}`' in schema, (
        f'{column} has no column in {SCHEMA.name}. The sink rejects the whole '
        f'batch on an unrecognised column, so this loses telemetry for every '
        f'action batched with it, not only for {feature}.'
    )


@pytest.mark.parametrize('feature', sorted(effect_feature_names()))
def test_effect_feature_is_passed_through_by_the_sink(feature: str) -> None:
    sink = SINK.read_text()
    column = f'__{feature}'
    passthrough = re.search(
        r'_PASSTHROUGH_INTERNAL_KEYS\s*=\s*frozenset\(\s*\{(.*?)\}', sink, re.DOTALL
    )
    assert passthrough, f'Could not find _PASSTHROUGH_INTERNAL_KEYS in {SINK.name}'
    assert f"'{column}'" in passthrough.group(1), (
        f'{column} is not in _PASSTHROUGH_INTERNAL_KEYS, so the sink silently '
        f'discards it. A column alone is not enough: the enforcement happens, '
        f'the column stays empty, and there is no record Osprey ever acted.'
    )


# --- rule names ------------------------------------------------------------
# Rule hits are columns too, and they are emitted on EVERY action evaluation
# rather than only when the rule matches: a Rule always returns a value, and
# False is a value. So a rule without a column does not lose one batch
# occasionally, it fails every insert for as long as the branch is deployed.
# That is how `ConfirmedNudityHashOnlyNullTarget` silently emptied
# osprey_events while the effect-column check above passed.

RULE_DEF_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*Rule\(', re.MULTILINE)


def rule_names() -> set[str]:
    names: set[str] = set()
    for path in sorted(RULES_DIR.rglob('*.sml')):
        names |= set(RULE_DEF_RE.findall(path.read_text()))
    return names


def test_there_is_at_least_one_rule_to_check() -> None:
    """Guard the guard, for the same reason as above."""
    assert rule_names(), (
        f'No rules discovered under {RULES_DIR}. Either the tree moved or '
        f'RULE_DEF_RE no longer matches how rules are declared.'
    )


@pytest.mark.parametrize('rule', sorted(rule_names()))
def test_rule_has_a_clickhouse_column(rule: str) -> None:
    schema = SCHEMA.read_text()
    assert f'`{rule}`' in schema, (
        f'Rule {rule} has no column in {SCHEMA.name}. Rule hits are emitted on '
        f'every action, so this fails EVERY insert, not an occasional batch: '
        f'osprey_events stops recording anything at all.'
    )
