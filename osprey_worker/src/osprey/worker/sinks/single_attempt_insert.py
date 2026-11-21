"""Prevent the pinned HTTP driver's connection-reset path replaying an insert."""

from dataclasses import dataclass
from importlib.metadata import version as package_version
from typing import Any
from urllib.parse import parse_qs, urlsplit

from clickhouse_connect.driver.httpclient import HttpClient
from urllib3.util import Retry, Timeout


class TransportReplayRefused(RuntimeError):
    """The outcome needs reconciliation; this exception never contains a payload."""


@dataclass(frozen=True)
class InsertReceipt:
    query_id: str
    outcome: str


class SingleAttemptPool:
    """One persistent proxy per maintenance sink's private HTTP client.

    Read-only schema discovery still uses the client's usual request handling.
    A Native POST requires an armed query ID and may reach the underlying pool
    once. A failed or unknown attempt cannot be rearmed, even with another ID,
    until its owner explicitly confirms the previous operation.
    """

    def __init__(self, pool: Any):
        self._pool = pool
        self._query_id: str | None = None
        self._confirmed = False
        self.dispatched = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)

    def arm(self, query_id: str) -> None:
        if not isinstance(query_id, str) or not 0 < len(query_id) <= 128:
            raise ValueError('invalid insert query identifier')
        if self.dispatched and (not self._confirmed or self._query_id == query_id):
            raise TransportReplayRefused('previous insert cannot be replayed')
        self._query_id = query_id
        self._confirmed = False
        self.dispatched = False

    def confirm(self) -> None:
        if not self.dispatched:
            raise ValueError('no dispatched insert to confirm')
        self._confirmed = True

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        headers = kwargs.get('headers', {})
        content_type = next((value for name, value in headers.items() if name.lower() == 'content-type'), '')
        native = method.upper() == 'POST' and content_type.split(';')[0].lower() == 'application/octet-stream'
        kwargs['timeout'] = Timeout(connect=10, read=60)
        if native:
            query_ids = parse_qs(urlsplit(url).query).get('query_id', [])
            if self._query_id is None or query_ids != [self._query_id] or self.dispatched:
                raise TransportReplayRefused('unarmed, mismatched or repeated native insert')
            # urllib3's retries and the driver's unconditional first reset retry
            # are separate. Disable the former; this guard refuses the latter.
            kwargs['retries'] = Retry(total=0, connect=0, read=0, redirect=0, status=0)
            kwargs['redirect'] = False
            self.dispatched = True
        return self._pool.request(method, url, **kwargs)


class NativeInsertOnce:
    """Return an explicit outcome without releasing the caller's frozen rows."""

    def __init__(self, client: Any):
        if package_version('clickhouse-connect') != '0.13.0':
            raise RuntimeError('reviewed ClickHouse driver required for maintenance inserts')
        if not isinstance(client, HttpClient):
            raise TypeError('maintenance inserts require the reviewed HTTP client')
        # The SDK annotates this attribute as a concrete PoolManager. The
        # reviewed adapter forwards its full surface through __getattr__.
        self._client: Any = client
        self._guard = client.http if isinstance(client.http, SingleAttemptPool) else SingleAttemptPool(client.http)
        self._client.http = self._guard

    def send(
        self, table: str, columns: tuple[str, ...], rows: tuple[tuple[Any, ...], ...], query_id: str
    ) -> InsertReceipt:
        armed = False
        try:
            self._guard.arm(query_id)
            armed = True
            # Schema discovery has its own generated query ID. Only the actual
            # Native insert receives this retained batch's correlation ID.
            context = self._client.create_insert_context(table, column_names=columns, data=rows, column_oriented=False)
            context.settings = {
                **(context.settings or {}),
                'query_id': query_id,
                'async_insert': 0,
                'wait_for_async_insert': 1,
                'wait_end_of_query': 1,
            }
            self._client.data_insert(context)
            self._guard.confirm()
            return InsertReceipt(query_id, 'confirmed')
        except BaseException:
            # A server exception can follow a partial base/MV commit. Only a
            # failure before Native dispatch is known not to have inserted.
            return InsertReceipt(query_id, 'unknown' if armed and self._guard.dispatched else 'not_sent')
