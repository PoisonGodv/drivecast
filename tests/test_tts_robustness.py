"""TTS 稳健性:合成时解析音色、来源记录防混音、不可重试错误快速失败、整集回退、ffmpeg 超时。"""
from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import httpx

from drivecast.config import settings
from drivecast.jobs import worker
from drivecast.models import Job
from drivecast.tts import doubao, edge, service
from drivecast.tts.errors import FFmpegTimeoutError, TTSFatalError, TTSRetryExhausted


def _script(ep_id: str = "ep") -> dict:
    return {
        "episode_id": ep_id,
        "target_minutes": 1,
        "sections": [{"key": key, "title": key, "content": f"第{key}段口播正文。"}
                     for key in service.SECTION_KEYS],
    }


class FakeProvider:
    """写入 'provider:voice:rate' 字节的假通道;fail_on 命中的 seq 抛指定异常。"""

    def __init__(self, name: str, fail_on: dict[int, BaseException] | None = None) -> None:
        self.name = name
        self.fail_on = fail_on or {}
        self.calls: list[tuple[str, object, str]] = []
        self.jitter_sleep = AsyncMock()

    async def synth_chunk(self, text, voice, rate, out_path: Path) -> None:
        seq = int(out_path.stem)
        self.calls.append((voice, rate, out_path.name))
        if seq in self.fail_on:
            raise self.fail_on[seq]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(f"{self.name}:{voice}:{rate}")


async def _probe(path: Path) -> float:
    return 3.0 if path.is_file() and path.stat().st_size > 0 else 0.0


class TTSRobustnessBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.patches = [
            patch.object(settings, "data_dir", Path(self.tmp.name)),
            patch.object(settings, "tts_provider", "edge"),
            patch.object(settings, "tts_fallback_provider", ""),
            patch.object(service, "_probe_duration", side_effect=_probe),
        ]
        for p in self.patches:
            p.start()
        self.edge = FakeProvider("edge")
        self.doubao = FakeProvider("doubao")
        self.providers = patch.dict(service.PROVIDERS, {"edge": self.edge, "doubao": self.doubao})
        self.providers.start()

    def tearDown(self) -> None:
        self.providers.stop()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def chunks(self, n: int = 3, speaker: str = "expert") -> list[dict]:
        return [{"seq": i, "speaker": speaker, "text": "正文"} for i in range(1, n + 1)]


class ResolveAndProvenanceTest(TTSRobustnessBase):
    async def test_tts_json_does_not_freeze_voice(self) -> None:
        chunks = service.build_tts_json(_script())
        stored = json.loads(settings.tts_path("ep").read_text("utf-8"))["chunks"]
        self.assertTrue(chunks)
        for chunk in stored:
            self.assertNotIn("voice", chunk)
            self.assertNotIn("rate", chunk)
            self.assertIn(chunk["speaker"], service.SPEAKERS)
            self.assertTrue(chunk["section_key"])

    async def test_voice_resolved_at_synthesis_and_recorded(self) -> None:
        chunks = self.chunks(2) + [{"seq": 3, "speaker": "host", "text": "提问"}]
        with patch.object(settings, "tts_voice_expert", "zh-CN-A"), \
                patch.object(settings, "tts_voice_host", "zh-CN-B"):
            await service._synth_missing("ep", chunks)
        self.assertEqual([c[0] for c in self.edge.calls], ["zh-CN-A", "zh-CN-A", "zh-CN-B"])
        sources = service._load_sources("ep")
        self.assertEqual(sources["3"]["provider"], "edge")
        self.assertEqual(sources["3"]["voice"], "zh-CN-B")

    async def test_provider_switch_resynthesizes_old_chunks(self) -> None:
        chunks = self.chunks(3)
        await service._synth_missing("ep", chunks)
        self.assertEqual(len(self.edge.calls), 3)
        await service._synth_missing("ep", chunks)  # 同通道续跑全部复用
        self.assertEqual(len(self.edge.calls), 3)
        with patch.object(settings, "tts_provider", "doubao"):
            paths = await service._synth_missing("ep", chunks)
        self.assertEqual(len(self.doubao.calls), 3)
        self.assertTrue(all(p.read_text().startswith("doubao:") for p in paths))
        self.assertEqual({s["provider"] for s in service._load_sources("ep").values()}, {"doubao"})

    async def test_voice_change_within_provider_resynthesizes(self) -> None:
        chunks = self.chunks(2)
        await service._synth_missing("ep", chunks)
        with patch.object(settings, "tts_voice_expert", "zh-CN-OtherNeural"):
            await service._synth_missing("ep", chunks)
        self.assertEqual(len(self.edge.calls), 4)

    async def test_legacy_chunks_reused_only_when_voice_matches(self) -> None:
        legacy = [
            {"seq": 1, "speaker": "expert", "voice": settings.tts_voice_expert,
             "rate": settings.tts_rate_expert, "text": "正文"},
            {"seq": 2, "speaker": "expert", "voice": "zh_male_old_bigtts", "rate": -5, "text": "正文"},
        ]
        directory = settings.chunks_dir("ep")
        directory.mkdir(parents=True)
        for chunk in legacy:
            (directory / f"{chunk['seq']:03d}.mp3").write_text("legacy")
        await service._synth_missing("ep", legacy)
        self.assertEqual([c[2] for c in self.edge.calls], ["002.mp3"])
        self.assertEqual(set(service._load_sources("ep")), {"1", "2"})

    async def test_local_only_never_calls_provider(self) -> None:
        chunks = self.chunks(2)
        directory = settings.chunks_dir("ep")
        directory.mkdir(parents=True)
        for chunk in chunks:
            (directory / f"{chunk['seq']:03d}.mp3").write_text("any-provider")
        with patch.object(settings, "tts_provider", "doubao"):
            await service._synth_missing("ep", chunks, local_only=True)
        self.assertEqual(self.edge.calls + self.doubao.calls, [])


class FallbackTest(TTSRobustnessBase):
    async def test_no_fallback_configured_raises_primary_error(self) -> None:
        self.edge.fail_on = {2: TTSFatalError("edge 音色无效")}
        with self.assertRaisesRegex(TTSFatalError, "音色无效"):
            await service._synth_with_fallback("ep", self.chunks(3))
        self.assertEqual(self.doubao.calls, [])

    async def test_fallback_resynthesizes_whole_episode(self) -> None:
        self.doubao.fail_on = {2: TTSFatalError("豆包 TTS 额度不足或账户欠费")}
        with patch.object(settings, "tts_provider", "doubao"), \
                patch.object(settings, "tts_fallback_provider", "edge"):
            paths, info = await service._synth_with_fallback("ep", self.chunks(3))
        # 主通道已产出的 001 也被备用通道重做,整集不混音色
        self.assertEqual([c[2] for c in self.edge.calls], ["001.mp3", "002.mp3", "003.mp3"])
        self.assertTrue(all(p.read_text().startswith("edge:") for p in paths))
        self.assertEqual(info["provider"], "edge")
        self.assertEqual(info["fallback_from"], "doubao")
        self.assertIn("欠费", info["fallback_reason"])

    async def test_fallback_on_retry_exhausted(self) -> None:
        self.edge.fail_on = {1: TTSRetryExhausted("edge-tts 片段 001 连续 5 次失败")}
        with patch.object(settings, "tts_fallback_provider", "doubao"):
            _, info = await service._synth_with_fallback("ep", self.chunks(2))
        self.assertEqual(info["provider"], "doubao")
        self.assertEqual(len(self.doubao.calls), 2)

    async def test_generic_errors_do_not_trigger_fallback(self) -> None:
        self.edge.fail_on = {1: RuntimeError("chunk 无法探测时长")}
        with patch.object(settings, "tts_fallback_provider", "doubao"):
            with self.assertRaises(RuntimeError):
                await service._synth_with_fallback("ep", self.chunks(2))
        self.assertEqual(self.doubao.calls, [])

    async def test_both_fail_reports_both_and_is_not_retryable(self) -> None:
        self.edge.fail_on = {1: TTSFatalError("edge 被拒")}
        self.doubao.fail_on = {1: TTSFatalError("豆包鉴权失败")}
        with patch.object(settings, "tts_fallback_provider", "doubao"):
            with self.assertRaises(service.TTSFallbackFailed) as ctx:
                await service._synth_with_fallback("ep", self.chunks(2))
        self.assertIn("edge 被拒", str(ctx.exception))
        self.assertIn("豆包鉴权失败", str(ctx.exception))
        self.assertFalse(ctx.exception.retryable)

    async def test_episode_meta_records_provider(self) -> None:
        self.edge.fail_on = {1: TTSFatalError("edge 被拒")}
        with patch.object(settings, "tts_fallback_provider", "doubao"), \
                patch.object(service, "_concat", AsyncMock()), \
                patch.object(service, "_peak_db", AsyncMock(return_value=-3.0)), \
                patch("drivecast.persist.upsert_audio", AsyncMock()) as upsert:
            meta = await service.synthesize_episode(_script())
        self.assertEqual(meta["tts"]["provider"], "doubao")
        self.assertEqual(meta["tts"]["fallback_from"], "edge")
        self.assertEqual(meta["tts"]["voices"]["expert"], settings.tts_doubao_voice_expert)
        saved = json.loads(settings.meta_path("ep").read_text("utf-8"))
        self.assertEqual(saved["tts"]["provider"], "doubao")
        self.assertEqual(upsert.await_args.args[2], settings.tts_doubao_voice_expert)


class FailFastTest(unittest.IsolatedAsyncioTestCase):
    async def _doubao_calls(self, response: httpx.Response) -> tuple[int, BaseException]:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return response

        transport = httpx.MockTransport(handler)
        with TemporaryDirectory() as tmp, \
                patch.object(doubao.settings, "tts_doubao_api_key", "sk-1"), \
                patch.object(doubao.settings, "tts_chunk_max_retries", 4), \
                patch.object(doubao.asyncio, "sleep", AsyncMock()), \
                patch.object(doubao, "_build_client", lambda: httpx.AsyncClient(transport=transport)):
            with self.assertRaises(Exception) as ctx:
                await doubao.synth_chunk("x", "v", 0, Path(tmp) / "001.mp3")
        return len(calls), ctx.exception

    async def test_doubao_403_fails_fast_with_chinese_message(self) -> None:
        count, exc = await self._doubao_calls(httpx.Response(403, text="forbidden"))
        self.assertEqual(count, 1)
        self.assertIsInstance(exc, TTSFatalError)
        self.assertIn("鉴权失败", str(exc))

    async def test_doubao_quota_fails_fast(self) -> None:
        body = (json.dumps({"code": 45000292, "message": "quota exceeded for types: concurrency"})
                + "\n").encode()
        count, exc = await self._doubao_calls(httpx.Response(200, content=body))
        # 并发限流不是欠费,仍需退避重试
        self.assertEqual(count, 4)
        self.assertIsInstance(exc, TTSRetryExhausted)
        body = (json.dumps({"code": 45000000, "message": "account arrears, insufficient balance"})
                + "\n").encode()
        count, exc = await self._doubao_calls(httpx.Response(200, content=body))
        self.assertEqual(count, 1)
        self.assertIn("欠费", str(exc))

    async def test_doubao_invalid_voice_fails_fast(self) -> None:
        body = (json.dumps({"code": 45000000, "message": "speaker permission denied"}) + "\n").encode()
        count, exc = await self._doubao_calls(httpx.Response(200, content=body))
        self.assertEqual(count, 1)
        self.assertIn("音色不可用", str(exc))

    async def test_doubao_server_error_retries_then_exhausts(self) -> None:
        count, exc = await self._doubao_calls(httpx.Response(502, text="bad gateway"))
        self.assertEqual(count, 4)
        self.assertIsInstance(exc, TTSRetryExhausted)
        self.assertTrue(exc.retryable)

    async def test_doubao_missing_credentials_is_fatal(self) -> None:
        with TemporaryDirectory() as tmp, \
                patch.object(doubao.settings, "tts_doubao_api_key", ""), \
                patch.object(doubao.settings, "tts_doubao_app_key", ""):
            with self.assertRaises(TTSFatalError):
                await doubao.synth_chunk("x", "v", 0, Path(tmp) / "001.mp3")

    async def test_edge_invalid_voice_fails_fast(self) -> None:
        with TemporaryDirectory() as tmp, patch.object(edge.asyncio, "sleep", AsyncMock()) as sleep:
            with self.assertRaisesRegex(TTSFatalError, "参数无效"):
                await edge.synth_chunk("x", "not-a-voice", "-5%", Path(tmp) / "001.mp3")
            sleep.assert_not_awaited()

    async def test_worker_fails_fatal_tts_job_without_retry(self) -> None:
        job = Job(id=7, kind="tts", ref_id="ep", payload="{}", attempt=1, max_attempt=3)
        with patch.object(worker, "_finish", AsyncMock(return_value=True)) as finish, \
                patch.object(worker, "set_episode_status", AsyncMock()) as status, \
                patch.object(worker.traceback, "print_exception"):
            await worker._retry_or_fail(job, TTSFatalError("豆包 TTS 额度不足或账户欠费"))
        finish.assert_awaited_once_with(7, "failed", "豆包 TTS 额度不足或账户欠费")
        status.assert_awaited_once_with("ep", "failed")

    async def test_worker_retries_exhausted_tts_job(self) -> None:
        job = Job(id=8, kind="tts", ref_id="ep", payload="{}", attempt=1, max_attempt=3)
        with patch.object(worker, "_finish", AsyncMock(return_value=True)) as finish:
            await worker._retry_or_fail(job, TTSRetryExhausted("edge-tts 片段 001 连续 5 次失败"))
        finish.assert_awaited_once_with(8, "pending", "edge-tts 片段 001 连续 5 次失败")


class FFmpegTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_kills_process_on_timeout(self) -> None:
        started = time.monotonic()
        with self.assertRaisesRegex(FFmpegTimeoutError, "FFMPEG_TIMEOUT_SEC"):
            await service._run(["sleep", "10"], timeout=0.3)
        self.assertLess(time.monotonic() - started, 5)

    async def test_run_uses_setting_default(self) -> None:
        with patch.object(settings, "ffmpeg_timeout_sec", 0.3):
            with self.assertRaises(FFmpegTimeoutError):
                await service._run(["sleep", "10"])


if __name__ == "__main__":
    unittest.main()
