"""独立于 MP3 编码版本的只读质量策略。"""
from datetime import datetime, timezone
import math

from ..config import settings

QUALITY_VERSION = 1


def policy() -> dict:
    return {"min_sec": settings.quality_min_minutes * 60,
            "max_sec": settings.quality_max_minutes * 60,
            "max_error_pct": settings.quality_duration_error_pct,
            "limit_db": settings.quality_max_peak_db}


def stale(meta: dict) -> bool:
    quality = meta.get("quality")
    quality = quality if isinstance(quality, dict) else {}
    return quality.get("version") != QUALITY_VERSION or quality.get("policy") != policy()


def positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def duration_check(duration: float) -> dict:
    p = policy()
    return {"value_sec": duration, "min_sec": p["min_sec"], "max_sec": p["max_sec"],
            "passed": p["min_sec"] <= duration <= p["max_sec"] if positive(duration) else None}


def error_check(duration, expected, key):
    error = abs(duration - expected) / expected * 100 if positive(expected) and positive(duration) else None
    return {key: expected, "error_pct": round(error, 2) if error is not None else None,
            "max_error_pct": settings.quality_duration_error_pct,
            "passed": error <= settings.quality_duration_error_pct if error is not None else None}


def report(duration, estimated, requested_minutes, peak, chunks) -> dict:
    checks = {
        "duration_range": duration_check(duration),
        "estimated_duration_error": error_check(duration, estimated, "estimated_sec"),
        "requested_duration_error": error_check(duration, requested_minutes * 60 if positive(requested_minutes) else None, "requested_sec"),
        "peak": {"max_volume_db": peak, "limit_db": settings.quality_max_peak_db,
                 "clipping_detected": peak > settings.quality_max_peak_db if peak is not None else None,
                 "passed": peak <= settings.quality_max_peak_db if peak is not None else None},
        "chunks": chunks,
    }
    failed = [k for k, v in checks.items() if v.get("passed") is False]
    unknown = any(v.get("passed") is None for v in checks.values())
    return {"version": QUALITY_VERSION, "policy": policy(),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "status": "warning" if failed else "unknown" if unknown else "passed",
            "checks": checks, "failed_checks": failed}


def message(name: str, checks: dict) -> str:
    if name == "duration_range":
        check = checks.get(name)
        check = check if isinstance(check, dict) else {}
        low = check.get("min_sec", settings.quality_min_minutes * 60) / 60
        high = check.get("max_sec", settings.quality_max_minutes * 60) / 60
        return f"音频时长不在 {low:g}–{high:g} 分钟范围内"
    return {"target_duration_error": "实际时长与文本预测时长误差超过阈值",
            "estimated_duration_error": "实际时长与文本预测时长误差超过阈值",
            "requested_duration_error": "实际时长与所选目标时长误差超过阈值",
            "peak": "峰值过高，可能存在削波", "chunks": "音频分段数量或时长存在异常"}.get(name, name)
