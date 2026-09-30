"""豆包(火山引擎)语音合成封装(docs/03 §3.5)— seed-tts-2.0 · HTTP 单向流式。

接口:POST https://openspeech.bytedance.com/api/v3/tts/unidirectional
鉴权(新版控制台,推荐):X-Api-Key + X-Api-Resource-Id: seed-tts-2.0,只需一个 API Key;
旧版控制台退回 X-Api-App-Id + X-Api-Access-Key 一对。Resource-Id 与音色代次必须匹配:
2.0 音色(*_uranus_bigtts / saturn_*)→ seed-tts-2.0,1.0 音色 → seed-tts-1.0。
响应为 NDJSON:逐行 {code,data(base64)} 拼音频,code=20000000 结束。
失败语义与 edge 通道完全一致:全局信号量 + 指数退避 + .part 原子写,断点续合成不受影响。
"""
from __future__ import annotations

import asyncio
import base64
import json
import random
from pathlib import Path

import httpx

from ..config import settings
from ..concurrency import request_limit
from .errors import TTSFatalError, TTSRetryExhausted, brief

STATUS_OK = 20000000  # code=20000000 音频合成结束的成功状态码
# 欠费/余额/额度/鉴权类关键词:重试只会重复扣请求次数、刷屏日志,直接判死
_QUOTA_HINTS = ("arrear", "insufficient", "balance", "欠费", "余额", "quota", "exceeded the limit",
                "not enough", "resource pack", "资源包", "用量", "trial")
_AUTH_HINTS = ("unauthorized", "forbidden", "invalid api key", "invalid token", "access denied",
               "authenticat", "鉴权", "not granted", "resource id is mismatched")
_VOICE_HINTS = ("speaker", "voice_type", "voice type", "音色")


class DoubaoTTSError(RuntimeError):
    """豆包 TTS 远端/协议错误,message 已含错误码与官方 message。"""


class DoubaoFatalError(DoubaoTTSError, TTSFatalError):
    """鉴权/欠费/音色无效等不可重试错误,message 为中文说明 + 原始错误。"""


def classify_error(raw: str, http_status: int | None = None) -> DoubaoTTSError:
    """把远端错误映射成可重试/不可重试;并发限流(concurrency)仍按可重试退避。"""
    low = raw.lower()
    if http_status == 429 or "concurrency" in low or "qps" in low or "rate limit" in low:
        return DoubaoTTSError(raw)
    if http_status in (401, 403) or any(h in low for h in _AUTH_HINTS):
        return DoubaoFatalError(
            f"豆包 TTS 鉴权失败({raw}),请检查 TTS_DOUBAO_API_KEY 与 TTS_DOUBAO_RESOURCE_ID 是否匹配、是否已开通")
    if any(h in low for h in _QUOTA_HINTS):
        return DoubaoFatalError(f"豆包 TTS 额度不足或账户欠费({raw}),请到火山引擎控制台充值或购买资源包")
    if any(h in low for h in _VOICE_HINTS):
        return DoubaoFatalError(f"豆包 TTS 音色不可用({raw}),请检查 TTS_DOUBAO_VOICE_* 是否填写正确且已授权")
    if http_status == 400:
        return DoubaoFatalError(f"豆包 TTS 拒绝了请求参数({raw})")
    return DoubaoTTSError(raw)


def speech_rate_value(rate: str | int | float) -> int:
    """edge 风格 "-5%" / 豆包 int 统一为 speech_rate,截断到官方区间 [-50,100]。"""
    if isinstance(rate, (int, float)):
        value = int(rate)
    else:
        text = str(rate).replace("%", "").strip()
        value = int(float(text)) if text else 0
    return max(-50, min(100, value))


def _auth_headers() -> dict[str, str]:
    """新版控制台一个 API Key 搞定;旧版退回 App-Id + Access-Key 一对(1598757 §2.1)。"""
    headers = {
        "Content-Type": "application/json",
        "X-Api-Resource-Id": settings.tts_doubao_resource_id,
    }
    if settings.tts_doubao_api_key:
        headers["X-Api-Key"] = settings.tts_doubao_api_key
    else:
        headers["X-Api-App-Id"] = settings.tts_doubao_app_key
        headers["X-Api-App-Key"] = settings.tts_doubao_app_key  # 两版文档头名不一,都带上
        headers["X-Api-Access-Key"] = settings.tts_doubao_access_key
    return headers


def _require_credentials() -> None:
    if settings.tts_doubao_api_key:
        return
    if settings.tts_doubao_app_key and settings.tts_doubao_access_key:
        return
    raise TTSFatalError(
        "豆包 TTS 缺少凭证: TTS_DOUBAO_API_KEY(新版控制台,推荐,一个 key 即可)"
        "或 TTS_DOUBAO_APP_KEY+TTS_DOUBAO_ACCESS_KEY(旧版);写入 .env(docs/03 §3.5)")


def _request_payload(text: str, speaker: str, speech_rate: int) -> dict:
    return {
        "user": {"uid": settings.tts_doubao_uid},
        "req_params": {
            "text": text,
            "speaker": speaker,
            "audio_params": {
                "format": settings.tts_doubao_format,
                "sample_rate": settings.tts_doubao_sample_rate,
                "speech_rate": speech_rate,
            },
        },
    }


def parse_ndjson_line(line: str) -> dict:
    """一行 NDJSON → dict;坏行抛错(流协议,吞掉会静默丢音频)。"""
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise DoubaoTTSError(f"NDJSON 坏行: {line[:200]}") from exc


def _build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=settings.tts_doubao_recv_timeout))


async def _synthesize_once(
    text: str, speaker: str, rate: str | int, client: httpx.AsyncClient | None = None,
) -> bytes:
    """单 chunk 单请求:POST 全文 → 流式收 NDJSON → 拼 base64 音频块。"""
    payload = _request_payload(text, speaker, speech_rate_value(rate))
    audio = bytearray()
    own = client is None
    if own:
        client = _build_client()
    try:
        async with client.stream(
            "POST", settings.tts_doubao_http_url, headers=_auth_headers(), json=payload,
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "ignore")
                raise classify_error(f"HTTP {resp.status_code}: {body[:200]}", resp.status_code)
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                item = parse_ndjson_line(line)
                code = item.get("code")
                if code == STATUS_OK:
                    break  # 合成结束
                if code not in (0, None):
                    raise classify_error(f"status={code} {item.get('message', '')}")
                data = item.get("data")
                if data:
                    try:
                        audio += base64.b64decode(data)
                    except (ValueError, TypeError) as exc:
                        raise DoubaoTTSError("音频块 base64 解码失败") from exc
    finally:
        if own:
            await client.aclose()
    if not audio:
        raise DoubaoTTSError("豆包 TTS 产物为空")
    return bytes(audio)


def _get_sem() -> asyncio.Semaphore:
    return request_limit("tts")


async def synth_chunk(text: str, voice: str, rate: str | int, out_path: Path) -> None:
    """合成单个 chunk 到 mp3;.part 原子写 + 指数退避,语义与 edge.synth_chunk 一致。"""
    _require_credentials()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = out_path.with_suffix(f"{out_path.suffix}.part")
    max_retry = settings.tts_chunk_max_retries
    async with _get_sem():
        last: BaseException | None = None
        for attempt in range(1, max_retry + 1):
            try:
                part_path.write_bytes(await _synthesize_once(text, voice, rate))
                part_path.replace(out_path)
                return
            except (asyncio.CancelledError, TTSFatalError):
                part_path.unlink(missing_ok=True)
                raise
            except Exception as exc:  # noqa: BLE001 — 网络/限流/协议错误统一退避
                part_path.unlink(missing_ok=True)
                last = exc
                if attempt == max_retry:
                    break
                delay = 2 ** (attempt - 1) + random.uniform(0, 0.5)
                print(f"[tts] chunk {out_path.name} 第{attempt}次失败({exc}), {delay:.1f}s 后重试")
                await asyncio.sleep(delay)
        raise TTSRetryExhausted(
            f"豆包 TTS 片段 {out_path.stem} 连续 {max_retry} 次失败:{brief(last or '')}") from last


async def jitter_sleep() -> None:
    """付费接口按并发配额限流,不需要模拟人类节奏,只留极短间隔。"""
    await asyncio.sleep(random.uniform(0.05, 0.15))
