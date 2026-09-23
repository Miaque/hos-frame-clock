"""通过 PP-OCRv6 识别单路摄像头画面的日期时间。"""

import asyncio
import base64
import math
import re
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from aiohttp import ClientError, ClientSession, ClientTimeout
from paddleocr import (
    AsyncPaddleOCRClient,
    Model,
    OCROptions,
    PaddleOCRAPIError,
    PollTimeoutError,
    RateLimitError,
    RequestTimeoutError,
)
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


def _parse_result(result, *, self_hosted: bool = False) -> FrameTime | None:
    matches: dict[datetime, str] = {}
    try:
        pages = result["result"]["ocrResults"] if self_hosted else result.pages
        if not isinstance(pages, list):
            raise TypeError
        for page in pages:
            pruned = page["prunedResult"] if self_hosted else page.pruned_result
            texts = pruned["rec_texts"]
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
    except (AttributeError, KeyError, TypeError) as exc:
        raise OCRServiceError("OCR 结果格式无效") from exc
    if len(matches) == 1:
        timestamp, raw_text = next(iter(matches.items()))
        return FrameTime(timestamp, raw_text)
    return None


def _detail(phase: str, queued: float | None) -> str:
    """失败定位信息：出错阶段，以及排队等待 Token 的秒数。"""
    if queued is None:
        return f"阶段 {phase}"
    return f"阶段 {phase}，排队 {queued:.2f} 秒"


async def _self_hosted_ocr(crop: bytes, timeout: float) -> dict:
    url = config.settings.self_hosted_url
    if not url:
        raise ValueError("自部署模式需要设置 PADDLEOCR_SELF_HOSTED_URL")
    payload = {
        "file": base64.b64encode(crop).decode("ascii"),
        "fileType": 1,
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
        "useTextlineOrientation": False,
        "visualize": False,
    }
    async with (
        ClientSession(timeout=ClientTimeout(total=timeout)) as client,
        client.post(url, json=payload) as response,
    ):
        if response.status == 429:
            raise OCRRateLimitedError("自部署 OCR 服务端限流：HTTP 429")
        response.raise_for_status()
        try:
            result = await response.json()
        except ValueError as exc:
            raise OCRServiceError("自部署 OCR 响应不是有效 JSON") from exc
    if not isinstance(result, dict) or result.get("errorCode") != 0:
        code = result.get("errorCode") if isinstance(result, dict) else None
        raise OCRServiceError(f"自部署 OCR 返回错误码 {code!r}")
    return result


async def recognize_frame(
    image_bytes: bytes,
    *,
    timeout: float = 10.0,
    job_url: str = _JOB_URL,
    crop_box: tuple[float, float, float, float] = _CROP_BOX,
) -> FrameTime | None:
    """识别单帧 JPEG/PNG 图片中识别区域的日期时间。

    ``crop_box`` 为识别区域相对帧宽高的比例 ``(left, top, right, bottom)``，默认右上角。
    线上模式中，每个 ``PADDLEOCR_TOKENS`` 中的 Token 同时最多服务
    ``PADDLEOCR_CONCURRENCY_PER_TOKEN`` 个在途请求，全部占用时排队等待空闲 Token。
    ``timeout`` 覆盖图片准备、排队等待、任务提交、轮询和结果获取。
    超时抛出 ``TimeoutError``，服务失败抛出 ``OCRServiceError``，
    其中服务端限流抛出其子类 ``OCRRateLimitedError``，本库不因此自动重试。
    无效图片抛出参数校验异常或 Pillow 异常。
    调用方取消继续向上传播。线上模式临时写入裁剪图供 SDK 上传，调用结束后删除。
    自部署模式直接发送内存中的裁剪图。
    """
    if not isinstance(image_bytes, bytes) or not image_bytes:
        raise ValueError("image_bytes 必须为非空的 JPEG/PNG 编码字节")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout 必须为有限正数")
    if config.settings.backend == "official":
        api_path = "/api/v2/ocr/jobs"
        if not job_url.rstrip("/").endswith(api_path):
            raise ValueError("job_url 必须以 /api/v2/ocr/jobs 结尾")
        base_url = job_url.rstrip("/")[:-len(api_path)]
    deadline = monotonic() + timeout
    phase, queued = "准备图片", None
    try:
        async with asyncio.timeout(timeout):
            crop = await asyncio.to_thread(_crop, image_bytes, crop_box)
            if config.settings.backend == "self_hosted":
                phase = "调用自部署 OCR"
                result = await _self_hosted_ocr(crop, timeout)
                phase = "解析识别结果"
                return _parse_result(result, self_hosted=True)
            with TemporaryDirectory() as directory:
                phase = "写入临时图片"
                path = Path(directory) / "frame.png"
                path.write_bytes(crop)
                if monotonic() >= deadline:
                    raise TimeoutError
                phase = "等待空闲 Token"
                leased_at = monotonic()
                async with config.settings.lease_token() as token:
                    queued = monotonic() - leased_at
                    phase = "识别任务"
                    async with AsyncPaddleOCRClient(
                        token=token, base_url=base_url,
                        request_timeout=timeout, poll_timeout=timeout,
                    ) as client:
                        result = await client.ocr(
                            file_path=str(path), model=Model.PP_OCRV6,
                            options=OCROptions(
                                use_doc_orientation_classify=False,
                                use_doc_unwarping=False,
                                use_textline_orientation=False,
                            ),
                        )
                    phase = "解析识别结果"
                    return _parse_result(result)
    except (TimeoutError, RequestTimeoutError, PollTimeoutError) as exc:
        raise TimeoutError(f"OCR 识别超时（{_detail(phase, queued)}）") from exc
    except RateLimitError as exc:
        raise OCRRateLimitedError(f"OCR 服务端限流（{_detail(phase, queued)}）") from exc
    except PaddleOCRAPIError as exc:
        raise OCRServiceError(
            f"OCR 服务调用失败：{type(exc).__name__}（{_detail(phase, queued)}）"
        ) from exc
    except ClientError as exc:
        raise OCRServiceError(
            f"OCR 网络请求失败：{type(exc).__name__}（{_detail(phase, queued)}）"
        ) from exc
