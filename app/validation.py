import json
import math

from pydantic import ValidationError

from app.schemas import ModelDraft


def check_draft(value, evidence_ids):
    errors, warnings = [], []
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, OverflowError):
        return None, ["JSON_INVALID: 包含非有限数值或不可序列化内容"], []
    try:
        model = ModelDraft.model_validate(value)
    except ValidationError as exc:
        return None, [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()], []
    draft = model.model_dump()
    elements = {e["localId"]: e for e in draft["model_elements"]}
    if len(elements) != len(draft["model_elements"]):
        errors.append("LOCAL_ID_DUPLICATE: 元素localId重复")
    names = set()
    allowed = set(evidence_ids)
    for element in draft["model_elements"]:
        key = (element["namespace"], element["kind"], element["name"])
        if key in names:
            errors.append(f"NAME_CONFLICT: {element['name']}在同一命名空间和类型下重复")
        names.add(key)
        refs = element["evidence_chunk_ids"]
        if not refs:
            errors.append(f"EVIDENCE_MISSING: {element['localId']}缺少依据")
        if not set(refs) <= allowed:
            errors.append(f"EVIDENCE_UNKNOWN: {element['localId']}引用未授权或未检索的片段")
        props = element["properties"]
        if "value" in props and isinstance(props["value"], (int, float)) and not math.isfinite(props["value"]):
            errors.append("VALUE_INVALID: 非有限数值")
    if not set(draft["evidence"]) <= allowed:
        errors.append("EVIDENCE_UNKNOWN: 总体来源列表不合法")
    if not {ref for e in draft["model_elements"] for ref in e["evidence_chunk_ids"]} <= set(draft["evidence"]):
        errors.append("EVIDENCE_INCOMPLETE: 总体来源未覆盖全部元素")
    graph, owners, seen = {}, {}, set()
    for relation in draft["sysml_relationships"]:
        src, tgt, kind = relation["source"], relation["target"], relation["relType"]
        if src not in elements or tgt not in elements:
            errors.append(f"ENDPOINT_MISSING: {src} -> {tgt}")
            continue
        if src == tgt:
            errors.append(f"SELF_REFERENCE: {src}")
        key = (src, kind, tgt)
        if key in seen:
            errors.append("RELATION_DUPLICATE: 重复关系")
        seen.add(key)
        if kind == "CONTAINS":
            if tgt in owners and owners[tgt] != src:
                errors.append(f"MULTIPLE_OWNERS: {tgt}")
            owners[tgt] = src
            graph.setdefault(src, []).append(tgt)
        if kind in {"DERIVES_FROM", "REFINES"} and elements[tgt]["kind"] != "Requirement":
            errors.append("RELATION_TYPE_MISMATCH: 派生或精化的目标必须是需求")
        if kind == "SATISFIES" and (elements[tgt]["kind"] != "Requirement" or elements[src]["kind"] == "Requirement"):
            errors.append("RELATION_TYPE_MISMATCH: 满足关系需从设计元素指向需求")
        if kind == "ALLOCATES" and (elements[src]["kind"] not in {"Action", "ActionType"} or elements[tgt]["kind"] not in {"Part", "PartType"}):
            errors.append("RELATION_TYPE_MISMATCH: 分配关系需从行为指向部件")
    visiting, visited = set(), set()

    def visit(node):
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(visit(child) for child in graph.get(node, [])):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    if any(visit(node) for node in elements):
        errors.append("OWNERSHIP_CYCLE: 包含关系形成循环")
    if draft["open_questions"]:
        warnings.append("存在待澄清问题，确认前必须修改并重新校验。")
    warnings.append("内部JSON模型草稿，不是经工具验证的SysMLv2工程文件。")
    return draft, errors, warnings


def validate_llm_output(raw, evidence_ids=()):
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        return None, [f"JSON_INVALID: {exc}"], []
    return check_draft(data, evidence_ids)
