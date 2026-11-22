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


def _is_ollama_url(url: str) -> bool:
    """判断地址是否为 Ollama 原生接口。

    识别方式：
    - 路径含 /api（/api、/api/chat）
    - 端口为 11434（Ollama 默认端口）
    """
    if not url:
        return False
    low = url.lower()
    if "/api" in low:
        return True
    m = re.search(r":(\d{2,5})(?:/|$)", low)
    if m and m.group(1) == "11434":
        return True
    return False


def _build_chat_request(
    base_url: str,
    model: str,
    text: str,
    temperature: float,
    prompt: Optional[str] = None,
) -> tuple:
    """构造抽取请求。

    返回 (url, payload)。兼容三类本地部署：
    - OpenAI 兼容（/v1/chat/completions）
    - Ollama 原生（/api/chat）
    - 无后缀裸地址（自动补 /v1）

    prompt 若含 {text} 占位符，会在此处替换为原文。
    """
    url = base_url.rstrip("/")
    if prompt:
        system_prompt = prompt.replace("{text}", text[:20000])
    else:
        system_prompt = _LLM_PROMPT % text[:20000]

    if _is_ollama_url(url):
        if url.endswith("/api/chat"):
            ollama_url = url
        elif url.endswith("/api"):
            ollama_url = url + "/chat"
        else:
            ollama_url = url + "/api/chat"
        return ollama_url, {
            "model": model,
            "messages": [
                {"role": "system", "content": "你只输出 JSON，不做解释。"},
                {"role": "user", "content": system_prompt},
            ],
            "stream": False,
            "options": {"temperature": temperature},
        }

    if not url.endswith("/v1") and not url.endswith("/chat/completions"):
        url = url + "/v1"
    if not url.endswith("/chat/completions"):
        url = url + "/chat/completions"
    return url, {
        "model": model,
        "messages": [
            {"role": "system", "content": "你只输出 JSON，不做解释。"},
            {"role": "user", "content": system_prompt},
        ],
        "temperature": temperature,
    }


def llm_extract(
    text: str,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 60.0,
    max_items: int = 12,
    temperature: float = 0.1,
    prompt: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """调用内网 OpenAI 兼容网关做抽取；失败则返回空列表。

    base_url 支持：
    - http://host:port/v1        （OpenAI 兼容网关，如 vLLM / Xinference）
    - http://host:port           （自动补 /v1）
    - http://host:port/api       （Ollama 原生接口，自动转 /api/chat）
    """
    if not (base_url and model):
        return []
    endpoint, payload = _build_chat_request(base_url, model, text, temperature, prompt)
    req = urllib.request.Request(
        endpoint,
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
        if "choices" in data:
            content = data["choices"][0]["message"]["content"]
        else:
            # Ollama 原生返回：message.content
            content = data["message"]["content"]
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


def _load_prompt(cfg) -> str:
    """读取自定义抽取提示词文件（支持 {text} 占位）。"""
    path = ""
    try:
        path = (getattr(cfg, "extract_prompt_file", "") or "").strip()
    except Exception:
        path = ""
    if not path:
        return ""
    try:
        from pathlib import Path

        return Path(path).expanduser().read_text(encoding="utf-8")
    except Exception:
        return ""


def distill(
    text: str,
    cfg,
    max_items: int = 0,
) -> List[Dict[str, Any]]:
    """组合抽取：优先配置的抽取模型，失败或未配置时用规则。"""
    try:
        eff = cfg.effective_extract()
    except Exception:
        eff = {
            "base_url": getattr(cfg, "llm_base_url", ""),
            "api_key": getattr(cfg, "llm_api_key", ""),
            "model": getattr(cfg, "llm_model", ""),
            "temperature": 0.1,
            "timeout": getattr(cfg, "llm_timeout", 60.0),
            "max_items": 12,
            "prompt_file": "",
            "enabled": False,
        }

    limit = int(max_items or eff.get("max_items") or 12)
    if eff.get("enabled"):
        prompt = ""
        pfile = eff.get("prompt_file")
        if pfile:
            try:
                from pathlib import Path

                raw = Path(pfile).expanduser().read_text(encoding="utf-8")
                prompt = raw.replace("{text}", text[:20000])
            except Exception:
                prompt = ""
        items = llm_extract(
            text,
            eff["base_url"],
            eff["api_key"],
            eff["model"],
            eff.get("timeout", 60.0),
            limit,
            temperature=eff.get("temperature", 0.1),
            prompt=prompt or None,
        )
        if items:
            return items
    return rule_extract(text, limit)
