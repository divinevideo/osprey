from typing import Optional

import clickhouse_connect
from osprey.worker.lib.config import Config
from osprey.worker.lib.singletons import CONFIG

from .clickhouse import ClickHouseQueryBackend


class ClickHouseClientHolder:
    def __init__(self) -> None:
        self._backend: Optional[ClickHouseQueryBackend] = None
        CONFIG.instance().register_configuration_callback(self.init_from_config)

    def init_from_config(self, config: Config) -> None:
        host = config.get_str('CLICKHOUSE_HOST', 'localhost')
        port = config.get_int('CLICKHOUSE_PORT', 8123)
        user = config.get_str('CLICKHOUSE_USER', 'default')
        password = config.get_str('CLICKHOUSE_PASSWORD', '')
        database = config.get_str('CLICKHOUSE_DATABASE', 'osprey')
        table = config.get_str('CLICKHOUSE_TABLE', 'osprey_events')

        client = clickhouse_connect.get_client(
            host=host,
            port=port,
            username=user,
            password=password,
            database=database,
        )
        self._backend = ClickHouseQueryBackend(client=client, database=database, table=table)

    @property
    def backend(self) -> ClickHouseQueryBackend:
        if not self._backend:
            raise Exception('ClickHouse client not configured')
        return self._backend
