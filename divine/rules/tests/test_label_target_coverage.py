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

    Whole-line comments are dropped because a condition mentioned in prose
    (`# ... see IsValidMediaHash`) must not read as the condition itself.

    INLINE comments are stripped rather than dropped. Leaving them attached made
    the marker matching below an exact-string comparison against a line that a
    trailing `  # why` silently changed, so annotating a rule removed its family
    from the check while every test still passed.
    """
    out = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        line = line.split('#', 1)[0].strip().rstrip(',').strip()
        if line:
            out.append(line)
    return out


def _has(conds: list[str], *fragments: str) -> bool:
    """True when some condition contains every fragment.

    Fragment matching, not equality: spacing and inline annotation must not
    decide whether a rule is seen. Equality is what let the marker drift.
    """
    return any(all(f in c for f in fragments) for c in conds)


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
            (value, rejected), {'requires': set(), 'null': set(), 'empty': set(), 'agnostic': set()}
        )
        touches_target = _has(conds, 'LabelTargetEvent')
        if _has(conds, 'LabelTargetEvent', '!= None'):
            shapes['requires'].add(name)
        if _has(conds, 'LabelTargetEvent', '== None'):
            shapes['null'].add(name)
        if _has(conds, 'LabelTargetEvent', "== ''"):
            shapes['empty'].add(name)
        if not touches_target:
            # Constrains no target shape, so it matches all three and the family
            # cannot fall through on any of them.
            shapes['agnostic'].add(name)
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
    if shapes['agnostic']:
        pytest.skip(
            f'family is target-agnostic via {sorted(shapes["agnostic"])}, so every '
            f'target shape already matches something'
        )
    # Deliberately NOT skipped when `requires` is empty. Skipping there made the
    # check disappear exactly when the marker stopped matching, which is the
    # failure this file exists to catch: a reviewer demonstrated it by annotating
    # a rule's `LabelTargetEvent != None,` line and watching two families go quiet.
    # A family that constrains the target at all must account for all three shapes.
    assert shapes['requires'] or shapes['null'] or shapes['empty'], (
        f'Family `{family[0]}` / `{family[1]}` was discovered but no rule in it '
        f'matched any target-shape marker. Either the spelling in the .sml changed '
        f'or the matching here did, and in both cases this family is now unchecked.'
    )

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
