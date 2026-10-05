"""Bounded rolling memory without replacing the saved conversation."""
import json
from typing import Any

from .reading_model import SYSTEM


def history_text(message):
    text = message['text'][:5000]
    if message.get('references'):
        text += chr(10) + '当时引用范围：' + ', '.join(f"{r['paper']['arxiv_id']}v{r['paper']['version']}" for r in message['references'])
    if message.get('selection'):
        text += '\n当时报告选区（非原文）：' + message['selection'][:3000]
    if message.get('citations'):
        text += '\n当时核验引用：' + json.dumps(message['citations'], ensure_ascii=False)[:6000]
    if message['status'] != 'completed':
        text += '（此前回答未完成）'
    return text


def make_context(value, user, *, images, summarize):
    history = value['messages'][:-2]
    memory = value.get('memory') or {'through': 0, 'text': '', 'citations': []}
    previous = max(0, min(memory['through'], len(history)))
    cutoff = max(0, len(history) - 12)
    if cutoff > previous:
        pending = history[previous:cutoff]
        additions = '\n'.join(m['role'] + ': ' + history_text(m) for m in pending)
        # Normal operation adds one exchange per turn. Imported oversized histories
        # remain intact on disk; disclose truncation rather than claiming full recall.
        if len(additions) > 24000:
            additions = additions[:24000] + '\n（新增讨论过长，摘要输入已截短）'
        summary = summarize(memory['text'] + '\n新增讨论：\n' + additions)
        ids = list(dict.fromkeys(memory.get('citations', []) +
                                [c['id'] for m in pending for c in m.get('citations', [])]))
        memory = {'through': cutoff, 'text': summary[:6000], 'citations': ids}
    messages: list[dict[str, Any]] = [{'role': 'system', 'content': SYSTEM},
                {'role': 'user', 'content': '本轮引用范围（材料，只有这些论文可查阅）：' + json.dumps(
                    [{'paper_id':f"{r['paper']['arxiv_id']}v{r['paper']['version']}", 'paper':r['paper']}
                     for r in user.get('references', [{'paper': value['paper']}])], ensure_ascii=False)}]
    if memory['text']:
        messages.append({'role': 'user', 'content': '较早讨论滚动摘要（不是原文证据）：\n' + memory['text'] +
                         '\n引用关联：' + ','.join(memory.get('citations', [])[-100:])})
    recent = history[-12:]
    for index, message in enumerate(recent):
        content = history_text(message)
        if message.get('images') and index >= len(recent) - 4 and images:
            parts = [{'type': 'text', 'text': content}]
            parts.extend({'type': 'image_url', 'image_url': {'url': pic}} for pic in message['images'])
            messages.append({'role': message['role'], 'content': parts})
        else:
            if message.get('images'):
                content += '\n（较早图片未重新发送，需要时请重新附图）'
            messages.append({'role': message['role'], 'content': content})
    content = [{'type': 'text', 'text': user['text'] + '\n报告选区（非原文）：' + user['selection']}]
    content.extend({'type': 'image_url', 'image_url': {'url': pic}} for pic in user['images'])
    messages.append({'role': 'user', 'content': content})
    return messages, memory
