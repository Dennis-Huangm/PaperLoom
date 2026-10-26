"""Small, non-persistent connection probe for the settings page."""
from __future__ import annotations

import os
import time
from urllib.parse import urlparse

from openai import OpenAI, APIConnectionError, APIStatusError, APITimeoutError


def connection_values(form, config):
    model = str(form.get("llm_model", config.model)).strip()
    if not model:
        raise ValueError("请填写模型名称。")
    values = {}
    for field, env_name in (("llm_api_key", config.api_key_env), ("llm_base_url", config.base_url_env)):
        value = str(form.get(field, ""))
        values[field] = "" if f"clear_{field}" in form else (value or os.getenv(env_name, ""))
    key = values["llm_api_key"].strip()
    url = values["llm_base_url"].strip().rstrip("/")
    if any(c in key + url + model for c in "\r\n\0"):
        raise ValueError("模型配置不能包含换行或空字符。")
    if url:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("API 地址应为 http:// 或 https:// 开头的服务地址，不包含账号、查询参数或片段。")
    return model, key, url


def probe_connection(model: str, key: str, url: str) -> dict:
    if not key and not url:
        return {"ok": False, "message": "请填写 API Key；本地模型服务可只填写 API 地址。"}
    started = time.monotonic()
    try:
        with OpenAI(api_key=key or "ollama", base_url=url or "https://api.openai.com/v1", timeout=15, max_retries=0) as client:
            result = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": "Reply with OK only."}],
            )
        if not result.choices or not (result.choices[0].message.content or "").strip():
            return {"ok": False, "message": "服务已响应，但模型返回空内容，请检查模型是否支持文本对话。"}
    except APITimeoutError:
        return {"ok": False, "message": "请求超时（15 秒），请检查网络或稍后重试。"}
    except APIConnectionError:
        return {"ok": False, "message": "无法连接服务，请检查 API 地址、网络、代理和证书设置。"}
    except APIStatusError as exc:
        messages = {401: "鉴权失败，请检查 API Key。", 403: "访问被拒绝，请检查密钥权限或服务商限制。",
                    404: "找不到接口或模型，请检查 API 地址（通常以 /v1 结尾）和模型名称。",
                    429: "服务限流或额度不足，请检查账户额度并稍后重试。"}
        return {"ok": False, "message": messages.get(exc.status_code, f"服务返回 HTTP {exc.status_code}，请检查模型名称和服务状态。")}
    except Exception:
        # Never return provider bodies or exception strings: they can contain credentials.
        return {"ok": False, "message": "服务响应无法解析，请确认地址支持 OpenAI 兼容的文本对话接口。"}
    return {"ok": True, "message": "连接成功，模型已返回有效回复。", "elapsed_ms": round((time.monotonic() - started) * 1000)}
