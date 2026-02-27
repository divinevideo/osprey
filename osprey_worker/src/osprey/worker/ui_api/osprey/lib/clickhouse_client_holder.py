from typing import Any, Dict, Optional

import clickhouse_connect
from osprey.worker.lib.config import Config
from osprey.worker.lib.singletons import CONFIG

from .clickhouse import ClickHouseQueryBackend


class ClickHouseClientHolder:
    def __init__(self) -> None:
        self._backend: Optional[ClickHouseQueryBackend] = None
        self._config: Dict[str, Any] = {}
        CONFIG.instance().register_configuration_callback(self._store_config)

    def _store_config(self, config: Config) -> None:
        self._config = {
            'host': config.get_str('CLICKHOUSE_HOST', 'localhost'),
            'port': config.get_int('CLICKHOUSE_PORT', 8123),
            'user': config.get_str('CLICKHOUSE_USER', 'default'),
            'password': config.get_str('CLICKHOUSE_PASSWORD', ''),
            'database': config.get_str('CLICKHOUSE_DATABASE', 'osprey'),
            'table': config.get_str('CLICKHOUSE_TABLE', 'osprey_events'),
        }

    def _init_backend(self) -> ClickHouseQueryBackend:
        client = clickhouse_connect.get_client(
            host=self._config.get('host', 'localhost'),
            port=self._config.get('port', 8123),
            username=self._config.get('user', 'default'),
            password=self._config.get('password', ''),
            database=self._config.get('database', 'osprey'),
        )
        return ClickHouseQueryBackend(
            client=client,
            database=self._config.get('database', 'osprey'),
            table=self._config.get('table', 'osprey_events'),
        )

    @property
    def backend(self) -> ClickHouseQueryBackend:
        if not self._backend:
            self._backend = self._init_backend()
        return self._backend
