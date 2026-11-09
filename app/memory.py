"""Durable, bounded conversation state. No automatic summary or inferred facts."""
import json

from app.models import Conversation, ConversationTurn, GenerationTask

MAX_MEMORY_CHARS = 60000
MAX_SOURCE_CHARS = 12000


def reserve_turn(db, project_id, conversation_id, revision, task_id, query):
    from app.services import BusinessError

    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.project_id != project_id:
        raise BusinessError("会话不属于当前项目", 404)
    if revision is None or revision != conversation.revision:
        raise BusinessError("会话版本冲突，请刷新会话", 409)
    previous = db.query(ConversationTurn).filter_by(conversation_id=conversation_id).order_by(
        ConversationTurn.turn_index.desc()).first()
    context = {"history": [], "previous_draft": None, "previous_chunks": [], "source_input": query}
    if previous:
        task = db.get(GenerationTask, previous.task_id)
        if task.status in {"PENDING", "RUNNING", "COMMITTING"}:
            raise BusinessError("请等待上一轮任务完成", 409)
        baseline = task.result_json or previous.context.get("previous_draft")
        if baseline:
            context = {
                "history": (previous.context.get("history", []) + [{"user": task.input_text,
                    "task_id": task.id, "status": task.status, "revision": task.revision}])[-4:],
                "previous_draft": baseline,
                "previous_chunks": task.retrieved_chunks if task.result_json else previous.context.get("previous_chunks", []),
                "source_input": previous.context.get("source_input", task.input_text) + "\n" + query,
            }
    # Never silently truncate a model, formula or authoritative user constraint.
    if len(context["source_input"]) > MAX_SOURCE_CHARS or len(json.dumps(context, ensure_ascii=False)) > MAX_MEMORY_CHARS:
        raise BusinessError("会话上下文达到上限，请新建会话并明确当前需求；不会自动摘要或丢弃约束", 422)
    changed = db.query(Conversation).filter_by(id=conversation_id, revision=revision).update(
        {"revision": revision + 1}, synchronize_session=False)
    if changed != 1:
        raise BusinessError("会话正在被其他请求修改，请刷新", 409)
    db.add(ConversationTurn(task_id=task_id, conversation_id=conversation_id,
                            turn_index=revision + 1, context=context))
    return context


def load_context(db, task_id):
    turn = db.get(ConversationTurn, task_id)
    return turn.context if turn else {}


def merge_chunks(previous, current, limit=12000):
    result, seen, size = [], set(), 0
    # Old evidence stays available for unchanged elements in the previous draft.
    for chunk in previous + current:
        if chunk["chunk_id"] in seen:
            continue
        if size + len(chunk["content"]) > limit:
            if chunk in previous:
                raise ValueError("Previous evidence exceeds context budget")
            continue
        result.append(chunk)
        seen.add(chunk["chunk_id"])
        size += len(chunk["content"])
    return result
