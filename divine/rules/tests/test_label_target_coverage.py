"""Guards that a confirmed or rejected label has somewhere to go when it carries
no event target.

The publishing side sets the imeta `x` (hash) tag unconditionally and the `e`
(event) tag only when it has an event id -- see
divine-moderation-service/src/nostr/publisher.mjs::createLabelEvent. So
hash-present/target-absent is the COMMON shape of a label, not an edge case, and
the admin category-verification path that a human moderator actually uses passes
no event id at all.

A rule whose `when_all` requires `LabelTargetEvent != None` therefore does not
match the ordinary case. If no sibling rule covers the null-target and
empty-target shapes, that label matches NOTHING: no verdict, no label write, no
COOP item, no telemetry row. It is not rejected and it is not acted on, it simply
evaporates. That is the failure this file exists to prevent, and it has already
happened twice -- the nudity and violence families shipped target-only before
gaining their hash-only variants.

Note the two spellings are genuinely distinct and both are needed. `!= None` does
not exclude `''`, and `== None` does not catch `''`, so a family needs an explicit
rule for each or one of the two shapes still falls through.

This checks that a shape is COVERED, not that the coverage is correct. A rule that
matches the shape and declares the wrong verdict passes here; that judgment belongs
in review, not in a parser.

Parsed from the live .sml file rather than a maintained list, so a new label family
that ships target-only fails here instead of evaporating in production.

Pure stdlib: no osprey engine, no plugins, no network.
Run: `python3 -m pytest divine/rules/tests/`
"""

import re
from pathlib import Path

import pytest

_LABEL_ROUTING = (
    Path(__file__).resolve().parent.parent
    / 'rules'
    / 'content'
    / 'label_routing.sml'
)

# `Name = Rule(when_all=[ ... ], description=...)`. Anchored on `description` so a
# rule whose body is reformatted still matches, and non-greedy so adjacent rules
# do not merge into one block.
_RULE_BLOCK = re.compile(
    r'^([A-Za-z_]\w*)\s*=\s*Rule\(\s*when_all=\[(.*?)\],\s*\n\s*description',
    re.S | re.M,
)

_REQUIRES_TARGET = 'LabelTargetEvent != None'
_NULL_TARGET = 'LabelTargetEvent == None'
_EMPTY_TARGET = "LabelTargetEvent == ''"


def _conditions(body: str) -> list[str]:
    """Condition lines, comments and blanks dropped.

    Comments are dropped because a condition mentioned in prose (`# ... see
    IsValidMediaHash`) must not read as the condition itself.
    """
    out = []
    for line in body.splitlines():
        line = line.strip().rstrip(',')
        if not line or line.startswith('#'):
            continue
        out.append(line)
    return out


def _families() -> dict[tuple[str, str], dict[str, set[str]]]:
    """Group rules by what content they select, ignoring target shape.

    The key is (label-value predicate, rejected predicate). Everything sharing a
    key is one family: the same content arriving in different shapes. The hash
    predicate is deliberately NOT part of the key -- the CSAM family's
    target-required rule constrains the hash differently from its hash-only
    variants, and they are still one family.
    """
    text = _LABEL_ROUTING.read_text()
    families: dict[tuple[str, str], dict[str, set[str]]] = {}
    for name, body in _RULE_BLOCK.findall(text):
        conds = _conditions(body)
        value = next((c for c in conds if 'LabelValue' in c), '<any value>')
        rejected = next((c for c in conds if 'LabelRejected' in c), '<any rejected>')
        shapes = families.setdefault(
            (value, rejected), {'requires': set(), 'null': set(), 'empty': set()}
        )
        if any(c == _REQUIRES_TARGET for c in conds):
            shapes['requires'].add(name)
        if any(c == _NULL_TARGET for c in conds):
            shapes['null'].add(name)
        if any(c == _EMPTY_TARGET for c in conds):
            shapes['empty'].add(name)
    return families


def test_there_is_at_least_one_family_to_check() -> None:
    """Guard the guard.

    Every assertion below is parametrised over discovered families. If
    `_RULE_BLOCK` stops matching -- a formatting change, a renamed keyword -- the
    parametrisation empties and the whole file passes while checking nothing,
    which is the exact class of failure it exists to catch.
    """
    families = _families()
    assert families, (
        f'No rules discovered in {_LABEL_ROUTING.name}. Either the file moved or '
        f'_RULE_BLOCK no longer matches how rules are declared.'
    )
    assert any(s['requires'] for s in families.values()), (
        'No target-requiring rule discovered, so the coverage check below is '
        'vacuous. _REQUIRES_TARGET no longer matches the live spelling.'
    )


@pytest.mark.parametrize('shape', ['null', 'empty'])
@pytest.mark.parametrize('family', sorted(_families()))
def test_target_requiring_family_also_covers_targetless_labels(
    family: tuple[str, str], shape: str
) -> None:
    shapes = _families()[family]
    if not shapes['requires']:
        pytest.skip('family has no target-requiring rule, nothing to fall through')

    spelling = {'null': _NULL_TARGET, 'empty': _EMPTY_TARGET}[shape]
    value, rejected = family
    assert shapes[shape], (
        f'Labels matching `{value}` / `{rejected}` are handled by '
        f'{sorted(shapes["requires"])} only when an event target is present, and '
        f'no rule covers `{spelling}`. The publisher emits the hash '
        f'unconditionally and the event tag only when it has one, so a label of '
        f'this shape matches no rule at all: no verdict, no COOP item, no '
        f'telemetry. Add a companion rule covering `{spelling}`.'
    )
