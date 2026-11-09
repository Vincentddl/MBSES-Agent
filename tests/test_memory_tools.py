import copy
import json
import time
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import event

from app.agent import run_tools
from app.database import SessionLocal, engine
from app.models import Conversation, ConversationTurn, GenerationTask
from app.services import create_task, requeue_expired_tasks, run_generation
from app.validation import check_draft
from test_workflow import upload


def wait_task(client, task_id):
    for _ in range(150):
        task = client.get('/api/tasks/' + task_id).json()
        if task['status'] not in {'PENDING', 'RUNNING'}:
            return task
        time.sleep(.02)
    raise TimeoutError(task_id)


def turn(client, project, conversation, revision, text, key):
    return client.post('/api/generate', json={'project_id': project, 'input_text': text,
        'conversation_id': conversation, 'expected_conversation_revision': revision,
        'idempotency_key': key})


def test_multi_turn_retains_draft_and_requires_human_confirmation(client, project):
    upload(client, project)
    cid = client.post('/api/conversations', json={'project_id': project}).json()['id']
    first = turn(client, project, cid, 0, '提取自动刷牙、更换刷头、质量要求和防水需求', 'turn-one')
    assert first.status_code == 202, first.text
    a = wait_task(client, first.json()['task_id'])
    assert a['status'] == 'READY_FOR_REVIEW', a
    second = turn(client, project, cid, 1, '将质量要求改为不超过180克，其他保持不变', 'turn-two')
    assert second.status_code == 202, second.text
    b = wait_task(client, second.json()['task_id'])
    assert b['status'] == 'READY_FOR_REVIEW', b
    aa, bb = a['result_json'], b['result_json']
    assert len(aa['model_elements']) == len(bb['model_elements'])
    assert aa['sysml_relationships'] == bb['sysml_relationships']
    assert next(e for e in bb['model_elements'] if e['localId'] == 'mass')['properties']['value'] == 180
    assert client.get('/api/elements/' + project).json()['elements'] == []
    usage = client.get('/api/tasks/' + b['id'] + '/usage').json()
    assert any(u['stage'] == 'tool_call' and u['detail']['name'] == 'read_previous_draft' for u in usage)
    assert client.post('/api/tasks/' + b['id'] + '/validate', json={'draft': bb, 'expected_revision': 1}).json()['valid']
    assert client.post('/api/tasks/' + b['id'] + '/confirm', json={'expected_revision': 2}).status_code == 200
    conversation = client.get('/api/conversations/' + cid).json()
    assert conversation['revision'] == 2 and len(conversation['turns']) == 2
    # A transport retry may refresh the conversation revision but must not create another turn.
    retry = turn(client, project, cid, 2, '将质量要求改为不超过180克，其他保持不变', 'turn-two')
    assert retry.json()['task_id'] == b['id'] and retry.json()['deduplicated']
    assert client.get('/api/conversations/' + cid).json()['revision'] == 2


def test_memory_project_isolation_revision_and_inflight_guard(client, project):
    cid = client.post('/api/conversations', json={'project_id': project}).json()['id']
    other = client.post('/api/projects', json={'name': 'other'}).json()['id']
    assert turn(client, other, cid, 0, '提取需求', 'cross-project').status_code == 404
    assert turn(client, project, cid, 9, '提取需求', 'stale').status_code == 409
    task_id, _, _ = create_task(project, '提取需求', 'inflight', conversation_id=cid, expected_conversation_revision=0)
    assert turn(client, project, cid, 1, '修改需求', 'inflight-two').status_code == 409
    with SessionLocal() as db:
        assert db.get(Conversation, cid).revision == 1
        assert db.query(ConversationTurn).filter_by(conversation_id=cid).count() == 1
    run_generation(task_id)


def test_memory_limit_rejects_without_losing_previous_state(client, project):
    cid = client.post('/api/conversations', json={'project_id': project}).json()['id']
    task_id, _, _ = create_task(project, 'first request', 'limit-one', conversation_id=cid, expected_conversation_revision=0)
    with SessionLocal.begin() as db:
        db.get(GenerationTask, task_id).status = 'NEEDS_INPUT'
        db.get(GenerationTask, task_id).result_json = {'large': 'x' * 61000}
    result = turn(client, project, cid, 1, '修改需求', 'limit-two')
    assert result.status_code == 422
    assert client.get('/api/conversations/' + cid).json()['revision'] == 1


def test_failed_followup_keeps_last_draft_snapshot(client, project):
    upload(client, project)
    cid = client.post('/api/conversations', json={'project_id': project}).json()['id']
    first = turn(client, project, cid, 0, '提取自动刷牙、更换刷头、质量要求和防水需求', 'recover-one')
    a = wait_task(client, first.json()['task_id'])
    failed_id, _, _ = create_task(project, '将质量要求改为不超过180克', 'recover-two',
        conversation_id=cid, expected_conversation_revision=1)
    with SessionLocal.begin() as db:
        db.get(GenerationTask, failed_id).status = 'FAILED'
    third_id, _, _ = create_task(project, '将质量要求改为不超过170克，其他不变', 'recover-three',
        conversation_id=cid, expected_conversation_revision=2)
    with SessionLocal() as db:
        assert db.get(ConversationTurn, third_id).context['previous_draft'] == a['result_json']
    run_generation(third_id)
    result = client.get('/api/tasks/' + third_id).json()
    assert result['status'] == 'READY_FOR_REVIEW', result
    assert next(e for e in result['result_json']['model_elements'] if e['localId'] == 'mass')['properties']['value'] == 170


def call(name, args='{}', ident='call-1'):
    return {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': args}}


@pytest.mark.parametrize('tool', [call('delete_project'), call('read_previous_draft', '{"project_id":"evil"}'),
                                 call('search_knowledge', '{"query":42}'), call('search_knowledge', 'not-json')])
def test_tools_reject_unknown_or_invalid_arguments(tool):
    client = Mock()
    client.select_tools.side_effect = [({'role': 'assistant', 'tool_calls': [tool]}, {}),
                                     ({'role': 'assistant', 'content': 'done'}, {})]
    record = Mock()
    chunks, results = run_tools(client, 'server-project', 'request', 0, [], {}, time.monotonic() + 5, record)
    assert not chunks and results[0]['ok'] is False


def test_tool_duplicate_and_step_budget():
    client = Mock()
    client.select_tools.return_value = ({'role': 'assistant', 'tool_calls': [call('read_previous_draft')]}, {})
    _, results = run_tools(client, 'p', 'q', 0, [], {}, time.monotonic() + 5, Mock())
    assert client.select_tools.call_count == 2
    assert results[0]['ok'] and not results[1]['ok'] and 'Duplicate' in results[1]['error']


def test_tool_planner_failure_degrades_to_workflow():
    client = Mock()
    client.select_tools.side_effect = RuntimeError('provider unavailable')
    chunks, results = run_tools(client, 'p', 'q', 0, [], {}, time.monotonic() + 5, Mock())
    assert chunks == [] and results[0]['fallback'] == 'fixed_rag_workflow'


def test_qwen_function_call_protocol_without_paid_api(monkeypatch):
    import app.llm_client as module
    post = Mock(return_value={'choices': [{'finish_reason': 'tool_calls', 'message': {
        'role': 'assistant', 'content': None, 'tool_calls': [call('read_previous_draft')]}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}})
    monkeypatch.setattr(module, 'post_json', post)
    from app.agent import TOOLS
    result, usage = module.QwenLLMClient().select_tools([{'role': 'user', 'content': 'test'}], TOOLS)
    assert result['tool_calls'][0]['function']['name'] == 'read_previous_draft'
    assert post.call_args.args[1]['tools'] == TOOLS and usage['total_tokens'] == 15


@pytest.mark.parametrize('text,value,valid', [('MTBF=12000h', 2000, False), ('质量200.5克', 200, False),
    ('温度-200度', 200, False), ('MTBF=2e3h', 2000, True), ('MTBF=12,000h', 12000, True)])
def test_numeric_evidence_matches_whole_values(draft, text, value, valid):
    d = copy.deepcopy(draft)
    d['model_elements'][1]['properties']['value'] = value
    _, errors, _ = check_draft(d, ['USER_INPUT'], {'USER_INPUT': text})
    assert (not any('FACT_UNSUPPORTED' in e for e in errors)) == valid


def test_reaper_does_not_overwrite_a_racing_heartbeat(client, project):
    task_id, _, _ = create_task(project, 'lease race', 'race')
    with SessionLocal.begin() as db:
        db.query(GenerationTask).filter_by(id=task_id).update({'status': 'RUNNING', 'lease_token': 'owner',
            'lease_until': datetime.utcnow() - timedelta(seconds=5)})
    fired = []
    def renew(conn, cursor, statement, parameters, context, executemany):
        if not fired and statement.startswith('UPDATE generation_task SET status='):
            fired.append(True)
            conn.exec_driver_sql('UPDATE generation_task SET lease_until=? WHERE id=?',
                (datetime.utcnow() + timedelta(seconds=600), task_id))
    event.listen(engine, 'before_cursor_execute', renew)
    try:
        assert task_id not in requeue_expired_tasks()
    finally:
        event.remove(engine, 'before_cursor_execute', renew)
    assert fired
    with SessionLocal() as db:
        assert db.get(GenerationTask, task_id).status == 'RUNNING'
        db.get(GenerationTask, task_id).status = 'FAILED'
        db.commit()


def test_pending_handoff_is_retried_after_a_submission_failure(client, project, monkeypatch):
    import app.services as module
    task_id, _, _ = create_task(project, 'recover pending', 'pending-handoff')
    submitted = []
    def failing_submit(value):
        submitted.append(value)
        raise module.BusinessError('executor unavailable', 503)
    monkeypatch.setattr(module, 'submit_generation', failing_submit)
    module.recover_pending_tasks()
    assert task_id in submitted
    submitted.clear()
    monkeypatch.setattr(module, 'submit_generation', lambda value: submitted.append(value))
    module.recover_pending_tasks()
    assert task_id in submitted
    with SessionLocal.begin() as db:
        db.get(GenerationTask, task_id).status = 'FAILED'


def test_periodic_submission_does_not_duplicate_executor_queue(client, project, monkeypatch):
    import app.services as module
    from concurrent.futures import Future
    future = Future()
    executor = Mock()
    executor.submit.return_value = future
    monkeypatch.setattr(module, '_executor', executor)
    task_id, _, _ = create_task(project, 'deduplicate submission', 'submission-dedup')
    module.submit_generation(task_id)
    module.submit_generation(task_id)
    assert executor.submit.call_count == 1
    future.set_result(None)
    module.submit_generation(task_id)
    assert executor.submit.call_count == 2
    with SessionLocal.begin() as db:
        db.get(GenerationTask, task_id).status = 'FAILED'


def test_recovery_cannot_reset_expired_execution_budget(client, project, monkeypatch):
    import app.services as module
    task_id, _, _ = create_task(project, 'expired recovered task', 'persist-deadline')
    expired = datetime.utcnow() - timedelta(seconds=2)
    with SessionLocal.begin() as db:
        db.get(GenerationTask, task_id).execution_deadline_at = expired
    model = Mock()
    monkeypatch.setattr(module, 'get_llm_client', model)
    run_generation(task_id)
    model.assert_not_called()
    with SessionLocal() as db:
        row = db.get(GenerationTask, task_id)
        assert row.status == 'FAILED' and 'TASK_DEADLINE_EXCEEDED' in row.error
        assert row.execution_deadline_at == expired


def test_reaper_fails_expired_budget_instead_of_replaying(client, project):
    task_id, _, _ = create_task(project, 'expired budget', 'reaper-deadline')
    with SessionLocal.begin() as db:
        row = db.get(GenerationTask, task_id)
        row.status, row.lease_token = 'RUNNING', 'expired-owner'
        row.lease_until = datetime.utcnow() - timedelta(seconds=1)
        row.execution_deadline_at = datetime.utcnow() - timedelta(seconds=2)
    assert task_id not in requeue_expired_tasks()
    with SessionLocal() as db:
        assert db.get(GenerationTask, task_id).status == 'FAILED'


def test_mysql_upgrade_preflight_does_not_change_schema(monkeypatch):
    import app.database as module
    inspector = Mock()
    inspector.get_table_names.return_value = ['generation_task']
    inspector.get_columns.return_value = [{'name': 'lease_token'}]
    monkeypatch.setattr(module.settings, 'DATABASE_URL', 'mysql+pymysql://unused')
    monkeypatch.setattr(module, 'inspect', lambda _: inspector)
    ddl = Mock()
    monkeypatch.setattr(module.Base.metadata, 'create_all', ddl)
    with pytest.raises(RuntimeError, match='migrate_runtime'):
        module.create_all()
    ddl.assert_not_called()


def test_mysql_empty_database_can_bootstrap(monkeypatch):
    import app.database as module
    inspector = Mock()
    inspector.get_table_names.return_value = []
    monkeypatch.setattr(module.settings, 'DATABASE_URL', 'mysql+pymysql://unused')
    monkeypatch.setattr(module, 'inspect', lambda _: inspector)
    ddl = Mock()
    monkeypatch.setattr(module.Base.metadata, 'create_all', ddl)
    module.create_all()
    ddl.assert_called_once_with(module.engine)


def test_tool_search_respects_configured_context_size(monkeypatch):
    import app.agent as module
    monkeypatch.setattr(module.settings, 'MAX_CONTEXT_CHARS', 5)
    existing = {'chunk_id': 'old', 'content': 'abc', 'section_title': 'old'}
    found = {'chunk_id': 'new', 'content': 'def', 'section_title': 'new'}
    monkeypatch.setattr(module, 'retrieve', lambda *args, **kwargs: ([found], False))
    client = Mock()
    client.select_tools.side_effect = [({'role': 'assistant', 'tool_calls': [
        call('search_knowledge', '{"query":"find evidence"}')]}, {}),
        ({'role': 'assistant', 'content': 'done'}, {})]
    chunks, results = run_tools(client, 'p', 'q', 0, [existing], {}, time.monotonic() + 5, Mock())
    assert chunks == [existing] and results[0]['ok']
    assert sum(len(c['content']) for c in chunks) <= 5
