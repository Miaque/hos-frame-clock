import asyncio
from datetime import datetime
from email.parser import BytesParser
from email.policy import default
from functools import partial
from io import BytesIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from hos_frame_clock import OCRServiceError, recognize_frame


def frame_bytes():
    output = BytesIO()
    with Image.new("RGB", (1728, 536), "green") as image:
        image.paste("red", (1382, 0, 1694, 65))
        image.save(output, format="PNG")
    return output.getvalue()


class RecognitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_automatic_token_lookup_and_precedence(self):
        with TemporaryDirectory() as directory:
            previous = Path.cwd()
            project = Path(directory)
            (project / '.env').write_text('PADDLEOCR_TOKEN="file-token"\n', encoding='utf-8')
            (project / 'child').mkdir()
            try:
                os.chdir(project / 'child')
                for environment, explicit, expected in (
                    ({}, {}, 'file-token'),
                    ({'PADDLEOCR_TOKEN': 'env-token'}, {}, 'env-token'),
                    ({'PADDLEOCR_TOKEN': 'env-token'}, {'token': 'explicit-token'}, 'explicit-token'),
                ):
                    async def handler(request):
                        self.assertEqual(request.headers['Authorization'], f'bearer {expected}')
                        return httpx.Response(401)
                    client = partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
                    with patch.dict(os.environ, environment, clear=True), patch('httpx.AsyncClient', client):
                        with self.assertRaises(OCRServiceError):
                            await recognize_frame(frame_bytes(), **explicit)
                        self.assertEqual(dict(os.environ), environment)
                (project / '.env').write_text('PADDLEOCR_TOKEN=\n', encoding='utf-8')
                with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
                    await recognize_frame(frame_bytes())
            finally:
                os.chdir(previous)

    async def test_crops_uploads_and_returns_datetime_and_original_text(self):
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
                    self.assertEqual(crop.size, (312, 65))
                    self.assertEqual(crop.getpixel((0, 0)), (255, 0, 0))
                    self.assertEqual(crop.getpixel((311, 64)), (255, 0, 0))
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
            result = await recognize_frame(frame_bytes(), token="test-token")
        self.assertEqual(result.timestamp, datetime(2026, 9, 14, 5, 25, 36))
        self.assertEqual(result.raw_text, "2026-09-14 05:25:36")

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
            return await recognize_frame(frame_bytes(), token="test-token", timeout=timeout)

    async def test_missing_invalid_and_ambiguous_times(self):
        for texts in ([], ["IPC"], ["05:25:36"], ["2026-O9-14 05:25:36"],
                      ["2026-02-30 05:25:36"], ["2026-09-14T05:25:36"],
                      ["2026-09-14 05:25:36.123"],
                      ["2026-09-14 05:25:36", "2026-09-14 05:25:37"]):
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
            await recognize_frame(frame_bytes(), token="test-token", timeout=0.3)
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
            result = await recognize_frame(frame_bytes(), token="test-token")
        self.assertEqual(result.timestamp, datetime(2024, 2, 29, 23, 59, 59))

    async def test_invalid_input_does_not_call_service(self):
        with patch("httpx.AsyncClient", side_effect=AssertionError("Unexpected network")):
            for kwargs in ({"image_bytes": b""}, {"token": ""}, {"timeout": 0},
                           {"timeout": float("nan")}, {"timeout": float("inf")}):
                arguments = {"image_bytes": frame_bytes(), "token": "test-token", **kwargs}
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    await recognize_frame(**arguments)
            with self.assertRaises(OSError):
                await recognize_frame(b"not an image", token="test-token")
