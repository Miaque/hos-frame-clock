"""Single-camera frame timestamp recognition through AI Studio PP-OCRv6."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
import json
import math
import os
import re
from urllib.parse import quote

import httpx
from dotenv import dotenv_values, find_dotenv
from PIL import Image

__all__ = ["FrameTime", "OCRServiceError", "recognize_frame"]

_JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
_TIME = re.compile(
    r"(?<![0-9A-Za-z])([0-9]{4})\s*-\s*([0-9]{2})\s*-\s*([0-9]{2})"
    r"\s+([0-9]{2})\s*:\s*([0-9]{2})\s*:\s*([0-9]{2})(?![0-9A-Za-z:.])"
)


@dataclass(frozen=True)
class FrameTime:
    """A timezone-naive displayed time and the original matching OCR text."""

    timestamp: datetime
    raw_text: str


class OCRServiceError(RuntimeError):
    """OCR request, remote job or response contract failure."""


def _crop(image_bytes: bytes) -> bytes:
    with Image.open(BytesIO(image_bytes)) as image:
        if image.format not in {"JPEG", "PNG"}:
            raise ValueError("Only JPEG and PNG images are supported")
        width, height = image.size
        box = (width * 80 // 100, 0, (width * 98 + 99) // 100,
               (height * 12 + 99) // 100)
        with image.crop(box).convert("RGB") as crop:
            output = BytesIO()
            crop.save(output, format="PNG")
            return output.getvalue()


def _parse_result(content: str) -> FrameTime | None:
    matches: dict[datetime, str] = {}
    lines = [line for line in content.splitlines() if line.strip()]
    if not lines:
        raise OCRServiceError("Empty OCR result document")
    try:
        for line in lines:
            results = json.loads(line)["result"]["ocrResults"]
            if not isinstance(results, list):
                raise TypeError
            for result in results:
                texts = result["prunedResult"]["rec_texts"]
                if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
                    raise TypeError
                for match in _TIME.finditer("\n".join(texts)):
                    try:
                        timestamp = datetime(*(int(part) for part in match.groups()))
                    except ValueError:
                        continue
                    matches.setdefault(timestamp, match.group())
    except (KeyError, TypeError, ValueError) as exc:
        raise OCRServiceError("Invalid OCR result document") from exc
    if len(matches) == 1:
        timestamp, raw_text = next(iter(matches.items()))
        return FrameTime(timestamp, raw_text)
    return None


def _job_data(response: httpx.Response) -> dict:
    try:
        data = response.json()["data"]
        if not isinstance(data, dict):
            raise TypeError
        return data
    except (KeyError, TypeError, ValueError) as exc:
        raise OCRServiceError("Invalid OCR job response") from exc


async def recognize_frame(
    image_bytes: bytes,
    *,
    token: str | None = None,
    timeout: float = 10.0,
    job_url: str = _JOB_URL,
) -> FrameTime | None:
    """Recognize the fixed top-right ROI of one JPEG/PNG frame.

    ``timeout`` covers image preparation, submission, polling and retrieval.
    Raises ``TimeoutError`` on expiry, ``OCRServiceError`` on service failures,
    and input validation or Pillow errors for invalid images.
    Cancellation propagates. Credentials are sent only to the jobs endpoint.
    """
    if not isinstance(image_bytes, bytes) or not image_bytes:
        raise ValueError("image_bytes must be non-empty JPEG/PNG bytes")
    if token is None:
        token = os.environ.get("PADDLEOCR_TOKEN")
        if token is None:
            env_file = find_dotenv(usecwd=True)
            token = dotenv_values(env_file, encoding="utf-8-sig").get("PADDLEOCR_TOKEN") if env_file else None
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Set PADDLEOCR_TOKEN in .env or environment, or pass a non-empty token")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    headers = {"Authorization": f"bearer {token}"}
    try:
        async with asyncio.timeout(timeout):
            crop = await asyncio.to_thread(_crop, image_bytes)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    job_url,
                    headers=headers,
                    data={
                        "model": "PP-OCRv6",
                        "optionalPayload": json.dumps({
                            "useDocOrientationClassify": False,
                            "useDocUnwarping": False,
                            "useTextlineOrientation": False,
                        }),
                    },
                    files={"file": ("frame.png", crop, "image/png")},
                )
                response.raise_for_status()
                job_id = _job_data(response).get("jobId")
                if not isinstance(job_id, str) or not job_id:
                    raise OCRServiceError("Missing OCR job ID")
                while True:
                    response = await client.get(
                        f"{job_url.rstrip('/')}/{quote(job_id, safe='')}", headers=headers
                    )
                    response.raise_for_status()
                    data = _job_data(response)
                    state = data.get("state")
                    if state == "done":
                        result_url = data.get("resultUrl")
                        url = result_url.get("jsonUrl") if isinstance(result_url, dict) else None
                        if not isinstance(url, str) or not url.startswith("https://"):
                            raise OCRServiceError("Missing or invalid OCR result URL")
                        response = await client.get(url, follow_redirects=True)
                        response.raise_for_status()
                        return await asyncio.to_thread(_parse_result, response.text)
                    if state == "failed":
                        raise OCRServiceError("OCR job failed")
                    if state not in ("pending", "running"):
                        raise OCRServiceError("Unknown OCR job state")
                    await asyncio.sleep(0.5)
    except httpx.TimeoutException as exc:
        raise TimeoutError("OCR recognition timed out") from exc
    except httpx.HTTPError as exc:
        raise OCRServiceError("OCR HTTP request failed") from exc
