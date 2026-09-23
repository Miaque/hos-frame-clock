# hos-frame-clock

识别单帧图片右上角的日期时间。Python ≥3.12，通过 PP-OCRv6 API 调用，默认总超时 10 秒。

其他项目接入请阅读 [调用方使用文档](docs/caller-guide.md)。

## 使用

在调用项目的 `.env` 中选择后端（字段模板见 `.env.example`）。线上官方 API 使用：

```dotenv
PADDLEOCR_BACKEND=official
PADDLEOCR_TOKENS=["填写你的真实Token"]
```

自部署 OCR 使用：

```dotenv
PADDLEOCR_BACKEND=self_hosted
PADDLEOCR_SELF_HOSTED_URL=http://你的OCR服务地址:7070/ocr
```

两种模式的 Python 调用完全相同，直接运行：

```powershell
uv run python examples/recognize.py frame.jpg
```

示例会先将实际识别区域放大 3 倍后的图片保存到 `examples/tmp/frame_crop.png` 并输出路径，供手动打开查看，再调用 OCR。文件名随原图名称变化，同名图片会被覆盖；该临时目录不纳入 Git。输出耗时仅统计 OCR 调用，不包含裁剪图保存。需要改变识别区域时加 `--crop-box LEFT TOP RIGHT BOTTOM`（帧宽高的比例，默认 `0.80 0 0.98 0.12`）。

`.env` 已加入 Git 忽略规则。库会自动查找并读取配置，直接调用：

```python
import asyncio
from pathlib import Path

from hos_frame_clock import recognize_frame


async def main():
    result = await recognize_frame(
        Path("frame.jpg").read_bytes(),
        timeout=10.0,
    )
    if result is not None:
        print(result.timestamp.strftime("%Y-%m-%d %H:%M:%S"))
        print(result.raw_text)


asyncio.run(main())
```

运行于 asyncio；已有异步应用直接 `await recognize_frame(...)`。环境配置使用 `pydantic-settings` 的全局实例管理，在首次导入库时加载一次；请在导入前设置环境变量并确定工作目录，后续环境变量或 `.env` 修改需重启进程生效。`PADDLEOCR_BACKEND` 默认 `official`，可选 `self_hosted`；自部署模式需要 `PADDLEOCR_SELF_HOSTED_URL`，不需要 Token。线上模式的 `PADDLEOCR_TOKENS` 为 JSON 数组（如 `["token1","token2"]`），每个 Token 同时最多服务 `PADDLEOCR_CONCURRENCY_PER_TOKEN`（默认 1）个在途请求，全部占满时排队且等待计入 `timeout`；自部署模式不使用这两个配置，也不在库内按 Token 限流。配置优先级为进程环境变量、当前工作目录的 `.env`；不通过函数参数传入 Token。配置仅读取、不修改进程环境变量。线上模式会临时写入裁剪后的 PNG 供 SDK 上传，调用结束后删除；自部署模式直接发送裁剪图的 base64。`job_url` 仅在线上模式生效，默认是 `https://paddleocr.aistudio-app.com/api/v2/ocr/jobs`；`crop_box` 在两种模式下均生效。

## 契约

- 输入为 JPEG/PNG 编码的 `bytes`，不接收视频、URL、文件路径或 OpenCV 数组。
- 识别区域由 `crop_box=(left, top, right, bottom)` 指定，为帧宽高的比例，要求 `0 ≤ left < right ≤ 1`、`0 ≤ top < bottom ≤ 1`，否则抛 `ValueError`；默认 `(0.80, 0.0, 0.98, 0.12)`，即右上角横向 80%–98%、纵向 0%–12%。像素框左上向下取整，右下向上取整。1728×536 图片默认对应 `(1382, 0, 1694, 65)`，右下边界不包含在内。
- 裁剪后转 RGB，使用 LANCZOS 将宽高各放大 3 倍，再编码为 PNG 上传，不二值化。312×65 的裁剪区域上传为 936×195。无需安装 PaddleOCR 本地模型。
- 返回不可变 `FrameTime(timestamp: datetime, raw_text: str)`。`timestamp` 不含时区；`raw_text` 保留匹配处原文，但日期与时间紧邻时会补一个空格，OCR 把日期和时间分开时仍以换行连接。
- 接受 `YYYY-MM-DD HH:MM:SS`；OCR 将日期与时间之间的空格识别为 `-`、`.`、`:` 或省略空格时，也按完整时间解析。时分之间的 `.` 可作为分隔符；保留匹配到的 OCR 原文，只有日期与时间紧邻时在 `raw_text` 中补一个空格。校验日期合法性；不猜测修正多出的数字、`O/0` 等字符，不补日期，不接受 ISO `T` 或毫秒格式。
- 无有效时间，或出现多个不同的有效时间，返回 `None`。相同时间重复出现仍返回一个结果。
- HTTP、网络、远端任务失败或响应格式异常抛 `OCRServiceError`；服务端限流（HTTP 429）抛其子类 `OCRRateLimitedError`，按 `OCRServiceError` 捕获仍然生效，库不因此自动重试。超时抛内置 `TimeoutError`。参数错误抛 `ValueError`，图片解码错误可抛 Pillow 的 `OSError`。
- 10 秒总超时覆盖图片准备及所选后端的请求和结果解析；调用方可调整 `timeout`。线上模式调用一次 SDK 的 `ocr()` 并等待结果；自部署模式对 `/ocr` 发送一次 JSON 请求并直接解析返回结果。均不自动重试。
- 线上模式最大并发为 Token 数 × `PADDLEOCR_CONCURRENCY_PER_TOKEN`，超出的调用排队；调用结束即归还 Token。不做可用性检查，失败也不换 Token 重试。自部署模式不使用 Token 队列，并发控制由调用方和自部署服务负责。
- 调用方取消会继续传播 `asyncio.CancelledError`。本地超时或取消不代表服务端任务已取消；图片处理线程也可能在后台完成，但不会继续发起 OCR 请求。

## 开发与验证

```powershell
uv sync --locked
uv run python -m unittest discover -s tests -v
uv build
```

测试使用真实图片编码、SDK 模拟结果和本机 HTTP 服务，不会访问线上 OCR。真实验收时配置所选后端，按上面的示例读取实际帧；应确认返回样图时间且耗时符合要求。服务真实排队耗时不由本模块保证。

## Nexus 发布与安装

Nexus 需提供 PyPI hosted 仓库用于上传。安装建议使用包含该 hosted 仓库及公共依赖代理的 group 仓库。上传 URL 不加 `/simple/`，安装索引加 `/simple/`。参见 [Sonatype 配置说明](https://help.sonatype.com/en/configure-pypi-with-nexus.html)。

已确认上传地址为 `http://172.18.6.206:8081/repository/pypi-hosted/`。从源码目录运行下列脚本，自动构建并读取当前用户 `~/.m2/settings.xml` 中 `<server><id>nexus</id>` 的用户名和密码，传入发布子进程的环境变量；不打印或复制凭据到仓库。当前支持配置中的明文值，不解析 Maven 加密值或占位符。

```powershell
uv run python scripts/publish.py
```

依赖项目通过 Nexus 安装固定版本（认证由本机或 CI 的包管理器配置提供；其他公共依赖仍由 uv 默认索引提供，如需统一走 Nexus，应改为管理员提供的 group 索引）：

```powershell
uv add --index http://172.18.6.206:8081/repository/pypi-hosted/simple/ "hos-frame-clock==0.6.0"
```

每次发布先更新 `pyproject.toml` 的版本，并使用对应版本的构建产物路径。构建产物位于 `dist/`。本仓库不包含凭据。

## 接口依据

- [AI Studio OCR 文档](https://ai.baidu.com/ai-doc/AISTUDIO/Kmfl2ycs0)：OCR 结果中的 `prunedResult`。
- [PaddleOCR 官方 API 客户端测试](https://github.com/PaddlePaddle/PaddleOCR/blob/main/tests/api_client/test_core.py)：任务结果 JSONL 的 `result.ocrResults[].prunedResult.rec_texts` 结构。
- [PaddleOCR 官方 API Python SDK](https://www.paddleocr.ai/latest/version3.x/inference_deployment/serving/paddleocr_official_api/python.html)：使用 `AsyncPaddleOCRClient.ocr(file_path=..., model=Model.PP_OCRV6)` 一次调用取得完成后的结果。真实服务验收另行进行。

放大对照实验见 [OCR 放大实验记录](docs/ocr-scale-experiment.md)。
