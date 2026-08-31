"""分段(docs/03 §2.3):200-400 字目标,500 硬上限,句读边界切,不切术语/数字中间。"""
from __future__ import annotations

import re

TARGET_MIN, TARGET_MAX, HARD_MAX = 200, 400, 500

# 句子结束符;引号/括号收尾也算
_SENT_END = re.compile(r"[。!?;!?…]+[」』\"']?")
# 保护段:术语/数字/版本号序列,不在其中间断(保护已 token 化的最小单元)
_PROTECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+/%_-]*")


def _sentences(paragraph: str) -> list[str]:
    out, pos = [], 0
    for m in _SENT_END.finditer(paragraph):
        out.append(paragraph[pos : m.end()])
        pos = m.end()
    if pos < len(paragraph):
        out.append(paragraph[pos:])
    return [s for s in out if s.strip()]


def _split_long(sentence: str) -> list[str]:
    """单句超 HARD_MAX:按逗号/顿号切;仍超则硬切(避开保护段)。"""
    if len(sentence) <= HARD_MAX:
        return [sentence]
    parts = re.split(r"(?<=[,,、:,,:])", sentence)
    parts = [p for p in parts if p.strip()]
    if all(len(p) <= HARD_MAX for p in parts):
        return parts
    # 硬切:找 HARD_MAX 内最靠后的非保护段边界
    out, rest = [], sentence
    while len(rest) > HARD_MAX:
        cut = HARD_MAX
        while cut > TARGET_MIN and _PROTECT.fullmatch(rest[max(0, cut - 1) : cut + 1]):
            cut -= 1
        out.append(rest[:cut])
        rest = rest[cut:]
    if rest.strip():
        out.append(rest)
    return out


def chunk_text(text: str) -> list[str]:
    """整篇 → 有序 chunk 列表。先按空行分段落,再组包到 200-400 字。"""
    units: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        for sent in _sentences(para):
            units.extend(_split_long(sent))

    chunks: list[str] = []
    buf = ""
    for u in units:
        if buf and len(buf) + len(u) > TARGET_MAX and len(buf) >= TARGET_MIN:
            chunks.append(buf)
            buf = u
        elif len(buf) + len(u) > HARD_MAX:
            chunks.append(buf)
            buf = u
        else:
            buf += u
    if buf.strip():
        chunks.append(buf)
    return chunks
