"""Named model connections; only non-secret metadata belongs in config.yaml."""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import yaml

from ...config import LLMConfig
from .model_connection import connection_values
from .settings import save_config_settings, update_dotenv


class ModelPresets:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.env_path = config_path.parent / ".env"

    def _entries(self) -> list[dict[str, str]]:
        return (yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}).get("model_presets", [])

    @staticmethod
    def _env_names(preset_id: str) -> tuple[str, str]:
        prefix = f"PAPERLOOM_MODEL_{preset_id.upper()}"
        return f"{prefix}_API_KEY", f"{prefix}_BASE_URL"

    def _values(self, entry: dict[str, str]) -> tuple[str, str, str]:
        key_env, url_env = self._env_names(entry["id"])
        if key_env not in os.environ or url_env not in os.environ:
            raise ValueError("此配置的凭据缺失，请重新填写后另存，或恢复包含 .env 的备份。")
        return entry["model"], os.environ[key_env], os.environ[url_env]

    def list(self, config: LLMConfig) -> list[dict]:
        current = (config.model, os.getenv(config.api_key_env, ""), os.getenv(config.base_url_env, ""))
        result = []
        for entry in self._entries():
            available = all(name in os.environ for name in self._env_names(entry["id"]))
            result.append({**entry, "available": available,
                           "active": available and self._values(entry) == current})
        return result

    def save(self, name: str, values: tuple[str, str, str]) -> str:
        name = name.strip()
        if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise ValueError("请填写 1–80 个字符的配置名称，不包含控制字符。")
        entries = self._entries()
        if any(entry["name"].casefold() == name.casefold() for entry in entries):
            raise ValueError("已存在同名模型配置，请使用其他名称。")
        preset_id = uuid.uuid4().hex
        model, key, url = values
        key_env, url_env = self._env_names(preset_id)
        # Persist empty values too: an official endpoint or keyless local model
        # must never inherit the credentials of the previously active service.
        update_dotenv(self.env_path, {key_env: key, url_env: url})
        save_config_settings(self.config_path, {"model_presets": [*entries, {
            "id": preset_id, "name": name, "model": model,
        }]})
        return preset_id

    def activate(self, preset_id: str, config: LLMConfig) -> None:
        entry = self._find(preset_id)
        model, key, url = self._values(entry)
        # Reuse the connection form's validation before changing any live values.
        connection_values({"llm_model": model, "llm_api_key": key, "llm_base_url": url,
                           **({"clear_llm_api_key": "true"} if not key else {}),
                           **({"clear_llm_base_url": "true"} if not url else {})}, config)
        # Keep empty snapshot values as availability markers, but remove empty
        # live credentials so the model client can use its default endpoint.
        live_values = {config.api_key_env: key, config.base_url_env: url}
        update_dotenv(self.env_path, {name: value for name, value in live_values.items() if value},
                      {name for name, value in live_values.items() if not value})
        save_config_settings(self.config_path, {"llm": {"model": model}})

    def _find(self, preset_id: str) -> dict[str, str]:
        entry = next((entry for entry in self._entries() if entry["id"] == preset_id), None)
        if entry is None:
            raise ValueError("模型配置不存在，请刷新页面后重试。")
        return entry

    def delete(self, preset_id: str) -> None:
        self._find(preset_id)
        save_config_settings(self.config_path, {"model_presets": [
            entry for entry in self._entries() if entry["id"] != preset_id
        ]})
        update_dotenv(self.env_path, {}, set(self._env_names(preset_id)))
