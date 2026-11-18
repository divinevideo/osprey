"""Verify the execution-result store against the integration stack's S3 service."""

from datetime import datetime, timezone
from uuid import uuid4

from minio import Minio
from osprey.worker.lib.singletons import CONFIG
from osprey.worker.lib.storage.stored_execution_result import StoredExecutionResultMinIO


def test_execution_results_round_trip_through_minio() -> None:
    config = CONFIG.instance()
    endpoint = config.get_str('OSPREY_MINIO_ENDPOINT', 'minio:9000')
    access_key = config.get_str('OSPREY_MINIO_ACCESS_KEY', 'minioadmin')
    secret_key = config.get_str('OSPREY_MINIO_SECRET_KEY', 'minioadmin123')
    secure = config.get_bool('OSPREY_MINIO_SECURE', False)
    client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
    bucket = f'osprey-test-{uuid4().hex}'
    client.make_bucket(bucket)

    try:
        store = StoredExecutionResultMinIO(endpoint, access_key, secret_key, secure, bucket)
        action_id = 1 << 42
        timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert store.select_one(action_id) is None

        store.insert(action_id, '{"score": 0.25}', '[]', timestamp, '{"type": "synthetic"}')
        expected = {
            'id': action_id,
            'extracted_features': '{"score": 0.25}',
            'error_traces': '[]',
            'timestamp': timestamp,
            'action_data': '{"type": "synthetic"}',
        }
        assert store.select_one(action_id) == expected
        assert store.select_many([action_id, action_id + 1]) == [expected]
    finally:
        for item in client.list_objects(bucket, recursive=True):
            client.remove_object(bucket, item.object_name)
        client.remove_bucket(bucket)
