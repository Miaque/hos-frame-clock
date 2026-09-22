import asyncio
from datetime import datetime
from email.parser import BytesParser
from email.policy import default
from functools import partial
from io import BytesIO
import json
from importlib import reload
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
from PIL import Image
from pydantic import ValidationError
from pydantic_settings import SettingsError

from hos_frame_clock import OCRRateLimitedError, OCRServiceError, recognize_frame
from hos_frame_clock import config


def frame_bytes():
    output = BytesIO()
    with Image.new("RGB", (1728, 536), "green") as image:
        image.paste("red", (1382, 0, 1694, 65))
        image.save(output, format="PNG")
    return output.getvalue()


def use_tokens(*tokens, concurrency=1):
    return patch.object(config, 'settings', config.Settings(
        PADDLEOCR_TOKENS=list(tokens), PADDLEOCR_CONCURRENCY_PER_TOKEN=concurrency,
    ))


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
                    async def handler(request):
                        self.assertEqual(request.headers['Authorization'], f'bearer {expected}')
                        return httpx.Response(401)
                    client = partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
                    with patch.dict(os.environ, environment, clear=True), patch('httpx.AsyncClient', client):
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
            async def handler(request):
                self.assertEqual(request.headers['Authorization'], 'bearer initial-token')
                return httpx.Response(401)

            client = partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
            with patch.dict(os.environ, {'PADDLEOCR_TOKENS': '["changed-token"]'}), patch('httpx.AsyncClient', client):
                for _ in range(2):
                    with self.assertRaises(OCRServiceError):
                        await recognize_frame(frame_bytes())

    def gated_service(self, started, gate):
        async def handler(request):
            if request.method == "POST":
                started.append(request.headers["Authorization"])
                await gate.wait()
                return httpx.Response(200, json={"data": {"jobId": "job-1"}})
            if request.url.host == "result.test":
                return httpx.Response(200, text=json.dumps({"result": {"ocrResults": [
                    {"prunedResult": {"rec_texts": ["2026-09-14 05:25:36"]}}
                ]}}))
            return httpx.Response(200, json={"data": {
                "state": "done", "resultUrl": {"jsonUrl": "https://result.test/data"}
            }})

        return patch("httpx.AsyncClient", partial(
            httpx.AsyncClient, transport=httpx.MockTransport(handler)
        ))

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
                    self.assertEqual(sorted(started), sorted(f"bearer {t}" for t in tokens * concurrency))
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
        self.assertEqual(started, ["bearer token-a"])

    async def test_token_is_released_after_failure(self):
        statuses = iter([401, 200])

        async def handler(request):
            if request.method == "POST":
                return httpx.Response(next(statuses), json={"data": {"jobId": "job-1"}})
            if request.url.host == "result.test":
                return httpx.Response(200, text=json.dumps({"result": {"ocrResults": [
                    {"prunedResult": {"rec_texts": ["2026-09-14 05:25:36"]}}
                ]}}))
            return httpx.Response(200, json={"data": {
                "state": "done", "resultUrl": {"jsonUrl": "https://result.test/data"}
            }})

        client = partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
        with use_tokens('token-a'), patch("httpx.AsyncClient", client):
            with self.assertRaises(OCRServiceError):
                await recognize_frame(frame_bytes())
            self.assertIsNotNone(await recognize_frame(frame_bytes(), timeout=1))

    async def test_crops_uploads_and_returns_datetime_and_original_text(self):
        expected_size = (936, 195)

        async def handler(request):
            if request.method == "POST":
                self.assertEqual(request.headers["Authorization"], "bearer test-token")
                message = BytesParser(policy=default).parsebytes(
                    b"Content-Type: " + request.headers["Content-Type"].encode()
                    + b"\r\n\r\n" + await request.aread()
                )
                parts = {part.get_param("name", header="content-disposition"): part
                         for part in message.iter_parts()}
                self.assertEqual(parts["model"].get_payload(decode=True), b"PP-OCRv6")
                with Image.open(BytesIO(parts["file"].get_payload(decode=True))) as crop:
                    self.assertEqual(crop.size, expected_size)
                    if expected_size == (936, 195):
                        self.assertEqual(crop.getpixel((0, 0)), (255, 0, 0))
                        self.assertEqual(crop.getpixel((935, 194)), (255, 0, 0))
                return httpx.Response(200, json={"data": {"jobId": "job-1"}})
            if request.url.host == "result.test":
                self.assertNotIn("Authorization", request.headers)
                return httpx.Response(200, text=json.dumps({"result": {"ocrResults": [
                    {"prunedResult": {"rec_texts": ["2026-09-14 05:25:36"]}}
                ]}}))
            return httpx.Response(200, json={"data": {
                "state": "done", "resultUrl": {"jsonUrl": "https://result.test/data"}
            }})

        client = partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
        with patch("httpx.AsyncClient", client):
            result = await recognize_frame(frame_bytes())
            self.assertEqual(result.timestamp, datetime(2026, 9, 14, 5, 25, 36))
            self.assertEqual(result.raw_text, "2026-09-14 05:25:36")
            expected_size = (2592, 804)
            self.assertIsNotNone(await recognize_frame(frame_bytes(), crop_box=(0.5, 0.5, 1, 1)))

    async def run_service(self, *, texts=None, content=None, state="done", status=200,
                          delay_at=None, timeout=10):
        async def handler(request):
            phase = "submit" if request.method == "POST" else (
                "download" if request.url.host == "result.test" else "poll"
            )
            if phase == delay_at:
                await asyncio.sleep(1)
            if phase == "submit":
                return httpx.Response(status, json={"data": {"jobId": "job-1"}})
            if phase == "poll":
                return httpx.Response(200, json={"data": {
                    "state": state, "resultUrl": {"jsonUrl": "https://result.test/data"}
                }})
            payload = content if content is not None else json.dumps({"result": {
                "ocrResults": [{"prunedResult": {"rec_texts": texts}}]
            }})
            return httpx.Response(200, text=payload)

        client = partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
        with patch("httpx.AsyncClient", client):
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

    async def test_split_text_whitespace_and_duplicate_time(self):
        result = await self.run_service(texts=["2026 - 09 - 14", "05 : 25 : 36"])
        self.assertEqual(result.timestamp, datetime(2026, 9, 14, 5, 25, 36))
        self.assertEqual(result.raw_text, "2026 - 09 - 14\n05 : 25 : 36")
        duplicate = await self.run_service(texts=["2026-09-14 05:25:36"] * 2)
        self.assertEqual(duplicate.timestamp, result.timestamp)

    async def test_service_and_protocol_failures_are_not_no_match(self):
        for kwargs in ({"status": 401}, {"status": 503}, {"state": "failed"},
                       {"state": "unexpected"}, {"content": ""},
                       {"content": "not json"}, {"content": "{}"},
                       {"texts": "2026-09-14 05:25:36"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(OCRServiceError):
                await self.run_service(**kwargs)

    async def test_rate_limiting_raises_a_distinguishable_error(self):
        with self.assertRaises(OCRRateLimitedError) as limited:
            await self.run_service(status=429)
        self.assertIsInstance(limited.exception, OCRServiceError)
        with self.assertRaises(OCRServiceError) as other:
            await self.run_service(status=503)
        self.assertNotIsInstance(other.exception, OCRRateLimitedError)

    async def test_timeout_covers_every_network_stage_and_poll_wait(self):
        for phase in ("submit", "poll", "download", None):
            with self.subTest(phase=phase), self.assertRaises(TimeoutError):
                await self.run_service(delay_at=phase, timeout=0.15,
                                       state="pending" if phase is None else "done")

    async def test_caller_cancellation_propagates(self):
        task = asyncio.create_task(self.run_service(delay_at="submit"))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_total_deadline_is_not_reset_between_requests(self):
        phases = []

        async def handler(request):
            phases.append(request.method)
            await asyncio.sleep(0.11)
            if request.method == "POST":
                return httpx.Response(200, json={"data": {"jobId": "job-1"}})
            return httpx.Response(200, json={"data": {
                "state": "done", "resultUrl": {"jsonUrl": "https://result.test/data"}
            }})

        client = partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
        with patch("httpx.AsyncClient", client), self.assertRaises(TimeoutError):
            await recognize_frame(frame_bytes(), timeout=0.3)
        self.assertEqual(phases, ["POST", "GET", "GET"])

    async def test_pending_running_done_sequence(self):
        states = iter(["pending", "running", "done"])

        async def handler(request):
            if request.method == "POST":
                return httpx.Response(200, json={"data": {"jobId": "job-1"}})
            if request.url.host == "result.test":
                return httpx.Response(200, text=json.dumps({"result": {"ocrResults": [
                    {"prunedResult": {"rec_texts": ["2024-02-29 23:59:59"]}}
                ]}}))
            return httpx.Response(200, json={"data": {
                "state": next(states), "resultUrl": {"jsonUrl": "https://result.test/data"}
            }})

        client = partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
        with patch("httpx.AsyncClient", client):
            result = await recognize_frame(frame_bytes())
        self.assertEqual(result.timestamp, datetime(2024, 2, 29, 23, 59, 59))

    async def test_invalid_input_does_not_call_service(self):
        with patch("httpx.AsyncClient", side_effect=AssertionError("Unexpected network")):
            for kwargs in ({"image_bytes": b""}, {"timeout": 0},
                           {"timeout": float("nan")}, {"timeout": float("inf")},
                           {"crop_box": (0.5, 0, 0.5, 1)}, {"crop_box": (0, 0.5, 1, 0.5)},
                           {"crop_box": (-0.1, 0, 1, 1)}, {"crop_box": (0, 0, 1.1, 1)},
                           {"crop_box": (0, 0, float("nan"), 1)}, {"crop_box": (0, 0, 1)}):
                arguments = {"image_bytes": frame_bytes(), **kwargs}
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    await recognize_frame(**arguments)
            with self.assertRaises(TypeError):
                await recognize_frame(frame_bytes(), token="test-token")
            with self.assertRaises(OSError):
                await recognize_frame(b"not an image")
