"""TTS 友好化(docs/03 §2.2):确定性念法改写,不靠 LLM 自由发挥。

听感翻车的 90% 在这步:QPS/p99/200ms/O(log n) 直接喂 TTS 会念成乱码。
规则分两类:映射表(术语逐条校对) + 正则规则(数字/单位/百分位)。
"""
from __future__ import annotations

import re

# ---------- 数字 → 中文 ----------

_DIGITS = "零一二三四五六七八九"
_UNITS4 = ["", "十", "百", "千"]
_UNITS_BIG = ["", "万", "亿"]


def int_to_chinese(n: int) -> str:
    """0-999999999 → 中文读法(一千二百三十四 / 一万 / 三点五亿 的整数部分)。"""
    if n == 0:
        return "零"
    if n < 0:
        return "负" + int_to_chinese(-n)

    def four(num: int) -> str:  # 0-9999
        s = ""
        zero_pending = False
        for i in range(3, -1, -1):
            d = num // (10 ** i) % 10
            if d == 0:
                if s:
                    zero_pending = True
            else:
                if zero_pending:
                    s += "零"
                    zero_pending = False
                s += _DIGITS[d] + _UNITS4[i]
        if s.startswith("一十"):  # 10-19 习惯读「十五」不读「一十五」
            s = s[1:]
        return s

    groups = []
    while n > 0:
        groups.append(n % 10000)
        n //= 10000
    parts = []
    for i in range(len(groups) - 1, -1, -1):
        g = groups[i]
        if g == 0:
            continue
        seg = four(g)
        # 万以下不足千位要补零: 10023 → 一万零二十三
        if parts and g < 1000:
            seg = "零" + seg
        parts.append(seg + _UNITS_BIG[i])
    s = "".join(parts)
    # 口语习惯:两百/两千/两万,不说二百/二千/二万
    return s.replace("二百", "两百").replace("二千", "两千").replace("二万", "两万")


def num_to_chinese(s: str) -> str:
    """支持小数: 99.9 → 九十九点九。"""
    if "." in s:
        a, b = s.split(".", 1)
        return int_to_chinese(int(a)) + "点" + "".join(_DIGITS[int(c)] for c in b)
    return int_to_chinese(int(s))


# ---------- 术语映射表(逐条人工校对,docs/03 §2.2) ----------
# 首字母缩写 → 逐字母念;通行中文名 → 中文;已通行英文发音的保留原样(edge-tts 多语种能读)。

TERM_MAP: dict[str, str] = {
    # 高频缩写 → 逐字母
    "QPS": "Q P S", "TPS": "T P S", "RPS": "R P S",
    "TTL": "T T L", "RPC": "R P C", "HTTP": "H T T P", "HTTPS": "H T T P S",
    "TCP": "T C P", "UDP": "U D P", "CPU": "C P U", "GPU": "G P U",
    "API": "A P I", "SQL": "S Q L", "ACID": "A C I D", "CAP": "C A P",
    "GC": "G C", "JVM": "J V M", "IO": "I O", "KV": "K V",
    "MQ": "M Q", "ES": "E S", "K8s": "K 八 s", "SLA": "S L A",
    "RTT": "R T T", "B+树": "B 加树", "MVCC": "M V C C",
    "SETNX": "set N X", "GETEX": "get E X",
    "STW": "S T W", "GMP": "G M P", "WAL": "W A L", "LSM": "L S M",
    "SSD": "S S D", "InnoDB": "Inno D B", "Goroutine": "goroutine",
    "Raft": "Raft", "Paxos": "Paxos",
    # 通行中文名
    "Redlock": "红锁", "RedLock": "红锁",
    "fencing token": "fencing 令牌", "Fencing Token": "fencing 令牌",
    "etcd": "E T C D", "nginx": "engine X", "Kubernetes": "酷博netes",
    "Kafka": "卡夫卡", "MySQL": "My S Q L", "PostgreSQL": "Postgre S Q L",
    "Redis": "Redis", "MongoDB": "Mongo D B",
    "Linux": "Linux", "Docker": "Docker", "Git": "Git",
    "Go": "Go", "Golang": "Go 语言",
    "LLM": "L L M", "RAG": "R A G", "SFT": "S F T", "LoRA": "LoRA",
    "KV cache": "K V cache", "vLLM": "v L L M",
    # 复杂度
    "O(log n)": "O log n", "O(n log n)": "O n log n", "O(n²)": "O n 平方",
    "O(n)": "O n", "O(1)": "O 一", "O(n^2)": "O n 平方",
}

# 长的在前,避免子串误伤(如 HTTPS 先于 HTTP);
# 纯 ASCII 术语加边界,防止 "ES" 误伤 "QUERIES" 这类英文单词内部。
def _term_pattern(k: str) -> str:
    pat = re.escape(k)
    if re.fullmatch(r"[A-Za-z0-9+.' ]+", k):
        pat = rf"(?<![A-Za-z0-9]){pat}(?![A-Za-z0-9])"
    return pat


_TERM_KEYS = sorted(TERM_MAP, key=len, reverse=True)
_TERM_RE = re.compile("|".join(_term_pattern(k) for k in _TERM_KEYS))

# 正则规则
_RE_PERCENTILE = re.compile(r"\b[pP](\d{2}(?:\.\d)?)\b")          # p99 / p99.9
_RE_MS = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|毫秒|μs|微秒|s|秒)\b")  # 200ms→两百毫秒
_RE_VERSION = re.compile(r"\b(\d+)\.(\d+)(?:\.(\d+))?\b")          # 1.21 / 3.9.2(在百分位之后处理)
_RE_BIG_NUM = re.compile(r"\b(\d{5,})\b")                           # ≥5 位大数 → 万/亿
_RE_NUM = re.compile(r"\b(\d{2,4})\b")                              # 2-4 位数 → 中文读法


def _unit_cn(unit: str) -> str:
    return {"ms": "毫秒", "μs": "微秒", "s": "秒"}.get(unit, unit)


def friendly(text: str) -> str:
    """口播文本 → TTS 友好文本。全篇一致性:同一术语处处同念法。"""
    # 1. 术语映射表
    text = _TERM_RE.sub(lambda m: TERM_MAP[m.group(0)], text)
    # 2. 百分位 p99 → P 九十九
    text = _RE_PERCENTILE.sub(lambda m: "P " + num_to_chinese(m.group(1)), text)
    # 3. 时间单位 200ms → 两百毫秒(先处理带单位的数,避免被通用数字规则抢)
    text = _RE_MS.sub(lambda m: num_to_chinese(m.group(1)) + _unit_cn(m.group(2)), text)
    # 4. 版本号 1.21 → 一点二一(此时小数只剩版本号类)
    text = _RE_VERSION.sub(lambda m: _version_cn(m.group(0)), text)
    # 5. 大数 10000 → 一万
    text = _RE_BIG_NUM.sub(lambda m: int_to_chinese(int(m.group(1))), text)
    # 6. 2-4 位数字 15 → 十五(1 位数字保持,中文上下文自然)
    text = _RE_NUM.sub(lambda m: int_to_chinese(int(m.group(1))), text)
    return text


def _version_cn(v: str) -> str:
    return "点".join("".join(_DIGITS[int(c)] for c in part) for part in v.split("."))
