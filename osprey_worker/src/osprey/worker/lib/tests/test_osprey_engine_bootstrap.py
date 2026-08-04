from pathlib import Path
from unittest.mock import MagicMock

from osprey.worker.lib.osprey_engine import bootstrap_engine_with_helpers


def _patch_bootstrap_dependencies(
    mocker, sources_provider: object
) -> tuple[object, object, object, MagicMock, MagicMock]:
    udf_registry = object()
    udf_helpers = object()
    validation_exporter = object()

    mocker.patch('osprey.worker.adaptor.plugin_manager.bootstrap_udfs', return_value=(udf_registry, udf_helpers))
    mocker.patch('osprey.worker.adaptor.plugin_manager.bootstrap_ast_validators')
    mocker.patch('osprey.worker.lib.osprey_engine.should_yield_during_compilation', return_value=True)
    mocker.patch('osprey.worker.lib.osprey_engine.get_validation_result_exporter', return_value=validation_exporter)
    engine_constructor = mocker.patch('osprey.worker.lib.osprey_engine.OspreyEngine', return_value=object())
    get_sources_provider = mocker.patch(
        'osprey.worker.lib.osprey_engine.get_sources_provider', return_value=sources_provider
    )

    return udf_registry, udf_helpers, validation_exporter, engine_constructor, get_sources_provider


def test_bootstrap_engine_with_helpers_wires_validation_exporter(mocker) -> None:
    sources_provider = object()
    udf_registry, udf_helpers, validation_exporter, engine_constructor, _ = _patch_bootstrap_dependencies(
        mocker, sources_provider
    )

    engine, returned_udf_helpers = bootstrap_engine_with_helpers(sources_provider=sources_provider)

    assert returned_udf_helpers is udf_helpers
    engine_constructor.assert_called_once_with(
        sources_provider=sources_provider,
        udf_registry=udf_registry,
        should_yield_during_compilation=True,
        validation_exporter=validation_exporter,
    )
    assert engine is engine_constructor.return_value


def test_bootstrap_engine_with_helpers_honors_rules_path_and_signaler(mocker, monkeypatch) -> None:
    sources_provider = object()
    signaler = object()
    _, _, _, _, get_sources_provider = _patch_bootstrap_dependencies(mocker, sources_provider)
    monkeypatch.setenv('OSPREY_RULES_PATH', './example_rules')

    bootstrap_engine_with_helpers(input_stream_ready_signaler=signaler)

    get_sources_provider.assert_called_once_with(
        rules_path=Path('./example_rules'), input_stream_ready_signaler=signaler
    )
