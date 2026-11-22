"""One-time legacy-tail operation ownership; not a worker runtime component.

These receipts never certify admission, source coverage, schema or authority.
The operator must establish those gates before invoking submission. There is no
client construction, CLI, worker wiring, replay or automatic reconciliation.
"""

import hashlib
import json
import os
import re
import stat
import struct
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from osprey.worker.sinks.legacy_partial_tail import (
    LEGACY_BATCH_SIZE,
    MAX_PREPARATION_BYTES,
    PreparedLegacyTail,
    prepared_payload_size,
)
from osprey.worker.sinks.single_attempt_insert import NativeInsertOnce

MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_HEX = re.compile(r'[0-9a-f]{64}')
_FORMAT = 'osprey-legacy-tail-v1'
# TODO(divinevideo/divine-iac-coreconfig#2615): retire after the verified legacy handoff.


class LegacyTailAttemptBlocked(RuntimeError):
    """A private operation requires inspection; never include its payload here."""


@dataclass(frozen=True)
class LegacyTailAttemptResult:
    query_id: str
    outcome: Literal['confirmed', 'unknown', 'not_sent']
    receipt_persisted: bool
    pending: PreparedLegacyTail | None = field(repr=False)


def _hash_identifier(value: Any) -> bool:
    return type(value) is str and _HEX.fullmatch(value) is not None


def _scalar(value: Any) -> Any:
    if type(value) is float:
        return {'f64': struct.pack('>d', value).hex()}
    if value is None or type(value) in {str, int, bool}:
        return value
    raise LegacyTailAttemptBlocked('unsupported manifest value')


def _unscalar(value: Any) -> Any:
    if type(value) is dict and set(value) == {'f64'}:
        bits = value['f64']
        if type(bits) is str and re.fullmatch(r'[0-9a-f]{16}', bits):
            return struct.unpack('>d', bytes.fromhex(bits))[0]
    elif value is None or type(value) in {str, int, bool}:
        return value
    raise LegacyTailAttemptBlocked('unsupported manifest value')


def _payload(prepared: PreparedLegacyTail) -> tuple[dict[str, Any], int]:
    if type(prepared.columns) is not tuple or not prepared.columns or len(prepared.columns) > 256:
        raise LegacyTailAttemptBlocked('invalid manifest columns')
    if not all(type(name) is str and name for name in prepared.columns) or len(set(prepared.columns)) != len(
        prepared.columns
    ):
        raise LegacyTailAttemptBlocked('invalid manifest columns')
    if not {'__time', '__action_id'} <= set(prepared.columns):
        raise LegacyTailAttemptBlocked('manifest requires legacy base columns')
    if type(prepared.rows) is not tuple or not 0 < len(prepared.rows) < LEGACY_BATCH_SIZE:
        raise LegacyTailAttemptBlocked('manifest requires one nonempty partial tail')
    if type(prepared.sources) is not tuple or len(prepared.sources) != len(prepared.rows):
        raise LegacyTailAttemptBlocked('invalid manifest source coordinates')
    previous = -1
    partition = None
    for source in prepared.sources:
        if type(source) is not tuple or len(source) != 3:
            raise LegacyTailAttemptBlocked('invalid manifest source coordinates')
        topic, part, offset = source
        if type(topic) is not str or not topic or type(part) is not int or part < 0:
            raise LegacyTailAttemptBlocked('invalid manifest source coordinates')
        if type(offset) is not int or offset <= previous:
            raise LegacyTailAttemptBlocked('invalid manifest source coordinates')
        if partition is not None and partition != (topic, part):
            raise LegacyTailAttemptBlocked('manifest requires one source partition')
        previous, partition = offset, (topic, part)
    rows = []
    for row in prepared.rows:
        if type(row) is not tuple or len(row) != len(prepared.columns):
            raise LegacyTailAttemptBlocked('invalid manifest row shape')
        timestamp = row[prepared.columns.index('__time')]
        action_id = row[prepared.columns.index('__action_id')]
        if type(timestamp) is not str or type(action_id) is not int or not 0 <= action_id < 2**64:
            raise LegacyTailAttemptBlocked('invalid legacy base identity')
        try:
            parsed = datetime.fromisoformat(timestamp)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError()
        except ValueError:
            raise LegacyTailAttemptBlocked('invalid legacy timestamp') from None
        rows.append([_scalar(value) for value in row])
    try:
        size = prepared_payload_size(prepared.columns, prepared.rows)
    except UnicodeEncodeError:
        raise LegacyTailAttemptBlocked('invalid manifest Unicode') from None
    except ValueError:
        raise LegacyTailAttemptBlocked('unsupported manifest value') from None
    if size > MAX_PREPARATION_BYTES:
        raise LegacyTailAttemptBlocked('partial tail capacity exceeded')
    return {'columns': prepared.columns, 'rows': rows, 'sources': prepared.sources}, size


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode()


def encode_legacy_tail_manifest(prepared: PreparedLegacyTail, *, legacy_container_id: str, schema_sha256: str) -> bytes:
    """Prepare reviewable bytes only; the caller writes them privately.

    Float bits, integer precision, NULL and strings round-trip without executable
    deserialization. The schema hash binds review; it is not a live schema check.
    """
    if not _hash_identifier(legacy_container_id) or not _hash_identifier(schema_sha256):
        raise LegacyTailAttemptBlocked('invalid manifest identity')
    payload, _ = _payload(prepared)
    try:
        raw = _json(
            {
                'format': _FORMAT,
                'legacy_container_id': legacy_container_id,
                'schema_sha256': schema_sha256,
                **payload,
            }
        )
    except ValueError:
        # Source offsets are not measured by _payload and can exceed the int-to-str limit.
        raise LegacyTailAttemptBlocked('unsupported manifest value') from None
    if len(raw) > MAX_MANIFEST_BYTES:
        raise LegacyTailAttemptBlocked('manifest capacity exceeded')
    return raw


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LegacyTailAttemptBlocked('duplicate manifest member')
        result[key] = value
    return result


def _invalid_constant(value: str) -> Any:
    raise LegacyTailAttemptBlocked('unsupported manifest encoding')


def _decode(raw: bytes, schema_sha256: str) -> tuple[dict[str, Any], PreparedLegacyTail]:
    data = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    if type(data) is not dict or set(data) != {
        'format',
        'legacy_container_id',
        'schema_sha256',
        'columns',
        'rows',
        'sources',
    }:
        raise LegacyTailAttemptBlocked('invalid manifest shape')
    if data['format'] != _FORMAT or not _hash_identifier(data['legacy_container_id']):
        raise LegacyTailAttemptBlocked('invalid manifest identity')
    if data['schema_sha256'] != schema_sha256:
        raise LegacyTailAttemptBlocked('reviewed schema identity changed')
    if not all(type(data[key]) is list for key in ['columns', 'rows', 'sources']):
        raise LegacyTailAttemptBlocked('invalid manifest shape')
    if not all(type(row) is list for row in data['rows'] + data['sources']):
        raise LegacyTailAttemptBlocked('invalid manifest shape')
    prepared = PreparedLegacyTail(
        columns=tuple(data['columns']),
        rows=tuple(tuple(_unscalar(v) for v in row) for row in data['rows']),
        sources=tuple(tuple(source) for source in data['sources']),
        serialized_size=0,
    )
    _, size = _payload(prepared)
    return data, replace(prepared, serialized_size=size)


def _private(info: os.stat_result, *, directory: bool) -> bool:
    kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    return (
        kind
        and info.st_uid == os.geteuid()
        and not (stat.S_IMODE(info.st_mode) & 0o077)
        and (directory or info.st_nlink == 1)
    )


def _write(fd: int, raw: bytes) -> None:
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _finish(directory: int, name: str, inode: tuple[int, int], record: dict[str, Any]) -> None:
    temporary = name + '.pending-' + uuid4().hex
    raw = _json(record)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        _write(fd, raw)
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if not _private(info, directory=False) or (info.st_dev, info.st_ino) != inode:
            raise OSError('operation receipt changed')
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def submit_legacy_tail_manifest(
    client: Any,
    *,
    directory: Path,
    manifest_name: str,
    expected_manifest_sha256: str,
    expected_schema_sha256: str,
) -> LegacyTailAttemptResult:
    """Reserve durably, then make one reviewed Native attempt for this container.

    Every existing receipt blocks, including confirmed, incomplete, corrupt and
    NOT_SENT records. Changing the manifest never creates a new attempt for the
    same legacy container. Keep this directory and receipts across restarts.

    The caller must retain the real admission fence, prove source/flush boundaries
    and privacy outcomes, verify the current schema hash, and supply an already
    verified authoritative client. This function establishes none of those gates.
    """
    directory_fd = -1
    try:
        if not directory.is_absolute() or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.json', manifest_name):
            raise LegacyTailAttemptBlocked('invalid private manifest location')
        if not _hash_identifier(expected_manifest_sha256) or not _hash_identifier(expected_schema_sha256):
            raise LegacyTailAttemptBlocked('invalid reviewed manifest identity')
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if not _private(os.fstat(directory_fd), directory=True):
            raise LegacyTailAttemptBlocked('operation directory must be private and owned')
        fd = os.open(manifest_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not _private(info, directory=False) or info.st_size > MAX_MANIFEST_BYTES:
                raise LegacyTailAttemptBlocked('manifest must be private, regular and bounded')
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES or hashlib.sha256(raw).hexdigest() != expected_manifest_sha256:
            raise LegacyTailAttemptBlocked('manifest differs from reviewed bytes')
        data, prepared = _decode(raw, expected_schema_sha256)
        name = 'attempt-' + data['legacy_container_id'] + '.json'
        query_id = 'osprey-legacy-tail-' + uuid4().hex
        record = {
            'format': 'osprey-legacy-attempt-v1',
            'outcome': 'reserved',
            'query_id': query_id,
            'manifest_sha256': expected_manifest_sha256,
            'manifest_json': raw.decode(),
        }
        record_raw = _json(record)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        info = os.fstat(fd)
        inode = info.st_dev, info.st_ino
        _write(fd, record_raw)
        os.fsync(directory_fd)
    except Exception:
        if directory_fd >= 0:
            os.close(directory_fd)
        raise LegacyTailAttemptBlocked('reviewed operation unavailable or already reserved') from None

    outcome: Literal['confirmed', 'unknown', 'not_sent'] = 'not_sent'
    entered_sender = False
    try:
        sender = NativeInsertOnce(client)
        entered_sender = True
        receipt = sender.send('osprey.osprey_events', prepared.columns, prepared.rows, query_id)
        if receipt.query_id != query_id:
            outcome = 'unknown'
        elif receipt.outcome == 'confirmed':
            outcome = 'confirmed'
        elif receipt.outcome == 'not_sent':
            outcome = 'not_sent'
        else:
            outcome = 'unknown'
    except BaseException:
        outcome = 'unknown' if entered_sender else 'not_sent'
    persisted = False
    try:
        record['outcome'] = outcome
        _finish(directory_fd, name, inode, record)
        persisted = True
    except Exception:
        outcome = 'unknown'
    finally:
        os.close(directory_fd)
    return LegacyTailAttemptResult(query_id, outcome, persisted, None if outcome == 'confirmed' else prepared)
