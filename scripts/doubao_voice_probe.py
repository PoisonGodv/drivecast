"""豆包 TTS 连通性/音色试听探针(docs/03 §3.5,seed-tts-2.0 · HTTP 单向流式)。

用法:
    python scripts/doubao_voice_probe.py [试音文本]
产物: data/audio/_probe/{voice}.mp3 —— 逐个试听挑音色;凭证缺失时报错给出 .env 指引。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drivecast.config import ensure_data_dirs, settings  # noqa: E402
from drivecast.tts import doubao  # noqa: E402


async def main() -> None:
    text = sys.argv[1] if len(sys.argv) > 1 else "欢迎收听驱动播客,今天聊聊分布式锁的来龙去脉。"
    ensure_data_dirs()
    out_dir = settings.data_dir / "audio" / "_probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    voices = {
        "expert 默认": settings.tts_doubao_voice_expert,
        "host 默认": settings.tts_doubao_voice_host,
        # 想试别的音色往这里加,名字见火山引擎《音色列表》;2.0 音色(*_uranus_bigtts/saturn_*)
        # 配 TTS_DOUBAO_RESOURCE_ID=seed-tts-2.0,1.0 音色要换回 seed-tts-1.0
    }
    for label, voice in voices.items():
        out = out_dir / f"{voice}.mp3"
        await doubao.synth_chunk(text, voice, 0, out)
        print(f"[probe] {label} {voice} → {out}")


if __name__ == "__main__":
    asyncio.run(main())
