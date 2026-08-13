"""Local-only stubs standing in for funnelcake's REST API and relay-manager.

Exists so the composed osprey branches can be exercised end to end on a laptop:
ResolveEventAuthor needs an event lookup, and the age-restrict effect needs
somewhere to send its enforcement call. Both record what they were asked for so
a test can assert on the request rather than on a log line.

Not shipped. Not a fake worth trusting beyond "our side of the contract".
"""

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HEX64 = re.compile(r'^[0-9a-f]{64}$')

# event_id -> pubkey, seeded via PUT /_seed so a test controls what resolves.
EVENTS: dict[str, str] = {}
# Everything relay-manager was asked to do, in order.
CALLS: list[dict] = []
# Programmable response, so the refusal paths can be exercised: POST /_respond
# with {"status": 409, "body": {...}} and the next relay-manager call gets it.
NEXT: dict = {}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # noqa: A003 - quieten default access logging
        print(f'[stub] {fmt % args}', flush=True)

    def do_GET(self):  # noqa: N802
        # Inspection endpoints for the test harness.
        if self.path == '/_calls':
            return self._send(200, CALLS)
        if self.path == '/_events':
            return self._send(200, EVENTS)

        # funnelcake: GET /api/event/{id}
        m = re.match(r'^/api/event/([^/?]+)$', self.path)
        if m:
            event_id = m.group(1).lower()
            pubkey = EVENTS.get(event_id)
            if not pubkey:
                return self._send(404, {'error': 'Event not found'})
            # Shape mirrors the real API: a bare Nostr event.
            return self._send(200, {
                'id': event_id,
                'pubkey': pubkey,
                'created_at': 1785000000,
                'kind': 34236,
                'tags': [],
                'content': '',
                'sig': '0' * 128,
            })
        return self._send(404, {'error': 'not found'})

    def do_PUT(self):  # noqa: N802
        if self.path != '/_seed':
            return self._send(404, {'error': 'not found'})
        payload = json.loads(self._body() or '{}')
        for event_id, pubkey in payload.items():
            EVENTS[event_id.lower()] = pubkey.lower()
        return self._send(200, {'seeded': len(payload)})

    def do_POST(self):  # noqa: N802
        if self.path == '/_reset':
            CALLS.clear()
            return self._send(200, {'cleared': True})

        body = self._body()
        try:
            parsed = json.loads(body or '{}')
        except json.JSONDecodeError:
            parsed = {'_raw': body.decode('utf-8', 'replace')}

        if self.path == '/_respond':
            NEXT.clear()
            NEXT.update(parsed)
            return self._send(200, {'armed': parsed})

        # relay-manager: /api/relay-rpc and /api/moderate-media
        CALLS.append({'path': self.path, 'body': parsed})
        print(f'[stub] CALL {self.path} {json.dumps(parsed)}', flush=True)
        if NEXT:
            armed = dict(NEXT)
            NEXT.clear()
            return self._send(int(armed.get('status', 200)), armed.get('body', {}))
        return self._send(200, {'success': True, 'result': 'ok'})

    def _body(self) -> bytes:
        length = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(length) if length else b''


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8080'))
    print(f'[stub] listening on {port}', flush=True)
    ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()
