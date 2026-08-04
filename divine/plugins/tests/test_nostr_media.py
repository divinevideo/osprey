"""Tests for services.nostr_media: resolving a nostr event's playable media URL
from the relay so COOP's MRT can render the video under review.

Pure parsing (media_from_event) and the fail-open relay fetch (fetch_event_media,
with create_connection mocked) are covered here; both must never raise.
"""

import json
import time

import pytest
from services import nostr_media


def _imeta(*fields):
    return ['imeta', *fields]


# --------------------------------------------------------------------------- #
# media_from_event: pure NIP-92 imeta parse (returns raw url/thumb, no filtering)
# --------------------------------------------------------------------------- #


def test_url_and_thumb():
    ev = {
        'tags': [
            _imeta('url https://media.divine.video/v.mp4', 'm video/mp4', 'thumb https://media.divine.video/t.jpg')
        ]
    }
    assert nostr_media.media_from_event(ev) == ('https://media.divine.video/v.mp4', 'https://media.divine.video/t.jpg')


def test_url_only():
    ev = {'tags': [_imeta('url https://media.divine.video/v.mp4', 'm video/mp4')]}
    assert nostr_media.media_from_event(ev) == ('https://media.divine.video/v.mp4', None)


def test_image_is_thumb_fallback():
    ev = {'tags': [_imeta('url https://m/v.mp4', 'image https://m/i.jpg')]}
    assert nostr_media.media_from_event(ev) == ('https://m/v.mp4', 'https://m/i.jpg')


def test_explicit_thumb_beats_image():
    ev = {'tags': [_imeta('url https://m/v.mp4', 'image https://m/i.jpg', 'thumb https://m/t.jpg')]}
    assert nostr_media.media_from_event(ev) == ('https://m/v.mp4', 'https://m/t.jpg')


def test_no_url_field_in_imeta():
    ev = {'tags': [_imeta('m video/mp4', 'x deadbeef')]}
    assert nostr_media.media_from_event(ev) == (None, None)


def test_no_imeta_tag():
    ev = {'tags': [['e', 'abc'], ['p', 'def']]}
    assert nostr_media.media_from_event(ev) == (None, None)


def test_first_imeta_with_url_wins():
    ev = {'tags': [_imeta('m video/mp4'), _imeta('url https://m/a.mp4'), _imeta('url https://m/b.mp4')]}
    assert nostr_media.media_from_event(ev) == ('https://m/a.mp4', None)


@pytest.mark.parametrize(
    'ev',
    [
        None,
        'x',
        123,
        {},
        {'tags': None},
        {'tags': 'nope'},
        {'tags': ['notalist', [], ['imeta'], [123], ['imeta', 123, 456]]},
    ],
)
def test_malformed_never_raises(ev):
    res = nostr_media.media_from_event(ev)
    assert isinstance(res, tuple) and len(res) == 2


# --------------------------------------------------------------------------- #
# fetch_event_media: fail-open relay fetch (create_connection mocked)
# --------------------------------------------------------------------------- #


class _FakeWS:
    def __init__(self, frames=None, raise_on_recv=None):
        self._frames = list(frames or [])
        self._raise = raise_on_recv
        self.closed = False
        self.close_timeout = None
        self.sent = None
        self.timeouts = []

    def settimeout(self, t):
        self.timeouts.append(t)

    def send(self, data):
        self.sent = data

    def recv(self):
        if self._raise is not None:
            raise self._raise
        if not self._frames:
            raise EOFError('no more frames')
        return self._frames.pop(0)

    def close(self, timeout=3):
        self.closed = True
        self.close_timeout = timeout


def _event_frame(tags):
    return json.dumps(['EVENT', nostr_media._SUB, {'id': 'abc', 'tags': tags}])


def _patch_ws(monkeypatch, ws):
    def fake_create_connection(url, timeout=None):
        return ws

    monkeypatch.setattr(nostr_media, 'create_connection', fake_create_connection)
    return ws


def test_fetch_returns_media_from_event(monkeypatch):
    ws = _patch_ws(
        monkeypatch,
        _FakeWS(
            [_event_frame([_imeta('url https://media.divine.video/v.mp4', 'thumb https://media.divine.video/t.jpg')])]
        ),
    )
    assert nostr_media.fetch_event_media('wss://relay', 'abc') == (
        'https://media.divine.video/v.mp4',
        'https://media.divine.video/t.jpg',
    )
    assert ws.closed  # socket always closed


def test_fetch_sends_req_by_id(monkeypatch):
    ws = _patch_ws(monkeypatch, _FakeWS([_event_frame([_imeta('url https://m/v.mp4')])]))
    nostr_media.fetch_event_media('wss://relay', 'eventid123')
    req = json.loads(ws.sent)
    assert req[0] == 'REQ' and req[2]['ids'] == ['eventid123'] and req[2].get('limit') == 1


def test_fetch_eose_not_found(monkeypatch):
    _patch_ws(monkeypatch, _FakeWS([json.dumps(['EOSE', nostr_media._SUB])]))
    assert nostr_media.fetch_event_media('wss://relay', 'abc') == (None, None)


def test_fetch_skips_frames_for_other_subs_and_notices(monkeypatch):
    frames = [
        json.dumps(['NOTICE', 'hello']),
        json.dumps(['EVENT', 'other-sub', {'tags': [_imeta('url https://m/wrong.mp4')]}]),
        _event_frame([_imeta('url https://m/right.mp4')]),
    ]
    _patch_ws(monkeypatch, _FakeWS(frames))
    assert nostr_media.fetch_event_media('wss://relay', 'abc') == ('https://m/right.mp4', None)


def test_fetch_non_http_url_filtered(monkeypatch):
    _patch_ws(monkeypatch, _FakeWS([_event_frame([_imeta('url data:video/mp4;base64,AAAA')])]))
    assert nostr_media.fetch_event_media('wss://relay', 'abc') == (None, None)


def test_fetch_url_with_crlf_filtered(monkeypatch):
    _patch_ws(monkeypatch, _FakeWS([_event_frame([_imeta('url https://m/v.mp4\r\nX-Injected: 1')])]))
    assert nostr_media.fetch_event_media('wss://relay', 'abc') == (None, None)


def test_fetch_recv_timeout_fails_open(monkeypatch):
    _patch_ws(monkeypatch, _FakeWS(raise_on_recv=TimeoutError('timed out')))
    assert nostr_media.fetch_event_media('wss://relay', 'abc') == (None, None)


def test_fetch_connect_error_fails_open(monkeypatch):
    def boom(url, timeout=None):
        raise OSError('connection refused')

    monkeypatch.setattr(nostr_media, 'create_connection', boom)
    assert nostr_media.fetch_event_media('wss://relay', 'abc') == (None, None)


def test_fetch_socket_closed_even_on_error(monkeypatch):
    ws = _patch_ws(monkeypatch, _FakeWS(raise_on_recv=ValueError('bad frame')))
    nostr_media.fetch_event_media('wss://relay', 'abc')
    assert ws.closed


@pytest.mark.parametrize('relay,eid', [('', 'abc'), ('wss://relay', ''), ('', '')])
def test_fetch_empty_inputs(monkeypatch, relay, eid):
    # must not even attempt a connection
    def boom(*a, **k):
        raise AssertionError('should not connect')

    monkeypatch.setattr(nostr_media, 'create_connection', boom)
    assert nostr_media.fetch_event_media(relay, eid) == (None, None)


def test_fetch_bounded_when_relay_streams_forever(monkeypatch):
    # a relay that never sends our EVENT/EOSE must not loop forever
    class _Flood:
        def __init__(self):
            self.recv_count = 0

        def settimeout(self, t):
            pass

        def send(self, d):
            pass

        def recv(self):
            self.recv_count += 1
            return json.dumps(['EVENT', 'other-sub', {'tags': []}])

        def close(self, timeout=3):
            pass

    flood = _Flood()
    monkeypatch.setattr(nostr_media, 'create_connection', lambda url, timeout=None: flood)
    assert nostr_media.fetch_event_media('wss://relay', 'abc') == (None, None)
    assert flood.recv_count <= nostr_media._MAX_FRAMES


def test_fetch_stops_at_wall_clock_deadline(monkeypatch):
    # A relay that answers steadily but never with our subscription resets the socket timeout
    # on every frame, so the frame cap alone would let it hold the sink for _MAX_FRAMES x
    # timeout. The deadline must cut it off well before the cap is reached.
    class _Chatty:
        def __init__(self):
            self.recv_count = 0

        def settimeout(self, t):
            pass

        def send(self, d):
            pass

        def recv(self):
            self.recv_count += 1
            time.sleep(0.02)
            return json.dumps(['NOTICE', 'still here'])

        def close(self, timeout=3):
            pass

    chatty = _Chatty()
    monkeypatch.setattr(nostr_media, 'create_connection', lambda url, timeout=None: chatty)

    started = time.monotonic()
    assert nostr_media.fetch_event_media('wss://relay', 'abc', timeout=0.1) == (None, None)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5  # would be ~1.3s if only the frame cap applied
    assert chatty.recv_count < nostr_media._MAX_FRAMES


def test_fetch_shrinks_read_timeout_towards_the_deadline(monkeypatch):
    frames = [json.dumps(['NOTICE', 'hi']), _event_frame([_imeta('url https://m/v.mp4')])]
    ws = _patch_ws(monkeypatch, _FakeWS(frames))
    nostr_media.fetch_event_media('wss://relay', 'abc', timeout=2.0)
    assert len(ws.timeouts) == 2
    assert all(0 < t <= 2.0 for t in ws.timeouts)
    assert ws.timeouts[1] < ws.timeouts[0]


def test_fetch_does_not_wait_for_the_peers_close_frame(monkeypatch):
    ws = _patch_ws(monkeypatch, _FakeWS([json.dumps(['EOSE', nostr_media._SUB])]))
    nostr_media.fetch_event_media('wss://relay', 'abc')
    assert ws.close_timeout == 0
