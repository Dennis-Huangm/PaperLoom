from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

from openai import OpenAI

from .config import LLMConfig
from .models import Paper
from .utils import env, extract_json_object


class LLMClient:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        api_key = env(config.api_key_env)
        base_url = env(config.base_url_env)
        self.enabled = bool(api_key or base_url)
        self.client = OpenAI(api_key=api_key or "ollama", base_url=base_url) if self.enabled else None

    def chat(self, system: str, user: str, json_mode: bool = False) -> str:
        if not self.client:
            raise RuntimeError(
                f"LLM 未配置：请设置 {self.config.api_key_env}，或同时配置本地 OpenAI-compatible base URL。"
            )
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.config.temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            response = self.client.chat.completions.create(**kwargs)
        except Exception:
            retry_kwargs = dict(kwargs)
            retry_kwargs.pop("temperature", None)
            if json_mode:
                retry_kwargs.pop("response_format", None)
            if retry_kwargs == kwargs:
                raise
            response = self.client.chat.completions.create(**retry_kwargs)
        return response.choices[0].message.content or ""

    def rerank(self, papers: list[Paper], interest_description: str) -> None:
        if not self.enabled or not papers:
            return
        candidates = "\n\n".join(
            f"ID: {paper.arxiv_id}\n标题: {paper.title}\n类别: {', '.join(paper.categories)}\n摘要: {paper.abstract[:1800]}"
            for paper in papers
        )
        prompt = f"""研究兴趣：
{interest_description}

候选论文：
{candidates}

请判断每篇论文对该研究者的阅读价值。只返回 JSON 对象：
{{"papers":[{{"id":"arxiv id","score":0到10的数字,"reason":"不超过40字的具体理由"}}]}}
评分应综合主题相关性、方法新颖性、潜在影响和是否值得立即阅读全文；不要根据标题夸大贡献。
严格校准：8-10 分只给同时直接研究 Agent/LLM/VLM 工作流与 T2I/图像生成编辑的论文；
6-7 分给机制上可直接迁移的相邻工作；只涉及通用 Agent 或只涉及普通图像生成时不得高于 5 分。
"""
        raw = self.chat("你是严谨的 AI 研究文献筛选助手。", prompt, json_mode=True)
        payload = extract_json_object(raw)
        by_id = {paper.arxiv_id: paper for paper in papers}
        for item in payload.get("papers", []):
            paper = by_id.get(str(item.get("id", "")))
            if not paper:
                continue
            paper.llm_score = max(0.0, min(10.0, float(item.get("score", 0))))
            paper.recommendation_reason = str(item.get("reason", "")).strip()

    def describe_figure(
        self,
        image_path: Path,
        paper_title: str,
        paper_abstract: str,
        caption: str,
    ) -> str:
        if not self.client:
            return ""
        mime_type = mimetypes.guess_type(image_path.name)[0] or "image/png"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        prompt = f"""论文标题：{paper_title}
论文摘要：{paper_abstract[:1800]}
作者原始图注（仅作为事实线索，不得翻译或照抄）：{caption}

请直接观察图片，用通俗中文解释这张图。说明：
1. 图从左到右或从上到下展示了什么；
2. 关键模块、箭头、输入输出或对比关系分别代表什么；
3. 读者应从图中抓住的核心方法思想是什么。

只输出 3-5 句连贯说明，不要标题，不要逐字复述图注，不要声称看到了无法确认的细节。"""
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是严谨的论文图示讲解助手，优先依据图片本身，并用面向研究者的通俗中文解释。",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                        },
                    ],
                },
            ],
            "temperature": self.config.temperature,
        }
        try:
            response = self.client.chat.completions.create(**kwargs)
        except Exception:
            kwargs.pop("temperature", None)
            response = self.client.chat.completions.create(**kwargs)
        return (response.choices[0].message.content or "").strip()
