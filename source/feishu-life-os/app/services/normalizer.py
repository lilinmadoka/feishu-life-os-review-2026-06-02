from __future__ import annotations

import re


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_candidate_sentences(text: str) -> list[str]:
    # Keep line-based structure first, then split long mixed messages.
    parts: list[str] = []
    for line in text.splitlines():
        line = line.strip(" -•\t")
        if not line:
            continue
        chunks = re.split(r"[；;。]\s*", line)
        parts.extend(chunk.strip() for chunk in chunks if chunk.strip())
    return parts or [text]


def compact_fingerprint(text: str) -> str:
    text = text.lower()
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[\s,，。.!！?？;；:：、\-—_~`'\"“”‘’()（）\[\]{}<>《》]", "", text)
    prefixes = ["提醒我", "帮我记", "記得", "记得", "别忘", "todo", "待办", "要", "需要", "我需要", "我要"]
    for prefix in prefixes:
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text[:80]
