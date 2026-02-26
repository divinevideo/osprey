"""Divine-specific sink registration — adds ClickHouseOutputSink alongside standard sinks."""

from typing import List, Sequence

from osprey.worker.adaptor.plugin_manager import hookimpl_osprey
from osprey.worker.lib.config import Config
from osprey.worker.sinks.sink.output_sink import BaseOutputSink, StdoutOutputSink


@hookimpl_osprey
def register_output_sinks(config: Config) -> Sequence[BaseOutputSink]:
    sinks: List[BaseOutputSink] = []

    if config.get_bool('OSPREY_STDOUT_OUTPUT_SINK', False):
        sinks.append(StdoutOutputSink())

    # Kafka output sink (if still needed for other consumers)
    if config.get_bool('OSPREY_KAFKA_OUTPUT_SINK', False):
        from kafka import KafkaProducer
        from osprey.worker.sinks.sink.kafka_output_sink import KafkaOutputSink

        output_topic = config.expect_str('OSPREY_KAFKA_OUTPUT_TOPIC')
        bootstrap_servers = config.expect_str_list('OSPREY_KAFKA_BOOTSTRAP_SERVERS')
        client_id = config.expect_str('OSPREY_KAFKA_OUTPUT_CLIENT_ID')
        sinks.append(
            KafkaOutputSink(
                kafka_topic=output_topic,
                kafka_producer=KafkaProducer(bootstrap_servers=bootstrap_servers, client_id=client_id),
            )
        )

    # ClickHouse output sink (Divine)
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
                batch_size=config.get_int('OSPREY_CLICKHOUSE_BATCH_SIZE', 500),
            )
        )

    # Execution result storage
    from osprey.worker._stdlibplugin.execution_result_store_chooser import (
        get_rules_execution_result_storage_backend,
    )
    from osprey.worker.lib.storage import ExecutionResultStorageBackendType
    from osprey.worker.sinks.sink.stored_execution_result_output_sink import StoredExecutionResultOutputSink

    storage_backend_type = ExecutionResultStorageBackendType(
        config.get_str('OSPREY_EXECUTION_RESULT_STORAGE_BACKEND', 'none').lower()
    )
    storage_backend = get_rules_execution_result_storage_backend(backend_type=storage_backend_type)
    if storage_backend is not None:
        sinks.append(StoredExecutionResultOutputSink())

    return sinks
