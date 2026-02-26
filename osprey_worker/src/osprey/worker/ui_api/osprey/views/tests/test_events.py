import json
from datetime import datetime
from typing import Any, Dict, Mapping
from unittest import mock

import pytest
from flask import Flask, Response, url_for
from flask.testing import FlaskClient
from osprey.worker.ui_api.osprey.lib.clickhouse import (
    BaseClickHouseQuery,
    PaginatedScanClickHouseQuery,
    TimeseriesClickHouseQuery,
    TopNClickHouseQuery,
)
from osprey.worker.ui_api.osprey.validators.events import BulkLabelTopNRequest

_base_query = BaseClickHouseQuery(start=datetime.now(), end=datetime.now(), query_filter='', entity=None)

config_a = {
    'main.sml': '',
    'config.yaml': json.dumps(
        {
            'acl': {
                'users': {
                    'local-dev@localhost': {'abilities': [{'name': 'CAN_VIEW_EVENTS_BY_ENTITY', 'allow_all': True}]}
                }
            }
        }
    ),
}

config_b = {
    'main.sml': '',
    'config.yaml': json.dumps(
        {
            'acl': {
                'users': {
                    'local-dev@localhost': {
                        'abilities': [
                            {'name': 'CAN_VIEW_EVENTS_BY_ENTITY', 'allow_all': True},
                            {
                                'name': 'CAN_VIEW_EVENTS_BY_ACTION',
                                'allow_specific': ['some_allowance_name', 'another_allowance_name'],
                            },
                        ]
                    },
                }
            }
        }
    ),
}


@pytest.fixture()
def model_url() -> Dict[str, Any]:
    return {'model': PaginatedScanClickHouseQuery(**_base_query.dict()), 'url': 'events.scan_query'}


@pytest.fixture()
def fake_clickhouse() -> Any:
    ch = mock.MagicMock()
    ch.datasource = 'osprey.execution_results'
    ch.backend = mock.MagicMock()

    return ch


@pytest.fixture()
def mock_clickhouse_client(fake_clickhouse: Any) -> Any:
    with mock.patch('osprey.worker.ui_api.osprey.singletons.CLICKHOUSE') as magic_mock:
        magic_mock.instance = mock.MagicMock(return_value=fake_clickhouse)
        yield


@pytest.mark.parametrize(
    'model,url',
    [
        (TopNClickHouseQuery(dimension='fake', **_base_query.dict()), 'events.topn_query'),
        (TimeseriesClickHouseQuery(granularity='fake', **_base_query.dict()), 'events.timeseries_query'),
        (PaginatedScanClickHouseQuery(**_base_query.dict()), 'events.scan_query'),
        (TopNClickHouseQuery(dimension='fake', **_base_query.dict()), 'events.topn_query_csv'),
        (
            BulkLabelTopNRequest(
                dimension='fake',
                expected_entities=1,
                no_limit=False,
                label_name='',
                label_status='',
                label_reason='',
                **_base_query.dict(),
            ),
            'events.topn_bulk_label',
        ),
    ],
)
def test_events_auth_reject_post(app: Flask, client: 'FlaskClient[Response]', model: BaseClickHouseQuery, url: str) -> None:
    res = client.post(url_for(url), content_type='application/json', data=model.json())
    assert res.status_code == 401, res.data


@pytest.mark.parametrize(
    'url,url_kwargs',
    [('events.get_event_data', {'event_id': 1})],
)
def test_events_auth_reject_get(
    app: Flask, client: 'FlaskClient[Response]', url: str, url_kwargs: Mapping['str', Any]
) -> None:
    res = client.get(url_for(url, **url_kwargs))
    assert res.status_code == 401, res.data


@pytest.mark.use_rules_sources(config_a)
def test_events_scan_request_missing_ability(
    app: Flask, client: 'FlaskClient[Response]', model_url: Dict[str, Any]
) -> None:
    res = client.post(url_for(model_url['url']), content_type='application/json', data=model_url['model'].json())
    assert res.status_code == 401
    assert res.data.decode('utf-8') == "User `local-dev@localhost` doesn't have ability `CAN_VIEW_EVENTS_BY_ACTION`"


# TODO: get ClickHouse local running again
@pytest.mark.use_rules_sources(config_b)
def test_events_scan_request(
    app: Flask,
    client: 'FlaskClient[Response]',
    model_url: Dict[str, Any],
    mock_clickhouse_client: Any,
    fake_clickhouse: Any,
) -> None:
    fake_clickhouse.backend.execute_query.return_value = []

    res = client.post(url_for(model_url['url']), content_type='application/json', data=model_url['model'].json())
    assert res.status_code == 200
