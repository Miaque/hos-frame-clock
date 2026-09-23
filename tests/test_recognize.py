import asyncio
import os
import unittest
from datetime import datetime
from importlib import reload
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import ClientConnectionError
from paddleocr import (
    AsyncPaddleOCRClient,
    AuthError,
    Model,
    PollTimeoutError,
    RateLimitError,
    RequestTimeoutError,
)
from PIL import Image
from pydantic import ValidationError
from pydantic_settings import SettingsError

from hos_frame_clock import (
    OCRRateLimitedError,
    OCRServiceError,
    config,
    recognize_frame,
)


def frame_bytes():
    output = BytesIO()
    with Image.new("RGB", (1728, 536), "green") as image:
        image.paste("red", (1382, 0, 1694, 65))
        image.save(output, format="PNG")
    return output.getvalue()


def ocr_result(texts):
    return SimpleNamespace(pages=[SimpleNamespace(pruned_result={"rec_texts": texts})])


def use_tokens(*tokens, concurrency=1):
    return patch.object(config, 'settings', config.Settings(
        PADDLEOCR_TOKENS=list(tokens), PADDLEOCR_CONCURRENCY_PER_TOKEN=concurrency,
    ))


def sdk_patch(handler):
    class FakeClient:
        def __init__(self, *, token, base_url, request_timeout, poll_timeout):
            self.token = token
            self.base_url = base_url
            self.request_timeout = request_timeout
            self.poll_timeout = poll_timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def ocr(self, **kwargs):
            return await handler(self, **kwargs)

    return patch("hos_frame_clock.AsyncPaddleOCRClient", FakeClient)


class RecognitionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(use_tokens('test-token'))

    async def test_automatic_token_lookup_and_precedence(self):
        with TemporaryDirectory() as directory:
            previous = Path.cwd()
            original_settings = config.settings
            project = Path(directory)
            (project / '.env').write_text('PADDLEOCR_TOKENS=["file-token"]\n', encoding='utf-8')
            (project / 'child').mkdir()
            (project / 'child' / '.env').write_text(
                'PADDLEOCR_TOKENS=["local-token"]\nOTHER_APP_SETTING=value\n',
                encoding='utf-8',
            )
            try:
                os.chdir(project / 'child')
                for environment, expected in (
                    ({}, 'local-token'),
                    ({'PADDLEOCR_TOKENS': '["env-token"]'}, 'env-token'),
                ):
                    async def handler(client, expected=expected, **_):
                        self.assertEqual(client.token, expected)
                        raise AuthError("认证失败")
                    with patch.dict(os.environ, environment, clear=True), sdk_patch(handler):
                        reload(config)
                        with self.assertRaises(OCRServiceError):
                            await recognize_frame(frame_bytes())
                        self.assertEqual(dict(os.environ), environment)
                with patch.dict(os.environ, {'PADDLEOCR_CONCURRENCY_PER_TOKEN': '2'}, clear=True):
                    reload(config)
                    self.assertEqual(config.settings.concurrency_per_token, 2)
                for environment in (
                    {'PADDLEOCR_TOKENS': '[]'},
                    {'PADDLEOCR_TOKENS': '[""]'},
                ):
                    with patch.dict(os.environ, environment, clear=True), self.assertRaises(ValueError):
                        reload(config)
                        await recognize_frame(frame_bytes())
                for concurrency in ('0', '-1', 'abc', '1.5'):
                    environment = {'PADDLEOCR_CONCURRENCY_PER_TOKEN': concurrency}
                    with patch.dict(os.environ, environment, clear=True), self.assertRaises(ValidationError):
                        reload(config)
                (project / 'child' / '.env').unlink()
                with patch.dict(os.environ, {}, clear=True):
                    reload(config)
                    self.assertEqual(config.settings.tokens, [])
                    self.assertEqual(config.settings.concurrency_per_token, 1)
                for content in ('PADDLEOCR_TOKENS=[]\n', 'PADDLEOCR_TOKENS\n', 'OTHER_APP_SETTING=value\n'):
                    (project / 'child' / '.env').write_text(content, encoding='utf-8')
                    with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
                        reload(config)
                        await recognize_frame(frame_bytes())
                for content in ('PADDLEOCR_TOKENS=\n', 'PADDLEOCR_TOKENS=local-token\n'):
                    (project / 'child' / '.env').write_text(content, encoding='utf-8')
                    with patch.dict(os.environ, {}, clear=True), self.assertRaises(SettingsError):
                        reload(config)
            finally:
                os.chdir(previous)
                config.settings = original_settings

    async def test_configuration_is_loaded_once(self):
        with patch.object(config, 'settings', config.Settings(PADDLEOCR_TOKENS=['initial-token'])):
            async def handler(client, **_):
                self.assertEqual(client.token, 'initial-token')
                raise AuthError("认证失败")

            with patch.dict(os.environ, {'PADDLEOCR_TOKENS': '["changed-token"]'}), sdk_patch(handler):
                for _ in range(2):
                    with self.assertRaises(OCRServiceError):
                        await recognize_frame(frame_bytes())

    def gated_service(self, started, gate):
        async def handler(client, **_):
            started.append(client.token)
            await gate.wait()
            return ocr_result(["2026-09-14 05:25:36"])

        return sdk_patch(handler)

    async def test_calls_wait_for_a_free_token(self):
        for tokens, concurrency, calls in (
            (['token-a', 'token-b', 'token-c'], 1, 4),
            (['token-a'], 2, 3),
        ):
            with self.subTest(tokens=tokens, concurrency=concurrency):
                started, gate = [], asyncio.Event()
                with use_tokens(*tokens, concurrency=concurrency), self.gated_service(started, gate):
                    tasks = [asyncio.create_task(recognize_frame(frame_bytes())) for _ in range(calls)]
                    await asyncio.sleep(0.2)
                    self.assertEqual(sorted(started), sorted(tokens * concurrency))
                    gate.set()
                    results = await asyncio.gather(*tasks)
                self.assertEqual(len(started), calls)
                self.assertEqual({r.timestamp.isoformat() for r in results}, {"2026-09-14T05:25:36"})

    async def test_queue_wait_counts_toward_timeout(self):
        started, gate = [], asyncio.Event()
        with use_tokens('token-a'), self.gated_service(started, gate):
            first = asyncio.create_task(recognize_frame(frame_bytes()))
            await asyncio.sleep(0.05)
            with self.assertRaises(TimeoutError):
                await recognize_frame(frame_bytes(), timeout=0.15)
            gate.set()
            self.assertIsNotNone(await first)
        self.assertEqual(started, ["token-a"])

    async def test_token_is_released_after_failure(self):
        calls = 0

        async def handler(client, **_):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise AuthError("认证失败")
            return ocr_result(["2026-09-14 05:25:36"])

        with use_tokens('token-a'), sdk_patch(handler):
            with self.assertRaises(OCRServiceError):
                await recognize_frame(frame_bytes())
            self.assertIsNotNone(await recognize_frame(frame_bytes(), timeout=1))

    async def test_sdk_receives_crop_and_returns_result_directly(self):
        expected_size = (936, 195)
        paths = []

        async def handler(client, *, file_path, model, options):
            self.assertEqual(client.token, "test-token")
            self.assertEqual(client.base_url, "https://paddleocr.aistudio-app.com")
            self.assertEqual(client.request_timeout, 10)
            self.assertEqual(client.poll_timeout, 10)
            self.assertEqual(model, Model.PP_OCRV6)
            self.assertFalse(options.use_doc_orientation_classify)
            self.assertFalse(options.use_doc_unwarping)
            self.assertFalse(options.use_textline_orientation)
            paths.append(Path(file_path))
            with Image.open(file_path) as crop:
                self.assertEqual(crop.format, "PNG")
                self.assertEqual(crop.size, expected_size)
                if expected_size == (936, 195):
                    self.assertEqual(crop.getpixel((0, 0)), (255, 0, 0))
                    self.assertEqual(crop.getpixel((935, 194)), (255, 0, 0))
            return ocr_result(["2026-09-14 05:25:36"])

        with sdk_patch(handler):
            result = await recognize_frame(frame_bytes())
            self.assertEqual(result.timestamp, datetime(2026, 9, 14, 5, 25, 36))
            self.assertEqual(result.raw_text, "2026-09-14 05:25:36")
            expected_size = (2592, 804)
            self.assertIsNotNone(await recognize_frame(frame_bytes(), crop_box=(0.5, 0.5, 1, 1)))
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(not path.exists() for path in paths))

    async def test_official_sdk_ocr_returns_parsed_pages(self):
        async def submit_file(_client, model, file_path, optional_payload, **_):
            self.assertEqual(model, "PP-OCRv6")
            self.assertTrue(Path(file_path).is_file())
            self.assertEqual(optional_payload["useTextlineOrientation"], False)
            return "job-1"

        jsonl = [{"result": {"ocrResults": [
            {"prunedResult": {"rec_texts": ["2026-09-14 05:25:36"]}}
        ]}}]
        with (
            patch("hos_frame_clock.AsyncPaddleOCRClient", AsyncPaddleOCRClient),
            patch("paddleocr._api_client.async_client.AsyncHTTPClient.__aenter__", new_callable=AsyncMock),
            patch("paddleocr._api_client.async_client.AsyncHTTPClient.close", new_callable=AsyncMock),
            patch("paddleocr._api_client.async_client.AsyncHTTPClient.submit_file", submit_file),
            patch("paddleocr._api_client.async_client.AsyncPoller.poll_until_done",
                  new_callable=AsyncMock, return_value=(jsonl, {})) as poll,
        ):
            result = await recognize_frame(frame_bytes())
        self.assertEqual(result.timestamp.isoformat(), "2026-09-14T05:25:36")
        poll.assert_awaited_once_with("job-1")

    async def test_temporary_file_is_removed_after_failure_and_timeout(self):
        paths = []

        async def handler(client, *, file_path, **_):
            paths.append(Path(file_path))
            if len(paths) == 1:
                raise AuthError("认证失败")
            await asyncio.sleep(1)

        with sdk_patch(handler):
            with self.assertRaises(OCRServiceError):
                await recognize_frame(frame_bytes())
            with self.assertRaises(TimeoutError):
                await recognize_frame(frame_bytes(), timeout=0.15)
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(not path.exists() for path in paths))

    async def run_service(self, *, texts=None, result=None, error=None, delay=0, timeout=10):
        async def handler(client, **_):
            if delay:
                await asyncio.sleep(delay)
            if error:
                raise error
            return result if result is not None else ocr_result(texts)

        with sdk_patch(handler):
            return await recognize_frame(frame_bytes(), timeout=timeout)

    async def test_missing_invalid_and_ambiguous_times(self):
        for texts in ([], ["IPC"], ["05:25:36"], ["2026-O9-14 05:25:36"],
                      ["2026-02-30 05:25:36"], ["2026-09-14T05:25:36"],
                      ["2026-09-14 05:25:36.123"],
                      ["2026-09-14 05:25:36", "2026-09-14 05:25:37"]):
            with self.subTest(texts=texts):
                self.assertIsNone(await self.run_service(texts=texts))

    async def test_date_and_time_without_whitespace(self):
        result = await self.run_service(texts=["2026-08-1505:20:17"])
        self.assertIsNotNone(result)
        self.assertEqual(result.timestamp.isoformat(), "2026-08-15T05:20:17")
        self.assertEqual(result.raw_text, "2026-08-15 05:20:17")
        for text, expected in (
            ("2026-09-2121:50:35", "2026-09-21 21:50:35"),
            ("2026-09-21 21:50:45", "2026-09-21 21:50:45"),
            ("2026-09-2121:49:29&#x20;", "2026-09-21 21:49:29"),
            ("2026-09-2121:49:30&#x20;", "2026-09-21 21:49:30"),
        ):
            with self.subTest(text=text):
                result = await self.run_service(texts=[text])
                self.assertIsNotNone(result)
                self.assertEqual(result.raw_text, expected)
        for texts in (
            ["2026-02-3005:20:17"],
            ["2026-08-1505:20:17", "2026-08-1505:20:18"],
            ["2026-08-1505:20:17.123"],
            ["2026-08-15005:20:17"],
        ):
            with self.subTest(texts=texts):
                self.assertIsNone(await self.run_service(texts=texts))

    async def test_real_camera_overlay_separator_recognitions(self):
        for text, expected in (
            ("2026-09-21-21:51:23", "2026-09-21T21:51:23"),
            ("2026-09-22.09:42:54", "2026-09-22T09:42:54"),
            ("2026-09-22:09:43:12", "2026-09-22T09:43:12"),
            ("2026-09-22.09.44:25", "2026-09-22T09:44:25"),
            ("2026-09-22-09:44:44", "2026-09-22T09:44:44"),
            ("2026-09-22.09:45:02", "2026-09-22T09:45:02"),
            ("2026-09-22-17:04:40", "2026-09-22T17:04:40"),
            ("2026-09-22-17:04:41", "2026-09-22T17:04:41"),
        ):
            with self.subTest(text=text):
                result = await self.run_service(texts=[text])
                self.assertIsNotNone(result)
                self.assertEqual(result.timestamp.isoformat(), expected)
                self.assertEqual(result.raw_text, text)
        for text in (
            "12026-09-2217:04:21",
            "2026-09-22 17113-18",
            "2026-02-30.09:42:54",
        ):
            with self.subTest(text=text):
                self.assertIsNone(await self.run_service(texts=[text]))
        self.assertIsNone(
            await self.run_service(texts=["2026-09-22.09:42:54", "2026-09-22-09:42:55"])
        )

    async def test_split_text_whitespace_and_duplicate_time(self):
        result = await self.run_service(texts=["2026 - 09 - 14", "05 : 25 : 36"])
        self.assertEqual(result.timestamp, datetime(2026, 9, 14, 5, 25, 36))
        self.assertEqual(result.raw_text, "2026 - 09 - 14\n05 : 25 : 36")
        duplicate = await self.run_service(texts=["2026-09-14 05:25:36"] * 2)
        self.assertEqual(duplicate.timestamp, result.timestamp)

    async def test_sdk_failures_and_invalid_results_are_not_no_match(self):
        for error in (AuthError("认证失败"), ClientConnectionError("连接失败"),
                      RequestTimeoutError("请求超时"), PollTimeoutError("job-1", 10),
                      RateLimitError("限流")):
            with self.subTest(error=type(error).__name__):
                expected = TimeoutError if isinstance(error, (RequestTimeoutError, PollTimeoutError)) else OCRServiceError
                with self.assertRaises(expected):
                    await self.run_service(error=error)
        for result in (SimpleNamespace(pages=None),
                       SimpleNamespace(pages=[SimpleNamespace(pruned_result={})]),
                       ocr_result("2026-09-14 05:25:36")):
            with self.subTest(result=result), self.assertRaises(OCRServiceError):
                await self.run_service(result=result)

    async def test_rate_limiting_raises_a_distinguishable_error(self):
        with self.assertRaises(OCRRateLimitedError) as limited:
            await self.run_service(error=RateLimitError("限流"))
        self.assertIsInstance(limited.exception, OCRServiceError)
        with self.assertRaises(OCRServiceError) as other:
            await self.run_service(error=AuthError("认证失败"))
        self.assertNotIsInstance(other.exception, OCRRateLimitedError)

    async def test_total_timeout_and_cancellation(self):
        with self.assertRaises(TimeoutError):
            await self.run_service(delay=1, timeout=0.15)
        task = asyncio.create_task(self.run_service(delay=1))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_invalid_input_does_not_call_service(self):
        with patch("hos_frame_clock.AsyncPaddleOCRClient", side_effect=AssertionError("不应调用 SDK")):
            for kwargs in ({"image_bytes": b""}, {"timeout": 0},
                           {"timeout": float("nan")}, {"timeout": float("inf")},
                           {"crop_box": (0.5, 0, 0.5, 1)}, {"crop_box": (0, 0.5, 1, 0.5)},
                           {"crop_box": (-0.1, 0, 1, 1)}, {"crop_box": (0, 0, 1.1, 1)},
                           {"crop_box": (0, 0, float("nan"), 1)}, {"crop_box": (0, 0, 1)},
                           {"job_url": "https://example.com/other"}):
                arguments = {"image_bytes": frame_bytes(), **kwargs}
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    await recognize_frame(**arguments)
            with self.assertRaises(TypeError):
                await recognize_frame(frame_bytes(), token="test-token")
            with self.assertRaises(OSError):
                await recognize_frame(b"not an image")
