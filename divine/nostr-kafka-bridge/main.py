"""Nostr-Kafka Bridge: subscribes to a Nostr relay and publishes events to Kafka."""

import asyncio
import json
import logging
import os
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import websockets
from kafka import KafkaProducer

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('nostr-kafka-bridge')

RELAY_URL = os.environ.get('RELAY_URL', 'wss://relay.divine.video')
KAFKA_BOOTSTRAP_SERVERS = os.environ.get('KAFKA_BOOTSTRAP_SERVERS', 'kafka:9092')
KAFKA_TOPIC = os.environ.get('KAFKA_TOPIC', 'nostr-events')
HEALTH_PORT = int(os.environ.get('HEALTH_PORT', '8080'))

connected = False


# --- Health check ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        status = 200 if connected else 503
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b'ok' if connected else b'disconnected')

    def log_message(self, *_):
        pass


def start_health_server():
    server = HTTPServer(('0.0.0.0', HEALTH_PORT), HealthHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    log.info('Health check listening on :%d', HEALTH_PORT)


# --- Kafka producer ---
def make_producer() -> KafkaProducer:
    return KafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS.split(','),
        value_serializer=lambda v: json.dumps(v).encode(),
    )


# --- Main loop ---
async def bridge():
    global connected
    producer = make_producer()
    backoff = 1

    while True:
        try:
            sub_id = uuid.uuid4().hex[:16]
            log.info('Connecting to %s (sub %s)', RELAY_URL, sub_id)

            async with websockets.connect(RELAY_URL) as ws:
                # Subscribe to all events
                await ws.send(json.dumps(['REQ', sub_id, {}]))
                connected = True
                backoff = 1
                log.info('Connected and subscribed')

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    if not isinstance(msg, list) or len(msg) < 3:
                        continue

                    if msg[0] == 'EVENT':
                        event = msg[2]
                        producer.send(KAFKA_TOPIC, value=event)
                        log.debug('Published event %s', event.get('id', '?')[:12])

        except Exception as exc:
            connected = False
            log.warning('Disconnected (%s), retrying in %ds', exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def main():
    start_health_server()
    asyncio.run(bridge())


if __name__ == '__main__':
    main()
