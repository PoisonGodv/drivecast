"""M0 冒烟(docs/05 M0-5):依赖导入 + 最小 LLM 调用 + 一句 edge-tts + 一次 ffmpeg loudnorm。

  .venv/bin/python scripts/smoke.py
"""
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ok = True


def check(name, fn):
    global ok
    try:
        fn()
        print(f"  ✓ {name}")
    except Exception as exc:
        ok = False
        print(f"  ✗ {name}: {exc}")


print("[1/4] 依赖导入")
def _imports():
    import edge_tts, openai, pydantic, pydantic_settings  # noqa: F401
    from drivecast.tts import friendly, chunk, edge, service  # noqa: F401
    from drivecast.llm import provider, prompts  # noqa: F401
check("全部依赖 + drivecast 包导入", _imports)

print("[2/4] LLM 连通")
from drivecast.llm.provider import get_provider  # noqa: E402
async def _llm():
    r = await get_provider().complete(system="你是助手", user="只回复两个字:正常", temperature=0)
    assert r.content.strip(), "LLM 返回为空"
    print(f"      LLM 回复: {r.content.strip()[:20]} (tokens={r.usage_tokens})")
check("最小 LLM 调用", lambda: asyncio.run(_llm()))

print("[3/4] edge-tts 合成")
from drivecast.config import settings  # noqa: E402
async def _tts():
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        out = Path(f.name)
    await asyncio.wait_for(_synth(out), timeout=60)
    assert out.stat().st_size > 1000, "合成产物过小"
    print(f"      合成 {out.stat().st_size} bytes → {out}")
    globals()["_tts_out"] = out
async def _synth(out):
    import edge_tts
    await edge_tts.Communicate("驱动播客冒烟测试,数字一万, Q P S。", voice=settings.tts_voice_expert).save(str(out))
check("edge-tts 一句合成", lambda: asyncio.run(_tts()))

print("[4/4] ffmpeg loudnorm")
def _ffmpeg():
    src = globals().get("_tts_out")
    assert src, "依赖第 3 步产物"
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        dst = f.name
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
         "-c:a", "libmp3lame", "-q:a", "4", dst],
        check=True, capture_output=True,
    )
    r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", dst],
                       check=True, capture_output=True)
    import json
    dur = json.loads(r.stdout)["format"]["duration"]
    print(f"      loudnorm OK, 时长 {dur}s")
check("ffmpeg 归一 + ffprobe 读时长", _ffmpeg)

print("\n冒烟" + ("全绿 ✓" if ok else "有失败 ✗"))
sys.exit(0 if ok else 1)
