"""Regressions for decision-contract review: reuse must remain trustworthy."""
from unittest.mock import MagicMock

import pytest

import ncp
from ncp.config import load_config
from ncp.mcp.server import make_handlers
from ncp.stores.sqlite import SQLiteStore
from ncp.types import DecisionRecord, OutcomeRecord, SubconsciousChunk

SCHEMA = 'ncp.slot.continue_or_escalate'
ARGS = dict(agent_id='a', task='retry', slot='retry', intent='review',
            pipeline_id='p', schema_id=SCHEMA)


@pytest.fixture
def setup(tmp_path):
    config = load_config(cwd=tmp_path, env={})
    store = SQLiteStore(tmp_path / 'db')
    store.write(SubconsciousChunk(content='retry policy smoke tests passed',
                                 layer='semantic', src='tool_result', pipeline_id='p'))
    return store, config


def record(handlers, **overrides):
    packet = handlers['ncp_compile_decision_query'](ARGS)
    args = dict(decision='continue', rationale='r', agent_id='a', schema_id=SCHEMA,
                slot='retry', choice='continue', confidence=0.9,
                state_hash=packet['state_hash'], pipeline_id='p')
    args.update(overrides)
    return handlers['ncp_record_decision'](args)


def test_zero_confidence_is_not_promoted_to_reusable(setup):
    store, config = setup
    h = make_handlers(store, config=config)
    result = record(h, confidence=0.0)
    assert store.get_decision(result['decision_id']).confidence == 0.0
    assert 'suggested_choice' not in h['ncp_compile_decision_query'](ARGS)


def test_old_failed_outcome_still_blocks_reuse(setup):
    store, config = setup
    h = make_handlers(store, config=config)
    result = record(h)
    h['ncp_record_outcome'](dict(success=False, chunk_ids=['e'],
                                 decision_id=result['decision_id']))
    for i in range(201):
        store.record_outcome(OutcomeRecord(chunk_ids=['unrelated'], success=True))
    assert 'suggested_choice' not in h['ncp_compile_decision_query'](ARGS)


def test_unresolved_linked_outcome_is_not_assumed_safe(setup):
    store, config = setup
    h = make_handlers(store, config=config)
    result = record(h)
    store.link_decision_outcome(result['decision_id'], 'missing')
    assert 'suggested_choice' not in h['ncp_compile_decision_query'](ARGS)


@pytest.mark.parametrize('schema,choice,strict', [
    (SCHEMA, 'teleport', False), ('unregistered', 'continue', True),
])
def test_python_api_rejects_invalid_contract(setup, schema, choice, strict):
    store, config = setup
    config.values['decisions']['strict_registered_schemas'] = strict
    decision = DecisionRecord(schema_id=schema, slot='retry', choice=choice, confidence=0.9)
    with pytest.raises(ValueError):
        ncp.record_decision(decision, store=store, config=config)
    assert store.get_decision(decision.decision_id) is None


def test_python_api_respects_disabled_decisions(setup):
    store, config = setup
    config.values['decisions']['enabled'] = False
    decision = DecisionRecord(schema_id=SCHEMA, slot='retry', choice='continue', confidence=0.9)
    assert ncp.record_decision(decision, store=store, config=config) is False
    assert store.get_decision(decision.decision_id) is None


def test_python_api_honors_dual_write_and_preserves_record_identity(setup):
    store, config = setup
    config.values['decisions']['dual_write_chunks'] = True
    decision = DecisionRecord(schema_id=SCHEMA, slot='retry', choice='continue', confidence=0.9)
    assert ncp.record_decision(decision, store=store, config=config)
    assert store.get_decision(decision.decision_id).canonical_json() == decision.canonical_json()
    assert len(store.query('', layer='reasoning_trace', fallback_to_trust_recency=True)) == 1


@pytest.mark.parametrize('new_version,new_options', [
    (2, ['continue', 'stop']), (1, ['escalate', 'stop']),
])
def test_schema_change_invalidates_precedent(setup, new_version, new_options):
    store, config = setup
    h = make_handlers(store, config=config)
    record(h)
    config.values['decision_schemas'][SCHEMA] = dict(
        version=new_version, choice_type='enum', options=new_options)
    updated = make_handlers(store, config=config)['ncp_compile_decision_query'](ARGS)
    assert 'suggested_choice' not in updated


def test_compile_with_configured_embedding_never_calls_provider(setup):
    store, config = setup
    provider = MagicMock()
    provider.embed.return_value = [0.1] * 1536
    store._embedding_adapter = provider
    result = make_handlers(store, config=config)['ncp_compile_decision_query'](ARGS)
    assert result['evidence_count'] == 1
    provider.embed.assert_not_called()


def test_old_consumed_success_still_supports_low_confidence_reuse(setup):
    store, config = setup
    h = make_handlers(store, config=config)
    result = record(h, confidence=0.2)
    linked = h['ncp_record_outcome'](dict(success=True, chunk_ids=['e'],
                                          decision_id=result['decision_id']))
    with store._connect() as connection:
        connection.execute('UPDATE outcomes SET consumed = 1 WHERE outcome_id = ?',
                           (linked['outcome_id'],))
    for _ in range(201):
        store.record_outcome(OutcomeRecord(chunk_ids=['unrelated'], success=False))
    packet = h['ncp_compile_decision_query'](ARGS)
    assert packet['suggested_choice'] == 'continue'
    assert packet['suggested_basis'] == 'outcome'


def test_current_schema_version_records_and_reuses(setup):
    store, config = setup
    config.values['decision_schemas'][SCHEMA] = dict(
        version=2, choice_type='enum', options=['continue', 'stop'])
    h = make_handlers(store, config=config)
    result = record(h)
    assert store.get_decision(result['decision_id']).schema_version == 2
    packet = h['ncp_compile_decision_query'](ARGS)
    assert packet['schema_version'] == 2
    assert packet['suggested_choice'] == 'continue'


def test_python_api_rejects_stale_version_without_writing(setup):
    store, config = setup
    config.values['decision_schemas'][SCHEMA] = dict(
        version=2, choice_type='enum', options=['continue', 'stop'])
    decision = DecisionRecord(schema_id=SCHEMA, slot='retry', choice='continue', confidence=0.9)
    with pytest.raises(ValueError, match='schema_version'):
        ncp.record_decision(decision, store=store, config=config)
    assert store.get_decision(decision.decision_id) is None


def test_regular_query_still_embeds_after_compile(setup):
    store, config = setup
    provider = MagicMock()
    provider.embed.return_value = [0.1] * 1536
    store._embedding_adapter = provider
    make_handlers(store, config=config)['ncp_compile_decision_query'](ARGS)
    store.query('retry', pipeline_id='p')
    provider.embed.assert_called_once_with('retry')
