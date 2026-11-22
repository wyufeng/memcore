"""记忆抽取：从对话/文档中提炼结构化记忆条目。

两级策略：
- L1 规则抽取：零依赖、离线可用，覆盖中英文常见"事实/偏好/决定/任务"句式
- L2 LLM 精炼：若配置了内网 LLM 网关，调用 OpenAI 兼容接口做高质量抽取

两级的输出统一为 dict 列表，便于后续写入 store。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence

# 中文/英文里表明显值得长期记住的信号词
_SIGNALS = [
    ("pref", ["我偏好", "我喜欢", "我不喜欢", "我讨厌", "我习惯", "请以后", "以后都",
              "以后请", "记住", "默认使用", "统一使用", "i prefer", "i like", "i don't like",
              "please always", "from now on", "going forward", "note that"]),
    ("decision", ["决定", "我们选择", "定为", "改成", "采用", "方案", "定为标准", "改成用",
                  "decided", "we will use", "let's use", "switch to", "chose", "chosen"]),
    ("env", ["环境是", "服务器", "地址是", "端口", "路径是", "版本是", "安装在", "配置在",
             "host is", "port is", "path is", "installed at", "endpoint"]),
    ("task", ["待办", "任务", "需要做", "下一步", "计划", "todo", "next step", "plan to"]),
    ("fact", ["是", "叫", "位于", "包含", "使用", "版本", "它", "定义"]),
]

_ROLE_PREFIX = re.compile(
    r"^\s*(user|assistant|human|ai|system|用户|助手|系统)\s*[:：]\s*", re.I)

_SENT_SPLIT = re.compile(r"(?<=[。！？!?\n；;])\s*|\n+")


def clean_text(text: str) -> str:
    t = _ROLE_PREFIX.sub("", text or "")
    return re.sub(r"[ \t]+", " ", t).strip()


def split_sentences(text: str) -> List[str]:
    parts = [clean_text(p) for p in _SENT_SPLIT.split(text or "")]
    return [p for p in parts if len(p) >= 6]


def guess_kind(sentence: str) -> str:
    low = sentence.lower()
    for kind, signals in _SIGNALS:
        for s in signals:
            if s.lower() in low:
                return kind
    return "fact"


def estimate_importance(sentence: str, kind: str) -> float:
    base = {"pref": 0.75, "decision": 0.85, "env": 0.7, "task": 0.6, "fact": 0.5}.get(kind, 0.5)
    # 含数字、路径、版本号、专有名词的信息更稳定
    if re.search(r"[/\.]|v\d|\d+", sentence):
        base += 0.05
    if len(sentence) > 120:
        base -= 0.05
    return max(0.1, min(1.0, base))


def rule_extract(text: str, max_items: int = 12) -> List[Dict[str, Any]]:
    """规则抽取：从对话文本中挑出候选记忆。"""
    out: List[Dict[str, Any]] = []
    seen = set()
    for sent in split_sentences(text):
        if len(sent) > 500:
            sent = sent[:500]
        key = sent.lower()[:80]
        if key in seen:
            continue
        kind = guess_kind(sent)
        # 只有 signal 命中的句子才保留，避免把闲聊全存进来
        if kind == "fact" and not re.search(r"[=:]|是|使用|位于|版本", sent):
            continue
        seen.add(key)
        out.append({
            "content": sent,
            "kind": kind,
            "importance": estimate_importance(sent, kind),
            "title": sent[:40],
        })
        if len(out) >= max_items:
            break
    return out


# ---------------- LLM 抽取 ----------------

_LLM_PROMPT = """你是长期记忆抽取器。从下面的对话中提取值得跨会话保留的记忆。

只提取稳定的、可复用的信息：用户偏好、已做出的技术决定、环境/路径/端口事实、待办事项。
不要提取寒暄、临时调试输出、一次性的报错堆栈。

严格输出 JSON 数组，每个元素：
{"content":"一句话记忆","kind":"fact|pref|decision|env|task","importance":0.0-1.0,"title":"不超过20字"}

没有可提取内容时输出 []。

对话：
%s
"""


def llm_extract(
    text: str,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 60.0,
    max_items: int = 12,
) -> List[Dict[str, Any]]:
    """调用内网 OpenAI 兼容网关做抽取；失败则返回空列表。"""
    if not (base_url and model):
        return []
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你只输出 JSON，不做解释。"},
            {"role": "user", "content": _LLM_PROMPT % text[:20000]},
        ],
        "temperature": 0.1,
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError):
        return []

    # 去掉可能的 markdown 围栏
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
        content = re.sub(r"```$", "", content).strip()
    try:
        items = json.loads(content)
    except ValueError:
        m = re.search(r"\[.*\]", content, re.S)
        if not m:
            return []
        try:
            items = json.loads(m.group(0))
        except ValueError:
            return []
    if not isinstance(items, list):
        return []

    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        content_s = str(it.get("content") or "").strip()
        if not content_s:
            continue
        kind = str(it.get("kind") or "fact")
        if kind not in ("fact", "pref", "decision", "env", "task"):
            kind = "fact"
        try:
            imp = float(it.get("importance", 0.6))
        except (TypeError, ValueError):
            imp = 0.6
        out.append({
            "content": content_s,
            "kind": kind,
            "importance": max(0.1, min(1.0, imp)),
            "title": str(it.get("title") or content_s[:40])[:60],
        })
        if len(out) >= max_items:
            break
    return out


def distill(
    text: str,
    cfg,
    max_items: int = 12,
) -> List[Dict[str, Any]]:
    """组合抽取：优先 LLM，失败或未配置时用规则。"""
    if cfg.use_llm and cfg.llm_base_url and cfg.llm_model:
        items = llm_extract(text, cfg.llm_base_url, cfg.llm_api_key,
                            cfg.llm_model, cfg.llm_timeout, max_items)
        if items:
            return items
    return rule_extract(text, max_items)
