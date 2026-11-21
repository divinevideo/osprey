import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from clickhouse_connect.driver.exceptions import OperationalError
from clickhouse_connect.driver.httpclient import HttpClient
from osprey.worker.sinks.single_attempt_insert import NativeInsertOnce, SingleAttemptPool, TransportReplayRefused
from urllib3 import PoolManager
from urllib3.exceptions import ProtocolError
from urllib3.util import Retry, Timeout

QUERY_ID = 'osprey-handoff-00000000000000000000000000000001'


class ResetAfterSend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def clear(self) -> None:
        pass

    def request(self, method: str, url: str, **kwargs: Any) -> None:
        self.calls.append({'method': method, 'url': url, **kwargs})
        try:
            raise ConnectionResetError('synthetic response lost after send')
        except ConnectionResetError as error:
            raise ProtocolError('synthetic connection reset') from error


def driver(pool: Any) -> HttpClient:
    """Use the pinned driver's actual request loop without server discovery."""
    client = HttpClient.__new__(HttpClient)
    client.headers = {}
    client.params = {}
    client.url = 'http://127.0.0.1:1/'
    client.http = pool
    client.http_retries = Retry(total=0)
    client.timeout = Timeout(connect=1, read=1)
    client.server_host_name = None
    client._send_progress = False
    client._progress_interval = None
    client._autogenerate_query_id = False
    client._active_session = None
    client.show_clickhouse_errors = False
    return client


def insert_request(client: HttpClient) -> None:
    client._raw_request(
        b'synthetic-native-body',
        params={'query_id': QUERY_ID},
        headers={'Content-Type': 'application/octet-stream'},
        server_wait=False,
    )


def test_pinned_driver_replays_reset_even_with_zero_configured_retries() -> None:
    pool = ResetAfterSend()
    with pytest.raises(OperationalError):
        insert_request(driver(pool))
    assert len(pool.calls) == 2


def test_guard_blocks_second_transport_request_after_unknown_first_outcome() -> None:
    pool = ResetAfterSend()
    guard = SingleAttemptPool(pool)
    guard.arm(QUERY_ID)
    with pytest.raises(TransportReplayRefused):
        insert_request(driver(guard))
    assert guard.dispatched
    assert len(pool.calls) == 1
    assert pool.calls[0]['retries'].total == 0
    assert pool.calls[0]['retries'].connect == 0
    assert pool.calls[0]['retries'].read == 0
    assert pool.calls[0]['redirect'] is False


def test_guard_refuses_unarmed_or_mismatched_native_insert_before_dispatch() -> None:
    pool = ResetAfterSend()
    guard = SingleAttemptPool(pool)
    with pytest.raises(TransportReplayRefused):
        insert_request(driver(guard))
    guard.arm('different-query')
    with pytest.raises(TransportReplayRefused):
        insert_request(driver(guard))
    assert not pool.calls


def test_guard_preserves_success_and_does_not_allow_same_attempt_to_be_rearmed() -> None:
    response = SimpleNamespace(status=200, headers={}, data=b'')
    pool = SimpleNamespace(request=lambda *args, **kwargs: response, clear=lambda: None)
    guard = SingleAttemptPool(pool)
    guard.arm(QUERY_ID)
    assert (
        driver(guard)._raw_request(
            b'synthetic-native-body',
            params={'query_id': QUERY_ID},
            headers={'Content-Type': 'application/octet-stream'},
            server_wait=False,
        )
        is response
    )
    with pytest.raises(TransportReplayRefused):
        guard.arm(QUERY_ID)


def test_unconfirmed_attempt_cannot_be_rearmed_with_another_identifier() -> None:
    pool = ResetAfterSend()
    guard = SingleAttemptPool(pool)
    guard.arm(QUERY_ID)
    with pytest.raises(TransportReplayRefused):
        insert_request(driver(guard))
    with pytest.raises(TransportReplayRefused):
        guard.arm('another-attempt')
    assert len(pool.calls) == 1


def test_response_loss_after_body_receipt_sends_only_one_physical_post() -> None:
    accepted_bodies: list[bytes] = []

    class DropResponse(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            accepted_bodies.append(self.rfile.read(int(self.headers['Content-Length'])))
            # The peer has consumed the request. Lose the response with a reset;
            # the sender cannot infer whether a real database committed it.
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
            self.close_connection = True
            self.connection.close()

    server = ThreadingHTTPServer(('127.0.0.1', 0), DropResponse)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    pool = PoolManager()
    guard = SingleAttemptPool(pool)
    guard.arm(QUERY_ID)
    client = driver(guard)
    client.url = f'http://127.0.0.1:{server.server_port}/'
    try:
        with pytest.raises((TransportReplayRefused, OperationalError)):
            insert_request(client)
        assert guard.dispatched
        assert accepted_bodies == [b'synthetic-native-body']
    finally:
        pool.clear()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class SyntheticInsertClient(HttpClient):
    def create_insert_context(self, *args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(settings={})

    def data_insert(self, context: Any) -> Any:
        return self._raw_request(
            b'synthetic-native-body',
            params=context.settings,
            headers={'Content-Type': 'application/octet-stream'},
            server_wait=False,
        )


def test_insert_sender_distinguishes_not_sent_from_unknown_and_never_replays() -> None:
    pool = ResetAfterSend()
    client = driver(pool)
    client.__class__ = SyntheticInsertClient
    sender = NativeInsertOnce(client)
    rows = ((1, 'synthetic'),)
    receipt = sender.send('osprey.events', ('id', 'value'), rows, QUERY_ID)
    assert receipt.outcome == 'unknown'
    assert receipt.query_id == QUERY_ID
    assert rows == ((1, 'synthetic'),)
    repeated = sender.send('osprey.events', ('id', 'value'), rows, QUERY_ID)
    assert repeated.outcome == 'not_sent'
    assert len(pool.calls) == 1


def test_insert_sender_records_local_preparation_failure_as_not_sent() -> None:
    pool = ResetAfterSend()
    client = driver(pool)
    client.__class__ = SyntheticInsertClient

    def fail_before_insert(*args: Any, **kwargs: Any) -> None:
        raise ValueError('synthetic local column preparation error')

    client.create_insert_context = fail_before_insert
    receipt = NativeInsertOnce(client).send('osprey.events', ('id',), ((1,),), QUERY_ID)
    assert receipt.outcome == 'not_sent'
    assert not pool.calls


def test_insert_sender_requires_synchronous_response_and_preserves_confirmed_payload() -> None:
    calls: list[dict[str, Any]] = []
    response = SimpleNamespace(status=200, headers={}, data=b'')

    def request(method: str, url: str, **kwargs: Any) -> Any:
        calls.append({'url': url, **kwargs})
        return response

    client = driver(SimpleNamespace(request=request, clear=lambda: None))
    client.__class__ = SyntheticInsertClient
    receipt = NativeInsertOnce(client).send('osprey.events', ('id',), ((1,),), QUERY_ID)
    assert receipt.outcome == 'confirmed'
    assert 'async_insert=0' in calls[0]['url']
    assert 'wait_for_async_insert=1' in calls[0]['url']
    assert 'wait_end_of_query=1' in calls[0]['url']


def test_unreviewed_driver_cannot_enable_maintenance_insert_transport() -> None:
    with patch('osprey.worker.sinks.single_attempt_insert.package_version', return_value='0.99.0', create=True):
        with pytest.raises(RuntimeError, match='reviewed ClickHouse driver'):
            NativeInsertOnce(driver(ResetAfterSend()))
