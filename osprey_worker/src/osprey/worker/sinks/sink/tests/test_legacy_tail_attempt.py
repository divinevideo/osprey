import concurrent.futures
import hashlib
import json
import os
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from osprey.worker.sinks import legacy_tail_attempt as owner
from osprey.worker.sinks.legacy_partial_tail import PreparedLegacyTail

SCHEMA = 'a' * 64
CONTAINER = 'b' * 64


def prepared(value: object = 'synthetic-private-value') -> PreparedLegacyTail:
    return PreparedLegacyTail(
        columns=('__time', '__action_id', 'FixtureValue'),
        rows=(('2026-01-01T00:00:00.123000+00:00', 7, value),),
        sources=(('synthetic-input', 0, 12),),
        serialized_size=128,
    )


def manifest(directory: Path, value: object = 'synthetic-private-value', container: str = CONTAINER) -> tuple[str, str]:
    assert callable(getattr(owner, 'encode_legacy_tail_manifest', None))
    body = owner.encode_legacy_tail_manifest(prepared(value), legacy_container_id=container, schema_sha256=SCHEMA)
    path = directory / 'reviewed.json'
    path.write_bytes(body)
    path.chmod(0o600)
    return path.name, hashlib.sha256(body).hexdigest()


def submit(directory: Path, name: str, digest: str, schema: str = SCHEMA) -> Any:
    return owner.submit_legacy_tail_manifest(
        None,
        directory=directory,
        manifest_name=name,
        expected_manifest_sha256=digest,
        expected_schema_sha256=schema,
    )


def records(directory: Path) -> list[Path]:
    return list(directory.glob('attempt-*.json'))


def test_reservation_is_durable_before_native_send_and_confirmation_is_retained(
    tmp_path: Path, monkeypatch: Any
) -> None:
    tmp_path.chmod(0o700)
    name, digest = manifest(tmp_path)
    syncs: list[str] = []
    fsync = os.fsync

    def sync(fd: int) -> None:
        syncs.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
        fsync(fd)

    class Sender:
        def send(self, table: str, columns: Any, rows: Any, query_id: str) -> Any:
            assert table == 'osprey.osprey_events'
            assert columns == prepared().columns and rows == prepared().rows
            assert syncs[:2] == ['file', 'directory']
            (path,) = records(tmp_path)
            saved = json.loads(path.read_text())
            assert saved['outcome'] == 'reserved' and saved['query_id'] == query_id
            assert saved['manifest_json'] == (tmp_path / name).read_text()
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            return SimpleNamespace(outcome='confirmed', query_id=query_id)

    monkeypatch.setattr(os, 'fsync', sync)
    monkeypatch.setattr(owner, 'NativeInsertOnce', lambda client: Sender(), raising=False)
    result = submit(tmp_path, name, digest)
    assert result.outcome == 'confirmed' and result.pending is None and result.receipt_persisted
    assert json.loads(records(tmp_path)[0].read_text())['outcome'] == 'confirmed'
    assert 'synthetic-private-value' not in repr(result)


@pytest.mark.parametrize('outcome', ['confirmed', 'unknown', 'not_sent'])
def test_every_previous_attempt_blocks_replay_even_with_a_changed_manifest(
    tmp_path: Path, monkeypatch: Any, outcome: str
) -> None:
    tmp_path.chmod(0o700)
    name, digest = manifest(tmp_path)
    calls: list[int] = []

    class Sender:
        def send(self, table: str, columns: Any, rows: Any, query_id: str) -> Any:
            calls.append(1)
            return SimpleNamespace(outcome=outcome, query_id=query_id)

    monkeypatch.setattr(owner, 'NativeInsertOnce', lambda client: Sender(), raising=False)
    result = submit(tmp_path, name, digest)
    assert (result.pending is None) == (outcome == 'confirmed')
    if result.pending is not None:
        assert result.pending.columns == prepared().columns and result.pending.rows == prepared().rows
    name, new_digest = manifest(tmp_path, 'changed-synthetic-value')
    with pytest.raises(owner.LegacyTailAttemptBlocked):
        submit(tmp_path, name, new_digest)
    assert len(calls) == 1


def test_pre_transport_failure_is_not_sent_and_still_cannot_rearm(tmp_path: Path, monkeypatch: Any) -> None:
    tmp_path.chmod(0o700)
    name, digest = manifest(tmp_path)

    def fail(client: Any) -> Any:
        raise ValueError('synthetic-private-value')

    monkeypatch.setattr(owner, 'NativeInsertOnce', fail, raising=False)
    result = submit(tmp_path, name, digest)
    assert result.outcome == 'not_sent' and result.pending is not None
    with pytest.raises(owner.LegacyTailAttemptBlocked):
        submit(tmp_path, name, digest)


def test_receipt_update_failure_retains_batch_and_reservation_after_success(tmp_path: Path, monkeypatch: Any) -> None:
    tmp_path.chmod(0o700)
    name, digest = manifest(tmp_path)

    class Sender:
        def send(self, table: str, columns: Any, rows: Any, query_id: str) -> Any:
            return SimpleNamespace(outcome='confirmed', query_id=query_id)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError('synthetic filesystem failure')

    monkeypatch.setattr(owner, 'NativeInsertOnce', lambda client: Sender(), raising=False)
    monkeypatch.setattr(os, 'replace', fail)
    result = submit(tmp_path, name, digest)
    assert result.outcome == 'unknown' and result.pending is not None and not result.receipt_persisted
    assert json.loads(records(tmp_path)[0].read_text())['outcome'] == 'reserved'
    with pytest.raises(owner.LegacyTailAttemptBlocked):
        submit(tmp_path, name, digest)


def test_reservation_directory_sync_failure_sends_nothing(tmp_path: Path, monkeypatch: Any) -> None:
    tmp_path.chmod(0o700)
    name, digest = manifest(tmp_path)
    calls: list[int] = []
    fsync = os.fsync

    def sync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError('synthetic directory sync failure')
        fsync(fd)

    monkeypatch.setattr(os, 'fsync', sync)
    monkeypatch.setattr(owner, 'NativeInsertOnce', lambda client: calls.append(1), raising=False)
    with pytest.raises(owner.LegacyTailAttemptBlocked):
        submit(tmp_path, name, digest)
    assert calls == [] and len(records(tmp_path)) == 1


@pytest.mark.parametrize('fault', ['manifest_hash', 'schema_hash', 'public_directory', 'public_manifest', 'symlink'])
def test_invalid_preconditions_never_reserve_or_construct_transport(
    tmp_path: Path, monkeypatch: Any, fault: str
) -> None:
    tmp_path.chmod(0o700)
    name, digest = manifest(tmp_path)
    schema = SCHEMA
    if fault == 'manifest_hash':
        digest = '0' * 64
    elif fault == 'schema_hash':
        schema = '0' * 64
    elif fault == 'public_directory':
        tmp_path.chmod(0o755)
    elif fault == 'public_manifest':
        (tmp_path / name).chmod(0o644)
    else:
        link = tmp_path / 'link.json'
        link.symlink_to(name)
        name = link.name
    calls: list[int] = []
    monkeypatch.setattr(owner, 'NativeInsertOnce', lambda client: calls.append(1), raising=False)
    with pytest.raises(owner.LegacyTailAttemptBlocked) as caught:
        submit(tmp_path, name, digest, schema)
    assert 'synthetic-private-value' not in str(caught.value)
    assert calls == [] and records(tmp_path) == []


def test_competing_owners_only_one_gets_to_send(tmp_path: Path, monkeypatch: Any) -> None:
    tmp_path.chmod(0o700)
    name, digest = manifest(tmp_path)
    calls: list[int] = []

    class Sender:
        def send(self, table: str, columns: Any, rows: Any, query_id: str) -> Any:
            calls.append(1)
            time.sleep(0.03)
            return SimpleNamespace(outcome='confirmed', query_id=query_id)

    monkeypatch.setattr(owner, 'NativeInsertOnce', lambda client: Sender(), raising=False)

    def contender() -> str:
        try:
            return submit(tmp_path, name, digest).outcome
        except owner.LegacyTailAttemptBlocked:
            return 'blocked'

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: contender(), range(2)))
    assert sorted(outcomes) == ['blocked', 'confirmed'] and len(calls) == 1


@pytest.mark.parametrize('phase', ['before_send', 'simulated_commit'])
def test_process_loss_leaves_a_reservation_that_blocks_new_process(
    tmp_path: Path, monkeypatch: Any, phase: str
) -> None:
    tmp_path.chmod(0o700)
    name, digest = manifest(tmp_path)
    script = """import os,sys
from pathlib import Path
from osprey.worker.sinks import legacy_tail_attempt as module
class Sender:
 def send(self,*args):
  fd=os.open(Path(sys.argv[1])/'synthetic-commits',os.O_CREAT|os.O_APPEND|os.O_WRONLY,0o600)
  os.write(fd,b'commit\\n');os.fsync(fd);os.close(fd);os._exit(91)
def factory(client):
 if sys.argv[5]=='before_send':os._exit(92)
 return Sender()
module.NativeInsertOnce=factory
module.submit_legacy_tail_manifest(None,directory=Path(sys.argv[1]),manifest_name=sys.argv[2],expected_manifest_sha256=sys.argv[3],expected_schema_sha256=sys.argv[4])
"""
    process = subprocess.run(
        [sys.executable, '-c', script, str(tmp_path), name, digest, SCHEMA, phase], capture_output=True, timeout=20
    )
    assert process.returncode == (92 if phase == 'before_send' else 91)
    record = json.loads(records(tmp_path)[0].read_text())
    assert record['outcome'] == 'reserved'
    assert hashlib.sha256(record['manifest_json'].encode()).hexdigest() == digest
    if phase == 'simulated_commit':
        assert (tmp_path / 'synthetic-commits').read_bytes() == b'commit\n'
    else:
        assert not (tmp_path / 'synthetic-commits').exists()
    calls: list[int] = []
    monkeypatch.setattr(owner, 'NativeInsertOnce', lambda client: calls.append(1), raising=False)
    with pytest.raises(owner.LegacyTailAttemptBlocked):
        submit(tmp_path, name, digest)
    assert calls == []


@pytest.mark.parametrize('value', [None, True, 2**64 - 1, -0.0, float('inf'), float('nan'), 'λ🎥'])
def test_manifest_codec_preserves_scalar_types_and_float_bits(tmp_path: Path, monkeypatch: Any, value: Any) -> None:
    tmp_path.chmod(0o700)
    name, digest = manifest(tmp_path, value)

    class Sender:
        def send(self, table: str, columns: Any, rows: Any, query_id: str) -> Any:
            actual = rows[0][2]
            assert type(actual) is type(value)
            if isinstance(value, float):
                assert struct.pack('>d', actual) == struct.pack('>d', value)
            else:
                assert actual == value
            return SimpleNamespace(outcome='unknown', query_id=query_id)

    monkeypatch.setattr(owner, 'NativeInsertOnce', lambda client: Sender(), raising=False)
    result = submit(tmp_path, name, digest)
    assert result.outcome == 'unknown' and result.pending is not None


@pytest.mark.parametrize(
    'field,value', [('__time', None), ('__time', '2026-01-01'), ('__action_id', True), ('__action_id', -1)]
)
def test_manifest_cannot_replace_legacy_base_identity_with_defaults(field: str, value: Any) -> None:
    data = prepared()
    row = list(data.rows[0])
    row[data.columns.index(field)] = value
    invalid = PreparedLegacyTail(data.columns, (tuple(row),), data.sources, data.serialized_size)
    with pytest.raises(owner.LegacyTailAttemptBlocked):
        owner.encode_legacy_tail_manifest(invalid, legacy_container_id=CONTAINER, schema_sha256=SCHEMA)


def test_manifest_requires_both_base_columns() -> None:
    invalid = PreparedLegacyTail(('FixtureValue',), (('synthetic',),), (('synthetic-input', 0, 12),), 20)
    with pytest.raises(owner.LegacyTailAttemptBlocked):
        owner.encode_legacy_tail_manifest(invalid, legacy_container_id=CONTAINER, schema_sha256=SCHEMA)


@pytest.mark.parametrize('fault', ['duplicate_member', 'float_tag', 'constant', 'source_duplicate', 'unknown_member'])
def test_malformed_reviewed_bytes_refuse_before_reservation(tmp_path: Path, monkeypatch: Any, fault: str) -> None:
    tmp_path.chmod(0o700)
    name, _ = manifest(tmp_path)
    path = tmp_path / name
    data = json.loads(path.read_bytes())
    if fault == 'duplicate_member':
        raw = path.read_bytes().replace(b'{', b'{"format":"duplicate",', 1)
    elif fault == 'constant':
        raw = path.read_bytes().replace(b'"synthetic-private-value"', b'NaN')
    else:
        if fault == 'float_tag':
            data['rows'][0][2] = {'f64': 'broken'}
        elif fault == 'source_duplicate':
            data['rows'] *= 2
            data['sources'] *= 2
        else:
            data['unexpected'] = True
        raw = json.dumps(data).encode()
    path.write_bytes(raw)
    calls: list[int] = []
    monkeypatch.setattr(owner, 'NativeInsertOnce', lambda client: calls.append(1), raising=False)
    with pytest.raises(owner.LegacyTailAttemptBlocked):
        submit(tmp_path, name, hashlib.sha256(raw).hexdigest())
    assert not records(tmp_path) and not calls


def test_hardlinked_manifest_is_not_accepted(tmp_path: Path, monkeypatch: Any) -> None:
    tmp_path.chmod(0o700)
    name, digest = manifest(tmp_path)
    (tmp_path / 'second-link.json').hardlink_to(tmp_path / name)
    calls: list[int] = []
    monkeypatch.setattr(owner, 'NativeInsertOnce', lambda client: calls.append(1), raising=False)
    with pytest.raises(owner.LegacyTailAttemptBlocked):
        submit(tmp_path, name, digest)
    assert not records(tmp_path) and not calls


def test_transport_confirmation_must_match_the_reserved_query_id(tmp_path: Path, monkeypatch: Any) -> None:
    tmp_path.chmod(0o700)
    name, digest = manifest(tmp_path)

    class Sender:
        def send(self, *args: Any) -> Any:
            return SimpleNamespace(outcome='confirmed', query_id='different-synthetic-operation')

    monkeypatch.setattr(owner, 'NativeInsertOnce', lambda client: Sender(), raising=False)
    result = submit(tmp_path, name, digest)
    assert result.outcome == 'unknown' and result.pending is not None
