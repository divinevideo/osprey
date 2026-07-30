from typing import Any, List, Sequence, Type

from osprey.engine.udf.base import UDFBase
from osprey.worker.adaptor.plugin_manager import hookimpl_osprey
from osprey.worker.lib.config import Config
from osprey.worker.lib.storage.labels import LabelsServiceBase
from osprey.worker.sinks.sink.output_sink import BaseOutputSink, StdoutOutputSink
from services.coop_sink import COOPSink
from services.labels_service import PostgresLabelsService
from services.relay_manager_sink import RelayManagerSink
from services.zendesk_sink import ZendeskSink
from udfs.age_restrict_nostr_event import AgeRestrictNostrEvent
from udfs.ban_nostr_event import BanNostrEvent
from udfs.check_moderation_result import CheckModerationResult
from udfs.is_trusted_moderation_signer import IsTrustedModerationSigner
from udfs.nostr_account_age import NostrAccountAge
from udfs.resolve_event_author import ResolveEventAuthor


@hookimpl_osprey
def register_udfs() -> Sequence[Type[UDFBase[Any, Any]]]:
    return [
        AgeRestrictNostrEvent,
        BanNostrEvent,
        NostrAccountAge,
        CheckModerationResult,
        ResolveEventAuthor,
        IsTrustedModerationSigner,
    ]


@hookimpl_osprey
def register_output_sinks(config: Config) -> Sequence[BaseOutputSink]:
    sinks: List[BaseOutputSink] = [
        StdoutOutputSink(log_sampler=None),
        RelayManagerSink(),
        ZendeskSink(),
        COOPSink(),
    ]

    # ClickHouse output sink for query UI
    if config.get_bool('OSPREY_CLICKHOUSE_OUTPUT_SINK', False):
        import clickhouse_connect
        from osprey.worker.sinks.sink.clickhouse_output_sink import ClickHouseOutputSink

        ch_client = clickhouse_connect.get_client(
            host=config.expect_str('OSPREY_CLICKHOUSE_HOST'),
            port=config.get_int('OSPREY_CLICKHOUSE_PORT', 8123),
            username=config.get_str('OSPREY_CLICKHOUSE_USER', 'default'),
            password=config.get_str('OSPREY_CLICKHOUSE_PASSWORD', ''),
            database=config.get_str('OSPREY_CLICKHOUSE_DATABASE', 'osprey'),
        )
        sinks.append(
            ClickHouseOutputSink(
                clickhouse_client=ch_client,
                table=config.get_str('OSPREY_CLICKHOUSE_TABLE', 'osprey_events'),
                database=config.get_str('OSPREY_CLICKHOUSE_DATABASE', 'osprey'),
                batch_size=config.get_int('OSPREY_CLICKHOUSE_BATCH_SIZE', 100),
            )
        )

    return sinks


@hookimpl_osprey
def register_labels_service_or_provider(config: Config) -> LabelsServiceBase:
    """Register a PostgreSQL-backed labels service."""
    return PostgresLabelsService()
