"""通过 AI Studio PP-OCRv6 识别单路摄像头画面的日期时间。"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
import json
import math
import re
from time import monotonic
from urllib.parse import quote

import httpx
from PIL import Image

from . import config

__all__ = ["FrameTime", "OCRRateLimitedError", "OCRServiceError", "recognize_frame"]


_JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
_CROP_BOX = (0.80, 0.0, 0.98, 0.12)
_TIME = re.compile(
    r"(?<![0-9A-Za-z])([0-9]{4})\s*-\s*([0-9]{2})\s*-\s*([0-9]{2})"
    r"\s*[-.:]?\s*([0-9]{2})\s*[:.]\s*([0-9]{2})\s*:\s*([0-9]{2})(?![0-9A-Za-z:.])"
)


@dataclass(frozen=True)
class FrameTime:
    """不含时区的画面时间及匹配的 OCR 文本。"""

    timestamp: datetime
    raw_text: str


class OCRServiceError(RuntimeError):
    """OCR 请求失败、远端任务失败或响应不符合契约。"""


class OCRRateLimitedError(OCRServiceError):
    """服务端限流（HTTP 429）。调用方应降低并发或退避后自行重试。"""


def _crop(
    image_bytes: bytes, crop_box: tuple[float, float, float, float] = _CROP_BOX
) -> bytes:
    left, top, right, bottom = crop_box
    if not all(math.isfinite(value) for value in crop_box) or not (
        0 <= left < right <= 1 and 0 <= top < bottom <= 1
    ):
        raise ValueError("crop_box 必须满足 0 <= left < right <= 1 且 0 <= top < bottom <= 1")
    with Image.open(BytesIO(image_bytes)) as image:
        if image.format not in {"JPEG", "PNG"}:
            raise ValueError("仅支持 JPEG 和 PNG 图片")
        width, height = image.size
        box = (math.floor(width * left), math.floor(height * top),
               math.ceil(width * right), math.ceil(height * bottom))
        with image.crop(box).convert("RGB") as crop:
            output = BytesIO()
            with crop.resize(
                (crop.width * 3, crop.height * 3), Image.Resampling.LANCZOS
            ) as enlarged:
                enlarged.save(output, format="PNG")
            return output.getvalue()


def _parse_result(content: str) -> FrameTime | None:
    matches: dict[datetime, str] = {}
    lines = [line for line in content.splitlines() if line.strip()]
    if not lines:
        raise OCRServiceError("OCR 结果文档为空")
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
                    raw_text = match.group()
                    if match.end(3) == match.start(4):
                        day_end = match.end(3) - match.start()
                        raw_text = f"{raw_text[:day_end]} {raw_text[day_end:]}"
                    matches.setdefault(timestamp, raw_text)
    except (KeyError, TypeError, ValueError) as exc:
        raise OCRServiceError("OCR 结果文档格式无效") from exc
    if len(matches) == 1:
        timestamp, raw_text = next(iter(matches.items()))
        return FrameTime(timestamp, raw_text)
    return None


def _detail(phase: str, queued: float | None) -> str:
    """失败定位信息：出错阶段，以及排队等待 Token 的秒数。"""
    if queued is None:
        return f"阶段 {phase}，未取得 Token"
    return f"阶段 {phase}，排队 {queued:.2f} 秒"


def _failure_payload(data: dict) -> str:
    """任务失败时的远端状态原文，去掉可能带签名的结果地址。"""
    text = json.dumps(
        {key: value for key, value in data.items() if key != "resultUrl"},
        ensure_ascii=False, default=str,
    )
    return text if len(text) <= 300 else f"{text[:300]}…"


def _job_data(response: httpx.Response) -> dict:
    try:
        data = response.json()["data"]
        if not isinstance(data, dict):
            raise TypeError
        return data
    except (KeyError, TypeError, ValueError) as exc:
        raise OCRServiceError("OCR 任务响应格式无效") from exc


async def recognize_frame(
    image_bytes: bytes,
    *,
    timeout: float = 10.0,
    job_url: str = _JOB_URL,
    crop_box: tuple[float, float, float, float] = _CROP_BOX,
) -> FrameTime | None:
    """识别单帧 JPEG/PNG 图片中识别区域的日期时间。

    ``crop_box`` 为识别区域相对帧宽高的比例 ``(left, top, right, bottom)``，默认右上角。
    每个 ``PADDLEOCR_TOKENS`` 中的 Token 同时最多服务 ``PADDLEOCR_CONCURRENCY_PER_TOKEN``
    个在途请求，全部占用时排队等待空闲 Token。
    ``timeout`` 覆盖图片准备、排队等待、任务提交、轮询和结果获取。
    超时抛出 ``TimeoutError``，服务失败抛出 ``OCRServiceError``，
    其中服务端限流抛出其子类 ``OCRRateLimitedError``，本库不因此自动重试。
    无效图片抛出参数校验异常或 Pillow 异常。
    调用方取消继续向上传播。凭据仅发送到任务端点。
    """
    if not isinstance(image_bytes, bytes) or not image_bytes:
        raise ValueError("image_bytes 必须为非空的 JPEG/PNG 编码字节")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout 必须为有限正数")
    phase, queued = "准备图片", None
    try:
        async with asyncio.timeout(timeout):
            crop = await asyncio.to_thread(_crop, image_bytes, crop_box)
            phase = "等待空闲 Token"
            leased_at = monotonic()
            async with (
                config.settings.lease_token() as token,
                httpx.AsyncClient(timeout=timeout) as client,
            ):
                queued = monotonic() - leased_at
                headers = {"Authorization": f"bearer {token}"}
                phase = "提交任务"
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
                    raise OCRServiceError(f"缺少 OCR 任务 ID（{_detail(phase, queued)}）")
                while True:
                    phase = "查询任务状态"
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
                            raise OCRServiceError(
                                f"OCR 结果 URL 缺失或无效（{_detail(phase, queued)}）"
                            )
                        phase = "下载识别结果"
                        response = await client.get(url, follow_redirects=True)
                        response.raise_for_status()
                        phase = "解析识别结果"
                        return await asyncio.to_thread(_parse_result, response.text)
                    if state == "failed":
                        raise OCRServiceError(
                            f"OCR 任务失败（{_detail(phase, queued)}）：{_failure_payload(data)}"
                        )
                    if state not in ("pending", "running"):
                        raise OCRServiceError(
                            f"未知的 OCR 任务状态 {state!r}（{_detail(phase, queued)}）"
                        )
                    phase = "等待轮询间隔"
                    await asyncio.sleep(0.5)
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise TimeoutError(f"OCR 识别超时（{_detail(phase, queued)}）") from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 429:
            raise OCRRateLimitedError(
                f"OCR 服务端限流：HTTP 429（{_detail(phase, queued)}）"
            ) from exc
        raise OCRServiceError(
            f"OCR HTTP 请求失败：HTTP {status}（{_detail(phase, queued)}）"
        ) from exc
    except httpx.HTTPError as exc:
        raise OCRServiceError(
            f"OCR HTTP 请求失败：{type(exc).__name__}（{_detail(phase, queued)}）"
        ) from exc
