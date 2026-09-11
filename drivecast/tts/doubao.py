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

STATUS_OK = 20000000  # code=20000000 音频合成结束的成功状态码

# 全局信号量:与 edge 通道同规格,多集共用一个(docs/03 §3.3)
_sem: asyncio.Semaphore | None = None


class DoubaoTTSError(RuntimeError):
    """豆包 TTS 远端/协议错误,message 已含错误码与官方 message。"""


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
    raise RuntimeError(
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
                raise DoubaoTTSError(f"HTTP {resp.status_code}: {body[:300]}")
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                item = parse_ndjson_line(line)
                code = item.get("code")
                if code == STATUS_OK:
                    break  # 合成结束
                if code not in (0, None):
                    raise DoubaoTTSError(f"status={code} {item.get('message', '')}")
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
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(settings.tts_max_concurrency)
    return _sem


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
            except asyncio.CancelledError:
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
        raise RuntimeError(f"chunk 合成失败({max_retry} 次): {out_path}") from last


async def jitter_sleep() -> None:
    """付费接口按并发配额限流,不需要模拟人类节奏,只留极短间隔。"""
    await asyncio.sleep(random.uniform(0.05, 0.15))
