"""Deterministic calculation; never execute a model-generated expression."""
import math


def calculate_reliability(task_hours, components, threshold=0.9):
    if type(task_hours) not in (int, float) or not math.isfinite(task_hours) or task_hours < 0:
        raise ValueError("任务时间必须是非负有限小时数")
    if not components or type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("组件或可靠度阈值不合法")
    rows = []
    for component in components:
        mtbf = component["mtbf_hours"]
        if type(mtbf) not in (int, float) or not math.isfinite(mtbf) or mtbf <= 0:
            raise ValueError("MTBF必须是正的有限小时数")
        rate = 1 / mtbf
        rows.append({**component, "failure_rate_per_hour": rate,
                     "reliability": math.exp(-rate * task_hours)})
    result = math.exp(-task_hours * sum(row["failure_rate_per_hour"] for row in rows))
    return {"task_hours": task_hours, "components": rows, "system_reliability": result,
            "threshold": threshold, "operator": ">", "passed": result > threshold,
            "assumptions": ["恒定失效率指数模型", "子系统独立且串联", "忽略案例刷柄和刷头结构件"],
            "note": "案例参数复算，不代表AI准确率、实物试验或通用可靠性结论。"}


def calculation_from_draft(draft):
    elements = draft.get("model_elements", [])
    task = [e for e in elements if e.get("properties", {}).get("parameter_role") == "task_hours"]
    mtbfs = [e for e in elements if e.get("properties", {}).get("parameter_role") == "mtbf"]
    limit = [e for e in elements if e.get("properties", {}).get("parameter_role") == "reliability_threshold"]
    if not task and not mtbfs and not limit:
        return None
    if len(task) != 1 or len(mtbfs) != 5 or len(limit) != 1:
        raise ValueError("本案例计算需要1个任务时间、5个子系统MTBF和1个阈值")
    if any(e["properties"].get("unit") != "h" for e in task + mtbfs):
        raise ValueError("可靠性计算需要统一使用小时h")
    if limit[0]["properties"].get("operator") != ">":
        raise ValueError("本案例可靠度要求为严格大于")
    return calculate_reliability(task[0]["properties"]["value"],
        [{"name": e["properties"]["component"], "mtbf_hours": e["properties"]["value"],
          "evidence_chunk_ids": e["evidence_chunk_ids"]} for e in mtbfs],
        limit[0]["properties"]["value"])
