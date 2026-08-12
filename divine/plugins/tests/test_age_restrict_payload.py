"""Guards that the age-restrict call SENDS a normalised media hash.

`media_hash.normalize_media_hash` is proven by test_media_hash.py. That proves the
function is correct, not that the sink uses it, and the two failures are
different: a normaliser nothing calls leaves the divergent-row bug exactly where
it was while looking fixed.

The distinction matters here specifically because validation deliberately accepts
uppercase. So the natural mistake is to validate the hash and then send the
original value, which passes every test that asserts on the predicate and none
that asserts on the payload.

This is a SOURCE-LEVEL check, and that is a deliberate compromise. The sink
imports the Osprey engine (`osprey.engine.executor`, `osprey.worker.sinks`),
which the plugin-test CI step does not install -- it runs with pytest and
websocket-client only, and `divine/` is never copied into the engine's test image.
So the sink cannot be imported here to intercept a real request. What this file
can still do is refuse to let the payload be built from the raw value.

The end-to-end proof, where a real request is really sent and the stored hash is
read back, belongs in scripts/local-osprey-harness/check-chain.sh, whose first
design rule is to assert on observed downstream state rather than on the call
that was supposed to produce it.

Pure stdlib: no osprey engine, no plugins, no network.
"""

import ast
from pathlib import Path

_SINK = Path(__file__).resolve().parents[1] / 'src' / 'services' / 'relay_manager_sink.py'


def _age_restrict_fn() -> ast.FunctionDef:
    tree = ast.parse(_SINK.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == '_age_restrict_media':
            return node
    raise AssertionError(
        f'_age_restrict_media not found in {_SINK.name}. If it was renamed, this '
        f'guard is now checking nothing and must be pointed at the new name.'
    )


def test_the_function_under_guard_exists() -> None:
    """Guard the guard: every assertion below reads this node."""
    assert _age_restrict_fn().body


def test_moderate_media_payload_does_not_send_the_raw_hash() -> None:
    """The payload's sha256 must not be `effect.sha256` straight off the effect."""
    fn = _age_restrict_fn()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and key.value == 'sha256'):
                continue
            raw = (
                isinstance(value, ast.Attribute)
                and value.attr == 'sha256'
                and isinstance(value.value, ast.Name)
                and value.value.id == 'effect'
            )
            assert not raw, (
                'The moderate-media payload sends `effect.sha256` unchanged. '
                'Validation accepts uppercase deliberately, so the raw value can '
                'be mixed-case; moderation-service compares case-sensitively and '
                'would open a second row for the same media. Send the value '
                'through normalize_media_hash first.'
            )


def test_the_hash_is_normalized_before_use() -> None:
    called = {
        node.func.id
        for node in ast.walk(_age_restrict_fn())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert 'normalize_media_hash' in called, (
        'normalize_media_hash is never called in _age_restrict_media, so whatever '
        'the payload sends has not been canonicalised.'
    )


def test_the_rejection_log_cannot_crash_on_the_value_it_reports() -> None:
    """The backstop must survive the input it exists to report.

    It previously sliced `effect.sha256` directly, which raises TypeError when the
    hash is None -- the single most likely malformed value, since an absent tag is
    the common shape. The backstop then crashed instead of logging, on exactly the
    case it was written for.
    """
    src = ast.unparse(_age_restrict_fn())
    assert 'effect.sha256[' not in src, (
        'The raw effect.sha256 is being sliced. It may be None or a non-string, '
        'so this raises inside the error path. Wrap it in repr() before slicing.'
    )
