"""文件产物级操作:勘误记录、产物备份、人审标记(docs/07 §4/§5)。

全部走文件态(data/),不碰 DB。勘误用独立 sidecar 文件
`data/meta/{ep}.errata.json` 而不是内嵌 meta——meta 会随重生成被备份替换,
勘误历史必须独立存活(对 docs/07 §5 的实现修订)。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings


def _errata_path(ep_ext_id: str) -> Path:
    return settings.meta_path(ep_ext_id).with_suffix(".errata.json")


def load_errata(ep_ext_id: str) -> list[dict]:
    """读取勘误历史;文件缺失/非法返回空列表。"""
    path = _errata_path(ep_ext_id)
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def append_errata(ep_ext_id: str, reason: str) -> list[dict]:
    """追加一条勘误记录,返回全量历史。"""
    entries = load_errata(ep_ext_id)
    entries.append({"reason": reason, "at": datetime.now(timezone.utc).isoformat()})
    path = _errata_path(ep_ext_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), "utf-8")
    return entries


def backup_artifacts(ep_ext_id: str) -> list[str]:
    """四类产物改名 .bak(存在才备份;旧 .bak 直接覆盖,只保留上一代,docs/07 §5)。"""
    backed: list[str] = []
    for path in (
        settings.script_path(ep_ext_id),
        settings.tts_path(ep_ext_id),
        settings.meta_path(ep_ext_id),
        settings.episode_audio_path(ep_ext_id),
    ):
        if not path.exists():
            continue
        bak = Path(f"{path}.bak")
        path.replace(bak)  # 原子改名,已有 .bak 覆盖
        backed.append(bak.name)
    return backed


def load_human_qc(ep_ext_id: str) -> dict | None:
    """读取 meta 里的人审标记(docs/07 §4);缺失返回 None。"""
    path = settings.meta_path(ep_ext_id)
    if not path.exists():
        return None
    try:
        meta = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = meta.get("human_qc")
    return value if isinstance(value, dict) else None


def write_human_qc(ep_ext_id: str, passed: bool, notes: str = "") -> dict:
    """人审标记写入 meta 的 human_qc;meta 缺失/损坏时建最小骨架不抛错。"""
    path = settings.meta_path(ep_ext_id)
    meta: dict = {}
    if path.exists():
        try:
            meta = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
    meta["human_qc"] = {
        "status": "passed" if passed else "pending",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "notes": notes,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    return meta["human_qc"]
