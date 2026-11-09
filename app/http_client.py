"""Retry only transient failures; never repeat authentication or input errors."""
import random
import time

import requests

from app.config import settings

TRANSIENT = {429, 500, 502, 503, 504}


def post_json(path, payload):
    if not settings.QWEN_API_KEY:
        raise RuntimeError("真实模型模式需要QWEN_API_KEY")
    if not settings.ALLOW_PAID_MODEL_CALLS:
        raise RuntimeError("付费模型调用未启用，请显式设置ALLOW_PAID_MODEL_CALLS=true")
    url = settings.QWEN_BASE_URL.rstrip("/") + path
    last = None
    for attempt in range(settings.LLM_MAX_RETRIES + 1):
        response = None
        try:
            response = requests.post(url, headers={"Authorization": f"Bearer {settings.QWEN_API_KEY}"},
                                     json=payload, timeout=(settings.CONNECT_TIMEOUT, settings.LLM_TIMEOUT_SECONDS))
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
            time.sleep(delay)
    raise RuntimeError(f"模型调用暂态失败，重试已耗尽: {last}")
