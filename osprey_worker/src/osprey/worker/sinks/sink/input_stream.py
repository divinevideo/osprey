import abc
import inspect
import json
import time
from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Generic, Iterator, List, Optional, Sequence, Type, TypeVar, Union, cast

import gevent
import msgpack
import sentry_sdk
from gevent.lock import RLock
from gevent.queue import Queue as GeventQueue
from google.api_core import retry
from google.cloud.pubsub_v1 import SubscriberClient, types
from google.cloud.pubsub_v1.subscriber.message import Message
from google.protobuf.message import DecodeError
from google.protobuf.message import Message as ProtoMessage
from google.pubsub_v1 import PubsubMessage
from kafka.consumer.fetcher import ConsumerRecord
from kafka.consumer.subscription_state import ConsumerRebalanceListener
from osprey.engine.executor.execution_context import Action
from osprey.worker.lib.encryption.envelope import Envelope
from osprey.worker.lib.instruments import metrics
from osprey.worker.lib.osprey_shared.logging import get_logger
from osprey.worker.lib.storage.postgres import Model, scoped_session
from osprey.worker.lib.utils.dates import parse_go_timestamp
from osprey.worker.sinks.output_handoff import Disposition, OutputHandoff, SourcePosition
from osprey.worker.sinks.utils.acking_contexts import (
    BaseAckingContext,
    NoopAckingContext,
    PubSubMessageAckingContext,
    PullPubSubMessageContext,
)
from osprey.worker.sinks.utils.gevent import FairRLock
from osprey.worker.sinks.utils.kafka import PatchedKafkaConsumer
from pydantic import BaseModel
from tenacity import RetryCallState, retry_if_exception_type, stop_never, wait_exponential
from tenacity import retry as tenacity_retry
from typing_extensions import Protocol

logger = get_logger()

_T = TypeVar('_T', covariant=True)
_PydanticModelT = TypeVar('_PydanticModelT', bound=BaseModel, covariant=True)


if TYPE_CHECKING:
    from google.cloud.pubsub_v1.types import PullResponse


class BaseInputStream(abc.ABC, Generic[_T]):
    """The base input stream produces objects that the sink will process."""

    def __init__(self) -> None:
        super().__init__()
        self._iterator: Optional[Iterator[_T]] = None
        self._iterator_lock = RLock()

    @abc.abstractmethod
    def _gen(self) -> Iterator[_T]:
        """Returns an iterator which yields the objects to process.

        Will only be called once per instance of the stream, and that iterator will be re-used."""
        raise NotImplementedError

    def __iter__(self) -> Iterator[_T]:
        return self

    def __next__(self) -> _T:
        iterator = self._iterator
        if iterator is None:
            with self._iterator_lock:
                # Re-check that we haven't set it
                iterator = self._iterator
                if iterator is None:
                    iterator = self._iterator = self._gen()
        if inspect.isgenerator(iterator):
            # Generators can only have one caller at a time
            with self._iterator_lock:
                # mypy isn't able to work out what this type is.
                return cast(_T, next(iterator))

        # If not a generator, no need to wait for a lock
        return next(iterator)


class StaticInputStream(BaseInputStream[_T]):
    """An input stream that will return a static list of actions, until exhausted."""

    def __init__(self, actions: Sequence[_T]):
        super().__init__()
        self._actions = deque(actions)

    def _gen(self) -> Iterator[_T]:
        return iter(self._actions)


class StaticMessagesInputStream(
    BaseInputStream[Union[NoopAckingContext[Action], PullPubSubMessageContext[List[_PydanticModelT]]]]
):
    """An input stream that will return a static list of contexts, until exhausted.

    This should only be used in tests to mock a stream of message contexts.
    """

    def __init__(
        self, contexts: Sequence[Union[NoopAckingContext[Action], PullPubSubMessageContext[List[_PydanticModelT]]]]
    ):
        super().__init__()
        self._contexts = list(contexts)

    def _gen(self) -> Iterator[Union[NoopAckingContext[Action], PullPubSubMessageContext[List[_PydanticModelT]]]]:
        return iter(self._contexts)


class InfiniteStaticInputStream(BaseInputStream[_T]):
    def __init__(self, item: _T):
        super().__init__()
        self._item = item

    def _gen(self) -> Iterator[_T]:
        while True:
            yield self._item


class BasePubSubInputStream(BaseInputStream[_T]):
    def __init__(self, subscriber: SubscriberClient, subscription_path: str, max_messages: int = 250):
        super().__init__()
        self.subscriber = subscriber
        self.subscription_path = subscription_path
        self.max_messages = max_messages

    def _pull(self, max_messages: Optional[int] = None) -> 'PullResponse':
        max_messages_to_pull = max_messages if max_messages is not None else self.max_messages
        with metrics.timed('pubsub_consumer.poll_time', tags=[f'subscription_path:{self.subscription_path}']):
            return self.subscriber.pull(
                subscription=self.subscription_path,
                max_messages=max_messages_to_pull,
                retry=retry.Retry(timeout=300),
            )


class PubSubOspreyActionInputStream(BasePubSubInputStream[BaseAckingContext[Action]]):
    def __init__(
        self,
        subscriber: SubscriberClient,
        subscription_path: str,
        kek_uri: str = '',
        max_messages: int = 250,
        gevent_queue_size: int = 1000,
    ):
        super().__init__(subscriber, subscription_path, max_messages)
        self.queue = GeventQueue(maxsize=gevent_queue_size)
        self.encryption_envelope = Envelope(kek_uri=kek_uri, gcp_credential_path='')  # use default gcp credentials

    def _handle_message_data_if_is_secure(self, message: Message) -> bytes:
        is_secure = message.attributes.get('encrypted', 'false') == 'true'
        message_data: bytes = message.data
        if is_secure:
            # if the message is encrypted then decrypt the message data before proceeding
            with metrics.timed('osprey_actions.encryption.decrypt'):
                message_data = self.encryption_envelope.decrypt(message_data)
        return message_data

    def _create_action(self, message: Message) -> Action:
        """
        Construct an Action from a pubsub Message

        Until we have strict protobuf types, if this method is changed, make sure
        content_cop/pydantic_models/messages.py::OspreyRulesInput is still compatible.
        """
        encoding = message.attributes.get('encoding')

        message_data: bytes = self._handle_message_data_if_is_secure(message)

        if encoding == 'proto':
            from osprey.worker.adaptor.plugin_manager import bootstrap_action_proto_deserializer

            deserializer = bootstrap_action_proto_deserializer()
            if deserializer is not None:
                res = deserializer.proto_bytes_to_dict(message_data)
                return Action(
                    action_id=res.action_id,
                    action_name=res.action_name,
                    data=res.data,
                    timestamp=message.publish_time,
                )
            else:
                logger.warning('Proto deserializer plugin not available, falling back to JSON processing')
        # Process as JSON (either explicitly requested or fallback from proto)
        # Right now there are two formats for loading messages
        # This code will prevent errors until the old format is flushed out
        # This will mainly affect the hasher
        try:
            # old format
            action = msgpack.loads(message_data)
            return Action(
                action_id=int(action['id']),
                action_name=action['name'],
                data=action['data'],
                secret_data=action.get('secret_data', {}),
                timestamp=message.publish_time,
            )
        except Exception:
            # new format
            action = json.loads(msgpack.loads(message_data))
            return Action(
                action_id=int(action['id']),
                action_name=action['name'],
                data=action['data'],
                secret_data=action.get('secret_data', {}),
                timestamp=message.publish_time,
            )


# TODO: this maybe not needed anymore with Coordinator
class AsyncPubSubOspreyActionInputStream(PubSubOspreyActionInputStream):
    def _worker(self) -> None:
        logger.info('Pubsub Consumer Worker spawned')

        def stream_callback(message: Message) -> None:
            with metrics.timed('pubsub_input_stream.queue.put_time'):
                self.queue.put(message)

        with self.subscriber as subscriber:
            flow_control = types.FlowControl(
                max_messages=self.max_messages,
                # assume 4KB per message
                max_bytes=4_000 * self.max_messages,
                max_lease_duration=60 * 60,
                max_duration_per_lease_extension=600,
            )
            streaming_pull_future = subscriber.subscribe(
                self.subscription_path, callback=stream_callback, flow_control=flow_control
            )

            while True:
                try:
                    streaming_pull_future.result()
                except Exception as e:
                    logger.error(e)
                    sentry_sdk.capture_exception(error=e)
                    continue

    def _gen(self) -> Iterator[PubSubMessageAckingContext[Action]]:
        gevent.spawn(self._worker)
        while True:
            try:
                with metrics.timed('pubsub_input_stream.queue.get_time'):
                    received_message = self.queue.get()
                    assert isinstance(received_message, Message)

                action = self._create_action(received_message)
                delay = datetime.now(timezone.utc) - action.timestamp
                metrics.timing('input_stream_delay', delay.total_seconds(), ['stream:pubsub_input_stream'])
                yield PubSubMessageAckingContext(action, received_message)
            except Exception:
                logger.exception('Error while generating input message')
                sentry_sdk.capture_exception()
                continue


class SynchronousPubSubMultiProtoInputStream(BasePubSubInputStream[PullPubSubMessageContext[ProtoMessage]]):
    def __init__(
        self,
        subscriber: SubscriberClient,
        subscription_path: str,
        proto_message_classes: List[Type[ProtoMessage]],
        max_messages: int = 250,
    ):
        super().__init__(subscriber, subscription_path, max_messages)
        self.proto_message_classes = proto_message_classes

    def _gen(self) -> Iterator[PullPubSubMessageContext[ProtoMessage]]:
        with SubscriberClient() as subscriber:
            while True:
                response = self._pull()
                if not response.received_messages:
                    logger.debug(f'No messages received for subscription {self.subscription_path}')
                    continue
                for received_message in response.received_messages:
                    message = received_message.message
                    assert isinstance(message, PubsubMessage)

                    item = None
                    for proto_message_class in self.proto_message_classes:
                        try:
                            item = proto_message_class.FromString(message.data)
                            break
                        except DecodeError:
                            continue

                    if not item:
                        raise DecodeError(f'Error decoding message as any of {self.proto_message_classes}')

                    yield PullPubSubMessageContext(
                        item=item,
                        subscriber=subscriber,
                        subscription_path=self.subscription_path,
                        ack_ids=[received_message.ack_id],
                        publish_time=message.publish_time.ToDatetime(),
                        attributes={k: v for k, v in message.attributes.items()},
                    )


# Use for utility scripts, not for production
class RawPubSubInputStream(BasePubSubInputStream[PullPubSubMessageContext[PubsubMessage]]):
    def _gen(self) -> Iterator[PullPubSubMessageContext[PubsubMessage]]:
        with SubscriberClient() as subscriber:
            while True:
                response = self._pull()
                if not response.received_messages:
                    continue

                for received_message in response.received_messages:
                    message = received_message.message
                    assert isinstance(message, PubsubMessage)
                    yield PullPubSubMessageContext(
                        item=message,
                        subscriber=subscriber,
                        subscription_path=self.subscription_path,
                        ack_ids=[received_message.ack_id],
                    )


class PubSubBulkInputStream(BasePubSubInputStream[PullPubSubMessageContext[List[_PydanticModelT]]]):
    def __init__(
        self, subscriber: SubscriberClient, subscription_path: str, model: Type[_PydanticModelT], bulk_size: int
    ):
        super().__init__(subscriber, subscription_path)
        self._model = model
        self._bulk_size = bulk_size

    def _gen(self) -> Iterator[PullPubSubMessageContext[List[_PydanticModelT]]]:
        with self.subscriber as subscriber:
            while True:
                try:
                    response = self._pull(max_messages=self._bulk_size)
                    if not response.received_messages:
                        logger.debug(f'No messages received for subscription {self.subscription_path}')
                        continue

                    messages: List[_PydanticModelT] = []
                    ack_ids: List[str] = []

                    for received_message in response.received_messages:
                        message = received_message.message
                        assert isinstance(message, PubsubMessage)
                        messages.append(self._create_element(message))
                        ack_ids.append(received_message.ack_id)

                    if messages:  # Only yield if we have messages to process
                        yield PullPubSubMessageContext(
                            item=messages,
                            subscriber=subscriber,
                            subscription_path=self.subscription_path,
                            ack_ids=ack_ids,
                            attributes={k: v for k, v in message.attributes.items()},
                        )
                except BaseException as e:
                    logger.exception(f'Failure in PubSubBulkInputStream._gen: {e}')
                    sentry_sdk.capture_exception()

    def _create_element(self, message: 'PubsubMessage') -> _PydanticModelT:
        return self._model.parse_raw(message.data)


_ModelT = TypeVar('_ModelT', bound=Model, covariant=True)


class _ClaimableModel(Protocol[_ModelT]):
    @classmethod
    def claim(cls) -> Optional[_ModelT]: ...


class PostgresInputStream(BaseInputStream[_ModelT]):
    def __init__(self, model: Type[_ClaimableModel[_ModelT]], cooldown: float = 3, tags: Sequence[str] = ()):
        super().__init__()
        self._model = model
        self._cooldown = cooldown
        self._tags = tags

    def _gen(self) -> Iterator[_ModelT]:
        def _before_sleep(retry_state: RetryCallState) -> None:
            if retry_state.next_action:
                gevent.sleep(retry_state.next_action.sleep)

        def _log_before_attempt(retry_state: RetryCallState) -> None:
            if retry_state.outcome and retry_state.outcome.failed:
                exception = retry_state.outcome.exception()
                if exception:
                    logger.error(f'Failed to claim or process row: {exception}')
                    sentry_sdk.capture_exception(exception)

        @tenacity_retry(
            wait=wait_exponential(multiplier=self._cooldown, min=self._cooldown, max=self._cooldown * 10),
            stop=stop_never,
            retry=retry_if_exception_type(Exception),
            reraise=True,
            before_sleep=_before_sleep,
            before=_log_before_attempt,
        )
        def claim_with_retry() -> Optional[_ModelT]:
            with metrics.timed('claim_postgres_row', tags=self._tags):
                return self._model.claim()

        while True:
            with scoped_session():
                data = claim_with_retry()
                if data:
                    yield data
                else:
                    gevent.sleep(self._cooldown)


class HandoffActionContext(NoopAckingContext[Action]):
    """Offset accounting is separate from the unchanged steady-state Kafka ack."""

    def __init__(self, action: Action, handoff: OutputHandoff, source: SourcePosition):
        super().__init__(action)
        self._handoff = handoff
        self._source = source
        self._disposition: Disposition = 'failed'

    def set_disposition(self, disposition: Disposition) -> None:
        self._disposition = disposition

    def __exit__(self, exc_type, exc_value, exc_traceback) -> None:
        self._handoff.finish(self._source, 'failed' if exc_type is not None or self._should_nack else self._disposition)


class KafkaInputStream(BaseInputStream[BaseAckingContext[Action]]):
    """An input stream that consumes messages from a Kafka topic and yields Action objects wrapped in an AckingContext."""

    def __init__(self, kafka_consumer: PatchedKafkaConsumer):
        super().__init__()
        self._consumer: PatchedKafkaConsumer = kafka_consumer
        self._handoff: OutputHandoff | None = None
        self._consumer_lock = FairRLock()
        self._assignment_seen = False
        self._membership_epoch = 0
        self._last_membership_poll = 0.0
        self._held = False
        self._accepted_next_offsets: dict[tuple[str, int], int] = {}
        self._commit_state = 'unrequested'
        self._committed_offsets: dict[Any, Any] = {}
        self._activation_epoch: int | None = None

    def configure_handoff(self, handoff: OutputHandoff, *, start_paused: bool = False) -> None:
        if not handoff.enabled:
            return
        if self._iterator is not None or self._handoff is not None:
            raise ValueError('maintenance must be configured before Kafka intake')
        self._handoff = handoff
        if start_paused:
            handoff.pause_before_intake()
        stream = self

        class Listener(ConsumerRebalanceListener):  # type: ignore[misc]  # Pinned kafka-python has no type metadata.
            def on_partitions_revoked(self, partitions):
                if stream._assignment_seen and partitions:
                    handoff.membership_changed()
                stream._membership_epoch += 1

            def on_partitions_assigned(self, partitions):
                if stream._assignment_seen:
                    handoff.membership_changed()
                stream._assignment_seen = True
                stream._membership_epoch += 1
                if not handoff.admission_open and partitions:
                    stream._consumer.pause(*partitions)

        self._consumer.subscribe(topics=sorted(self._consumer.subscription()), listener=Listener())

    @property
    def holding(self) -> bool:
        return self._held and time.monotonic() - self._last_membership_poll <= 2

    def request_handoff_activation(self) -> bool:
        """Signal-safe request; the polling owner validates metadata before opening."""
        handoff = self._handoff
        if (
            handoff is None
            or handoff.snapshot()['state'] != 'paused'
            or not self.holding
            or not self._assignment_seen
            or self._activation_epoch is not None
        ):
            return False
        self._activation_epoch = self._membership_epoch
        return True

    def handoff_startup_status(self) -> dict[str, Any]:
        with self._consumer_lock:
            handoff = self._handoff
            state = handoff.snapshot() if handoff is not None else {}
            assignment = set(self._consumer.assignment())
            offsets = self._consumer._subscription.all_consumed_offsets() if state.get('state') == 'paused' else {}
            return {
                'ready': bool(
                    state.get('state') == 'paused'
                    and self.holding
                    and assignment
                    and set(offsets) == assignment
                    and not state['pending_bytes']
                    and not state['pending_batches']
                    and not state['pending_deliveries']
                    and not state['blocked_reasons']
                ),
                'membership_epoch': self._membership_epoch,
                'positions': [
                    {'topic': partition.topic, 'partition': partition.partition, 'next_offset': value.offset}
                    for partition, value in sorted(offsets.items())
                ],
            }

    def _activate_after_poll(self) -> None:
        if self._activation_epoch is None:
            return
        handoff = self._handoff
        assert handoff is not None
        if self._activation_epoch != self._membership_epoch:
            self._activation_epoch = None
            handoff.block('membership_changed')
            return
        if handoff.snapshot()['state'] != 'paused':
            self._activation_epoch = None
            return
        assignment = set(self._consumer.assignment())
        offsets = self._consumer._subscription.all_consumed_offsets()
        if not assignment or set(offsets) != assignment or self._commit_state != 'unrequested':
            return
        if handoff.activate_before_intake():
            self._activation_epoch = None

    def request_handoff_commit(self) -> bool:
        handoff = self._handoff
        if handoff is None:
            return False
        with self._consumer_lock:
            if self._commit_state in {'pending', 'confirmed'}:
                return True
            if self._commit_state == 'failed':
                return False
            snapshot = handoff.snapshot()
            if (
                snapshot['state'] != 'draining'
                or snapshot['active']
                or snapshot['poll_active']
                or snapshot['pending_bytes']
                or snapshot['pending_batches']
                or snapshot['pending_deliveries']
                or snapshot['blocked_reasons']
                or not self._held
                or not self._assignment_seen
                or time.monotonic() - self._last_membership_poll > 2
            ):
                return False
            assignment = set(self._consumer.assignment())
            epoch = self._membership_epoch
            # Pinned kafka-python 1.4.7 uses this metadata-only snapshot for its
            # own automatic commits. position() may block fetching a position;
            # never interrupt membership service with that network operation.
            offsets = dict(self._consumer._subscription.all_consumed_offsets())
            if not assignment or set(offsets) != assignment:
                return False
            for partition, value in offsets.items():
                expected = self._accepted_next_offsets.get((partition.topic, partition.partition))
                if expected is not None and value.offset != expected:
                    handoff.block('invalid_source_record')
                    return False
            self._commit_state = 'pending'

            def committed(actual_offsets, response) -> None:
                if epoch != self._membership_epoch or assignment != set(self._consumer.assignment()):
                    self._commit_state = 'failed'
                    handoff.membership_changed()
                elif response is not None or actual_offsets != offsets:
                    self._commit_state = 'failed'
                    handoff.block('offset_commit_failed')
                else:
                    self._committed_offsets = offsets
                    self._commit_state = 'confirmed'

            try:
                # The returned future may represent coordinator lookup rather
                # than the actual commit. Only the explicit callback is proof.
                self._consumer.commit_async(offsets=offsets, callback=committed)
            except Exception:
                self._commit_state = 'failed'
                handoff.block('offset_commit_failed')
                return False
            return True

    def handoff_offsets(self) -> list[dict[str, Any]]:
        return [
            {'topic': partition.topic, 'partition': partition.partition, 'next_offset': value.offset}
            for partition, value in sorted(self._committed_offsets.items())
        ]

    def _handoff_gen(self) -> Iterator[BaseAckingContext[Action]]:
        handoff = self._handoff
        assert handoff is not None
        while True:
            with self._consumer_lock:
                held = not handoff.admission_open
                self._held = held
                assignment = self._consumer.assignment()
                if held:
                    if assignment:
                        self._consumer.pause(*assignment)
                    admitted = handoff.begin_membership_poll()
                else:
                    if assignment:
                        self._consumer.resume(*assignment)
                    admitted = handoff.begin_poll()
                if not admitted:
                    continue
                try:
                    polled = self._consumer.poll(timeout_ms=250, max_records=1)
                except Exception:
                    handoff.accept_poll_result([])
                    handoff.block('invalid_source_record')
                    gevent.sleep(0.25)
                    continue
                records = [record for batch in polled.values() for record in batch]
                positions = [SourcePosition(record.topic, record.partition, record.offset) for record in records]
                # poll() advances offsets before returning. Never discard a
                # returned record merely because admission closed meanwhile.
                handoff.accept_poll_result(positions)
                for position in positions:
                    self._accepted_next_offsets[(position.topic, position.partition)] = position.offset + 1
                self._last_membership_poll = time.monotonic()
                self._held = held
                self._activate_after_poll()
                if self._commit_state == 'confirmed' and held:
                    handoff.certify(committed=True)
            for record, position in zip(records, positions):
                try:
                    data = json.loads(record.value)
                    action_data = data['data']
                    action = Action(
                        action_id=int(action_data['action_id']),
                        action_name=action_data['action_name'],
                        data=action_data['data'],
                        timestamp=parse_go_timestamp(data['send_time']),
                    )
                    handoff.bind_action(action, position)
                except Exception:
                    handoff.block('invalid_source_record')
                    handoff.finish(position, 'failed')
                    continue
                yield HandoffActionContext(action, handoff, position)

    def _gen(self) -> Iterator[BaseAckingContext[Action]]:
        if self._handoff is not None:
            yield from self._handoff_gen()
            return
        while True:
            try:
                with metrics.timed('kafka_consumer.lock_time'):
                    with metrics.timed('kafka_consumer.poll_time'):
                        record: ConsumerRecord = next(self._consumer)
                data = json.loads(record.value)
                timestamp = parse_go_timestamp(data['send_time'])
                action_data = data['data']
                # this was here for when this was protobuf. If its json by default, we should just assume its all in one
                # json blob.
                # action_data = json.loads(action_data_json)

                action = Action(
                    action_id=int(action_data['action_id']),
                    action_name=action_data['action_name'],
                    data=action_data['data'],
                    timestamp=timestamp,
                )
                # Wrap in NoopAckingContext for now, or implement a KafkaAckingContext if needed
                yield NoopAckingContext(action)
            except Exception as e:
                logger.exception('Error while consuming from Kafka')
                sentry_sdk.capture_exception(e)
                continue
