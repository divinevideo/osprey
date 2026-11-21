from dataclasses import dataclass
from typing import Any

import sentry_sdk
from gevent.lock import Semaphore
from kafka import KafkaProducer
from osprey.engine.executor.execution_context import ExecutionResult
from osprey.worker.lib.osprey_shared.logging import get_logger
from osprey.worker.sinks.output_handoff import OutputHandoff, SourcePosition
from osprey.worker.sinks.sink.output_sink import BaseOutputSink

logger = get_logger()


@dataclass
class PendingDelivery:
    payload: bytes
    source: SourcePosition | None
    future: Any = None
    failed: bool = False


class KafkaOutputSink(BaseOutputSink):
    """An output sink that sends the extracted features to a given kafka topic."""

    def __init__(self, kafka_topic: str, kafka_producer: KafkaProducer):
        self._kafka_topic = kafka_topic
        self._kafka_producer = kafka_producer
        self._handoff: OutputHandoff | None = None
        self._accounting_owner = object()
        self._lock = Semaphore()
        self._deliveries: dict[int, PendingDelivery] = {}
        self._next_delivery = 0

    def configure_handoff(self, handoff: OutputHandoff) -> None:
        if not handoff.enabled:
            return
        with self._lock:
            if self._handoff is not None or self._deliveries:
                handoff.block('required_output_failed')
                raise ValueError('maintenance must be configured before Kafka output')
            self._handoff = handoff

    def _update_pending(self) -> None:
        assert self._handoff is not None
        self._handoff.set_pending_deliveries(
            len(self._deliveries),
            bytes_pending=sum(len(value.payload) for value in self._deliveries.values()),
            owner=self._accounting_owner,
        )

    def _confirmed(self, token: int, metadata: Any) -> None:
        with self._lock:
            delivery = self._deliveries.get(token)
            if delivery is not None and not delivery.failed:
                del self._deliveries[token]
                self._update_pending()

    def _failed(self, token: int, error: Any) -> None:
        with self._lock:
            if token in self._deliveries:
                self._deliveries[token].failed = True
                assert self._handoff is not None
                self._handoff.block('kafka_delivery_failed')
                self._update_pending()

    def maintenance_status(self) -> dict[str, int]:
        with self._lock:
            return {
                'pending_deliveries': len(self._deliveries),
                'failed_deliveries': sum(delivery.failed for delivery in self._deliveries.values()),
            }

    def will_do_work(self, result: ExecutionResult) -> bool:
        return True

    def push(self, result: ExecutionResult) -> None:
        if self._handoff is not None:
            delivery = PendingDelivery(
                result.extracted_features_json.encode('utf-8'), self._handoff.source_for_action(result.action)
            )
            with self._lock:
                token = self._next_delivery
                self._next_delivery += 1
                self._deliveries[token] = delivery
                self._update_pending()
            try:
                future = self._kafka_producer.send(topic=self._kafka_topic, value=delivery.payload)
                delivery.future = future
                future.add_callback(self._confirmed, token)
                future.add_errback(self._failed, token)
            except BaseException as error:
                # Timeout may interrupt send after producer admission. Retain
                # exact output; never replay classification or sibling sinks.
                self._failed(token, error)
            return
        kafka_future: Any = self._kafka_producer.send(
            topic=self._kafka_topic, value=result.extracted_features_json.encode('utf-8')
        )
        kafka_future.add_errback(self.push_err_to_sentry)

    @classmethod
    def push_err_to_sentry(cls, e: Exception) -> None:
        logger.error(f'exception raised when pushing event to kafka: {str(e)}')
        sentry_sdk.capture_exception(error=e)

    def stop(self) -> None:
        if self._handoff is not None:
            try:
                self._kafka_producer.flush(timeout=10)
            except BaseException:
                self._handoff.block('kafka_delivery_failed')
            # A flush return is insufficient: failed/unresolved futures stay
            # retained and counted until their individual outcome is known.
