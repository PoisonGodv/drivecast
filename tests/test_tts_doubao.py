"""豆包 seed-tts-2.0 HTTP 单向流式 TTS 单测(docs/03 §3.5):NDJSON 解析 + 会话流程(MockTransport)。"""
from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx

from drivecast.tts import doubao


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def ndjson(*items: dict) -> bytes:
    return ("\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n").encode()


OK_STREAM = ndjson(
    {"code": 0, "message": "", "data": b64(b"\xff\xfb part1")},
    {"code": 0, "message": "", "data": None, "sentence": {"text": "你好。", "words": []}},
    {"code": 0, "message": "", "data": b64(b" part2")},
    {"code": 20000000, "message": "ok", "data": None, "usage": {"text_words": 3}},
)


def mock_client(handler, **kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)


class DoubaoAuthTest(unittest.TestCase):
    def test_api_key_alone_suffices(self) -> None:
        with patch.object(doubao.settings, "tts_doubao_api_key", "sk-1"), \
                patch.object(doubao.settings, "tts_doubao_app_key", ""), \
                patch.object(doubao.settings, "tts_doubao_access_key", ""):
            headers = doubao._auth_headers()
            self.assertEqual(headers["X-Api-Key"], "sk-1")
            self.assertNotIn("X-Api-App-Id", headers)
            doubao._require_credentials()  # 不抛即通过

    def test_legacy_console_pair_falls_back(self) -> None:
        with patch.object(doubao.settings, "tts_doubao_api_key", ""), \
                patch.object(doubao.settings, "tts_doubao_app_key", "123"), \
                patch.object(doubao.settings, "tts_doubao_access_key", "tok"):
            headers = doubao._auth_headers()
            self.assertNotIn("X-Api-Key", headers)
            self.assertEqual(headers["X-Api-App-Id"], "123")
            self.assertEqual(headers["X-Api-Access-Key"], "tok")

    def test_missing_credentials_raise_with_env_hint(self) -> None:
        with patch.object(doubao.settings, "tts_doubao_api_key", ""), \
                patch.object(doubao.settings, "tts_doubao_app_key", ""), \
                patch.object(doubao.settings, "tts_doubao_access_key", "only-one"):
            with self.assertRaisesRegex(RuntimeError, "TTS_DOUBAO_API_KEY"):
                doubao._require_credentials()

    def test_speech_rate_conversion(self) -> None:
        self.assertEqual(doubao.speech_rate_value("-5%"), -5)
        self.assertEqual(doubao.speech_rate_value("+2%"), 2)
        self.assertEqual(doubao.speech_rate_value(0), 0)
        self.assertEqual(doubao.speech_rate_value(-8), -8)
        self.assertEqual(doubao.speech_rate_value("-80%"), -50)   # 截断到下界
        self.assertEqual(doubao.speech_rate_value("150%"), 100)   # 截断到上界

    def test_request_payload_shape(self) -> None:
        with patch.object(doubao.settings, "tts_doubao_uid", "drivecast"):
            payload = doubao._request_payload("你好", "zh_male_m191_uranus_bigtts", -5)
        self.assertEqual(payload["req_params"]["speaker"], "zh_male_m191_uranus_bigtts")
        self.assertEqual(payload["req_params"]["audio_params"]["speech_rate"], -5)
        self.assertEqual(payload["req_params"]["audio_params"]["format"], "mp3")


class DoubaoSynthTest(unittest.IsolatedAsyncioTestCase):
    async def test_synthesize_concatenates_ndjson_audio(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("X-Api-Key")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, content=OK_STREAM)

        with patch.object(doubao.settings, "tts_doubao_api_key", "sk-1"):
            async with mock_client(handler) as client:
                audio = await doubao._synthesize_once("你好世界", "zh_female_vv_uranus_bigtts",
                                                      "-5%", client=client)
        self.assertEqual(audio, b"\xff\xfb part1 part2")
        self.assertIn("/api/v3/tts/unidirectional", seen["url"])
        self.assertEqual(seen["auth"], "sk-1")
        self.assertEqual(seen["body"]["req_params"]["audio_params"]["speech_rate"], -5)

    async def test_stream_error_code_raises(self) -> None:
        stream = ndjson(
            {"code": 0, "message": "", "data": b64(b"abc")},
            {"code": 45000000, "message": "speaker permission denied"},
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=stream)

        async with mock_client(handler) as client:
            with self.assertRaisesRegex(doubao.DoubaoTTSError, "45000000.*permission denied"):
                await doubao._synthesize_once("x", "zh_bad", 0, client=client)

    async def test_http_error_raises_with_body(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="unauthorized")

        async with mock_client(handler) as client:
            with self.assertRaisesRegex(doubao.DoubaoTTSError, "401.*unauthorized"):
                await doubao._synthesize_once("x", "v", 0, client=client)

    async def test_empty_audio_raises(self) -> None:
        stream = ndjson({"code": 20000000, "message": "ok", "data": None})

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=stream)

        async with mock_client(handler) as client:
            with self.assertRaisesRegex(doubao.DoubaoTTSError, "产物为空"):
                await doubao._synthesize_once("x", "v", 0, client=client)

    async def test_synth_chunk_retries_then_writes_file(self) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(500, text="boom")  # 第一次失败,验证退避重试
            return httpx.Response(200, content=OK_STREAM)

        transport = httpx.MockTransport(handler)  # 每次尝试新建 client,共享 transport
        with TemporaryDirectory() as tmp, \
                patch.object(doubao.settings, "tts_doubao_api_key", "sk-1"), \
                patch.object(doubao.settings, "tts_chunk_max_retries", 2), \
                patch.object(doubao, "_build_client",
                             lambda: httpx.AsyncClient(transport=transport)):
            out = Path(tmp) / "001.mp3"
            await doubao.synth_chunk("重试一次", "zh_male_m191_uranus_bigtts", 2, out)
            self.assertEqual(len(calls), 2)
            self.assertEqual(out.read_bytes(), b"\xff\xfb part1 part2")
            self.assertFalse(out.with_suffix(".mp3.part").exists())

    async def test_synth_chunk_atomic_write_and_credentials(self) -> None:
        with TemporaryDirectory() as tmp, \
                patch.object(doubao.settings, "tts_doubao_api_key", ""), \
                patch.object(doubao.settings, "tts_doubao_app_key", ""), \
                patch.object(doubao.settings, "tts_doubao_access_key", ""):
            out = Path(tmp) / "001.mp3"
            with self.assertRaisesRegex(RuntimeError, "TTS_DOUBAO_API_KEY"):
                await doubao.synth_chunk("x", "v", 0, out)
            self.assertFalse(out.exists())
            self.assertFalse(out.with_suffix(".mp3.part").exists())


if __name__ == "__main__":
    unittest.main()
