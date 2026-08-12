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
SINK = REPO_ROOT / 'osprey_worker' / 'src' / 'osprey' / 'worker' / 'sinks' / 'sink' / 'clickhouse_output_sink.py'

# Splits the two halves of the schema. The CREATE list governs a database built
# fresh from this file; everything after governs upgrading one that already
# exists, which is every deployed environment. They are separate obligations, and
# a name present in only one of them is a live defect, so the checks below must
# not be allowed to satisfy each other.
_UPGRADE_MARKER = '-- Upgrade DDL'


def schema_halves() -> tuple[str, str]:
    """CREATE half and upgrade half, or fail if the split is not real.

    `str.split(marker)[0]` on a missing marker returns the whole file, and the
    CREATE checks below would then pass on ALTER text. That is the one-sided
    coupling this file exists to close, so the marker and both halves are
    asserted here rather than assumed.
    """
    schema = SCHEMA.read_text()
    assert _UPGRADE_MARKER in schema, (
        f'{SCHEMA.name} has no {_UPGRADE_MARKER!r} marker, so the CREATE checks '
        f'cannot tell the two halves apart and would pass on ALTER text.'
    )
    create_half, upgrade_half = schema.split(_UPGRADE_MARKER, 1)
    assert 'CREATE TABLE' in create_half, (
        f'The text before {_UPGRADE_MARKER!r} in {SCHEMA.name} is not the CREATE '
        f'list. The split no longer isolates the obligation it is meant to.'
    )
    assert 'ALTER TABLE' in upgrade_half, (
        f'The text after {_UPGRADE_MARKER!r} in {SCHEMA.name} has no ALTER. The '
        f'split no longer isolates the upgrade obligation.'
    )
    assert 'ALTER TABLE' not in create_half, (
        f'The CREATE half of {SCHEMA.name} contains ALTER TABLE, so a name that '
        f'exists only as an upgrade would satisfy the CREATE checks.'
    )
    return create_half, upgrade_half


# CustomExtractedFeature subclasses declare their wire name in a feature_name
# classmethod. That name is prefixed with '__' by the execution context.
FEATURE_NAME_RE = re.compile(r"def feature_name\(cls\)[^:]*:\s*\n\s*return '([^']+)'", re.MULTILINE)


def effect_feature_names() -> set[str]:
    names: set[str] = set()
    for path in sorted(UDF_DIR.glob('*.py')):
        names |= set(FEATURE_NAME_RE.findall(path.read_text()))
    return names


def test_schema_halves_are_distinct() -> None:
    """Guard the split the CREATE checks depend on."""
    create_half, upgrade_half = schema_halves()
    assert create_half.strip()
    assert upgrade_half.strip()


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
    # The CREATE half only, for the same reason the rule check splits: a name
    # present solely in the ALTER path would otherwise satisfy this, and a database
    # built fresh from this file would reject the first insert carrying it.
    create_half, _ = schema_halves()
    column = f'__{feature}'
    assert f'`{column}`' in create_half, (
        f'{column} has no column in {SCHEMA.name}. The sink rejects the whole '
        f'batch on an unrecognised column, so this loses telemetry for every '
        f'action batched with it, not only for {feature}.'
    )


@pytest.mark.parametrize('feature', sorted(effect_feature_names()))
def test_effect_feature_has_an_alter_upgrade_column(feature: str) -> None:
    """CREATE alone is not enough: existing deployments never re-run CREATE.

    `CREATE TABLE IF NOT EXISTS` is a no-op when osprey_events already exists,
    which is every non-CI environment. A column that only appears in the CREATE
    list therefore never lands on an upgraded table. The first insert that
    carries the effect then fails the whole batch. Assert the ALTER half too.
    """
    schema = SCHEMA.read_text()
    column = f'__{feature}'
    alter = re.compile(
        rf'ALTER\s+TABLE\s+osprey\.osprey_events\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+`{re.escape(column)}`',
        re.IGNORECASE,
    )
    assert alter.search(schema), (
        f'{column} has no ALTER ADD COLUMN IF NOT EXISTS in {SCHEMA.name}. '
        f'CREATE TABLE IF NOT EXISTS does not upgrade existing tables, so this '
        f'column is missing everywhere the schema was applied before the effect '
        f'shipped, and the first {feature} batch empties osprey_events.'
    )


@pytest.mark.parametrize('feature', sorted(effect_feature_names()))
def test_effect_feature_is_passed_through_by_the_sink(feature: str) -> None:
    sink = SINK.read_text()
    column = f'__{feature}'
    passthrough = re.search(r'_PASSTHROUGH_INTERNAL_KEYS\s*=\s*frozenset\(\s*\{(.*?)\}', sink, re.DOTALL)
    assert passthrough, f'Could not find _PASSTHROUGH_INTERNAL_KEYS in {SINK.name}'
    assert f"'{column}'" in passthrough.group(1), (
        f'{column} is not in _PASSTHROUGH_INTERNAL_KEYS, so the sink silently '
        f'discards it. A column alone is not enough: the enforcement happens, '
        f'the column stays empty, and there is no record Osprey ever acted.'
    )


# --- rule names ------------------------------------------------------------
# Rule hits are columns too, and a rule that does not match still emits `false`
# rather than nothing, so a column is needed whether or not the rule ever fires.
#
# An earlier version of this comment said a Rule "always returns a value, and
# False is a value", and therefore that a missing column fails EVERY insert. That
# overstates it, and a reviewer reasoned from it to a wrong conclusion, so it is
# corrected here rather than left to mislead again. What actually decides it is
# the syntactic form of the conditions: tolerant forms (`==`, `!=`, `in`,
# Optional-argument UDFs) resolve a missing feature to None and yield `false`,
# which needs a column; non-tolerant forms (bare `X`, `not X`, numeric
# comparisons) propagate the node failure and yield `null`, which the sink skips
# and which needs nothing.
#
# The practical consequence is unchanged, which is why the check is unchanged:
# a rule emitting `false` on ANY action type needs a column unconditionally, and
# `_flush` unions columns across the whole buffer into one insert, so a single
# such action destroys every unrelated row batched with it. Provision a column
# for every rule name and the question never has to be asked.
#
# That is how `ConfirmedNudityHashOnlyNullTarget` silently emptied osprey_events
# while the effect-column check above passed.

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
    """The CREATE list, so a database built from this file is born correct."""
    create_half, _ = schema_halves()
    assert f'`{rule}`' in create_half, (
        f'Rule {rule} has no column in the CREATE TABLE list of {SCHEMA.name}. A '
        f'database created fresh from this file would reject every insert carrying '
        f'the rule, which is every action once it is deployed.'
    )


@pytest.mark.parametrize('rule', sorted(rule_names()))
def test_rule_has_an_alter_upgrade_column(rule: str) -> None:
    """And the ALTER half, which is the one every deployed environment runs.

    Rules got only a substring check against the whole file, so a name present in
    EITHER half passed. That is the same one-sided coupling the effect features
    already guard against above, and it is not hypothetical: on 2026-08-12 staging
    was found 19 columns behind and discarding batches of 100 rows, because the
    deployed table is upgraded by ALTER and never by CREATE.
    """
    schema = SCHEMA.read_text()
    alter = re.compile(
        rf'ALTER\s+TABLE\s+osprey\.osprey_events\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+`{re.escape(rule)}`',
        re.IGNORECASE,
    )
    assert alter.search(schema), (
        f'Rule {rule} has no ALTER ADD COLUMN IF NOT EXISTS in {SCHEMA.name}. '
        f'CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so this '
        f'column never lands anywhere the schema was applied before the rule '
        f'shipped, and osprey_events stops recording there.'
    )
