"""Retry only transient failures; never repeat authentication or input errors."""
import random
import time

import requests

from app.config import settings

TRANSIENT = {429, 500, 502, 503, 504}


class DeadlineExceeded(RuntimeError):
    """任务级总时限耗尽；继续重试只会消耗预算而不可能成功。"""


def remaining_seconds(deadline):
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DeadlineExceeded("TASK_DEADLINE_EXCEEDED: no execution budget remains")
    return remaining


def post_json(path, payload, deadline=None):
    if not settings.QWEN_API_KEY:
        raise RuntimeError("真实模型模式需要QWEN_API_KEY")
    if not settings.ALLOW_PAID_MODEL_CALLS:
        raise RuntimeError("付费模型调用未启用，请显式设置ALLOW_PAID_MODEL_CALLS=true")
    url = settings.QWEN_BASE_URL.rstrip("/") + path
    last = None
    for attempt in range(settings.LLM_MAX_RETRIES + 1):
        if deadline is not None and time.monotonic() >= deadline:
            raise DeadlineExceeded(f"任务总时限已耗尽，取消对{path}的调用与重试")
        remaining = remaining_seconds(deadline)
        # 单次请求读超时受剩余时限约束，避免一次长调用吞掉整个任务的预算。
        read_timeout = settings.LLM_TIMEOUT_SECONDS if remaining is None else max(min(settings.LLM_TIMEOUT_SECONDS, remaining), 0.001)
        response = None
        try:
            response = requests.post(url, headers={"Authorization": f"Bearer {settings.QWEN_API_KEY}"},
                                     json=payload, timeout=(min(settings.CONNECT_TIMEOUT, remaining) if remaining else settings.CONNECT_TIMEOUT, read_timeout))
            remaining_seconds(deadline)
            if response.status_code not in TRANSIENT:
                if response.status_code >= 400:
                    raise RuntimeError(f"模型HTTP {response.status_code}，请检查鉴权、参数与地域配置")
                return response.json()
            last = f"模型HTTP {response.status_code}"
        except (requests.ConnectionError, requests.Timeout) as exc:
            last = type(exc).__name__
        except requests.RequestException as exc:
            raise RuntimeError(f"模型请求失败: {type(exc).__name__}") from exc
        if attempt < settings.LLM_MAX_RETRIES:
            retry_after = response.headers.get("Retry-After", "") if response is not None else ""
            delay = min(float(retry_after), 15) if retry_after.isdigit() else min(2 ** attempt, 8) + random.random() / 4
            remaining = remaining_seconds(deadline)
            if remaining is not None:
                # 退避等待同样不能越过任务总时限。
                delay = min(delay, max(remaining, 0.001))
            time.sleep(delay)
    raise RuntimeError(f"模型调用暂态失败，重试已耗尽: {last}")
