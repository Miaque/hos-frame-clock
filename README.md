# hos-frame-clock

识别单帧图片右上角的日期时间。Python ≥3.12，通过 PP-OCRv6 API 调用，默认总超时 10 秒。

其他项目接入请阅读 [调用方使用文档](docs/caller-guide.md)。

## 使用

在调用项目的 `.env` 中填写 `PADDLEOCR_TOKENS`（JSON 数组，字段模板见 `.env.example`），直接运行：

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

运行于 asyncio；已有异步应用直接 `await recognize_frame(...)`。环境配置使用 `pydantic-settings` 的全局实例管理，在首次导入库时加载一次；请在导入前设置环境变量并确定工作目录，后续环境变量或 `.env` 修改需重启进程生效。`PADDLEOCR_TOKENS` 为 JSON 数组（如 `["token1","token2"]`），每个 Token 同时最多服务 `PADDLEOCR_CONCURRENCY_PER_TOKEN`（默认 1）个在途请求，所有 Token 都占满时新调用排队等待最先空闲的 Token，等待时间计入 `timeout`。配置优先级为进程环境变量、当前工作目录的 `.env`；Token 不能通过参数传入。使用 UTF-8 编码，忽略 `.env` 中其他应用的配置项；空数组或取到空 Token 会报 `ValueError`，不向低优先级配置回退；`PADDLEOCR_TOKENS` 不是合法 JSON 数组时，在首次导入阶段抛出 `pydantic_settings.SettingsError`，`PADDLEOCR_CONCURRENCY_PER_TOKEN` 不是 ≥1 的整数时抛出 `pydantic.ValidationError`。仅读取配置，不修改进程环境变量；调用期间会将裁剪后的 PNG 写入系统临时目录供官方 SDK 上传，并在调用结束时删除；不打印日志。可通过 `job_url` 覆盖 AI Studio 任务端点，地址须以 `/api/v2/ocr/jobs` 结尾，默认是 `https://paddleocr.aistudio-app.com/api/v2/ocr/jobs`；可通过 `crop_box` 指定识别区域。

## 契约

- 输入为 JPEG/PNG 编码的 `bytes`，不接收视频、URL、文件路径或 OpenCV 数组。
- 识别区域由 `crop_box=(left, top, right, bottom)` 指定，为帧宽高的比例，要求 `0 ≤ left < right ≤ 1`、`0 ≤ top < bottom ≤ 1`，否则抛 `ValueError`；默认 `(0.80, 0.0, 0.98, 0.12)`，即右上角横向 80%–98%、纵向 0%–12%。像素框左上向下取整，右下向上取整。1728×536 图片默认对应 `(1382, 0, 1694, 65)`，右下边界不包含在内。
- 裁剪后转 RGB，使用 LANCZOS 将宽高各放大 3 倍，再编码为 PNG 上传，不二值化。312×65 的裁剪区域上传为 936×195。无需安装 PaddleOCR 本地模型。
- 返回不可变 `FrameTime(timestamp: datetime, raw_text: str)`。`timestamp` 不含时区；`raw_text` 保留匹配处原文，但日期与时间紧邻时会补一个空格，OCR 把日期和时间分开时仍以换行连接。
- 接受 `YYYY-MM-DD HH:MM:SS`；OCR 将日期与时间之间的空格识别为 `-`、`.`、`:` 或省略空格时，也按完整时间解析。时分之间的 `.` 可作为分隔符；保留匹配到的 OCR 原文，只有日期与时间紧邻时在 `raw_text` 中补一个空格。校验日期合法性；不猜测修正多出的数字、`O/0` 等字符，不补日期，不接受 ISO `T` 或毫秒格式。
- 无有效时间，或出现多个不同的有效时间，返回 `None`。相同时间重复出现仍返回一个结果。
- HTTP、网络、远端任务失败或响应格式异常抛 `OCRServiceError`；服务端限流（HTTP 429）抛其子类 `OCRRateLimitedError`，按 `OCRServiceError` 捕获仍然生效，库不因此自动重试。超时抛内置 `TimeoutError`。参数错误抛 `ValueError`，图片解码错误可抛 Pillow 的 `OSError`。
- 10 秒覆盖图片准备、临时文件写入、等待空闲 Token、SDK 提交任务、轮询和结果下载/解析；调用方可调整 `timeout`。调用一次 SDK 的 `ocr()`，直接等待并解析任务结果；轮询由 SDK 负责，任务只提交一次，不自动重试。
- 最大并发为 Token 数 × `PADDLEOCR_CONCURRENCY_PER_TOKEN`，超出的调用排队，先到先得，谁先空闲用谁；调用结束（成功、失败、超时或取消）即归还 Token。不做可用性检查，失败也不换 Token 重试。排队等待绑定首个需要等待的事件循环，同一进程多次 `asyncio.run()` 不受支持。
- 调用方取消会继续传播 `asyncio.CancelledError`。本地超时或取消不代表服务端任务已取消；图片处理线程也可能在后台完成，但不会继续发起 OCR 请求。

## 开发与验证

```powershell
uv sync --locked
uv run python -m unittest discover -s tests -v
uv build
```

测试使用真实图片编码和 SDK 模拟结果，不会访问 OCR 服务。真实验收时设置 `PADDLEOCR_TOKENS`，按上面的示例读取实际帧；应确认返回样图时间且耗时符合要求。服务真实排队耗时不由本模块保证。

## Nexus 发布与安装

Nexus 需提供 PyPI hosted 仓库用于上传。安装建议使用包含该 hosted 仓库及公共依赖代理的 group 仓库。上传 URL 不加 `/simple/`，安装索引加 `/simple/`。参见 [Sonatype 配置说明](https://help.sonatype.com/en/configure-pypi-with-nexus.html)。

已确认上传地址为 `http://172.18.6.206:8081/repository/pypi-hosted/`。从源码目录运行下列脚本，自动构建并读取当前用户 `~/.m2/settings.xml` 中 `<server><id>nexus</id>` 的用户名和密码，传入发布子进程的环境变量；不打印或复制凭据到仓库。当前支持配置中的明文值，不解析 Maven 加密值或占位符。

```powershell
uv run python scripts/publish.py
```

依赖项目通过 Nexus 安装固定版本（认证由本机或 CI 的包管理器配置提供；其他公共依赖仍由 uv 默认索引提供，如需统一走 Nexus，应改为管理员提供的 group 索引）：

```powershell
uv add --index http://172.18.6.206:8081/repository/pypi-hosted/simple/ "hos-frame-clock==0.5.1"
```

每次发布先更新 `pyproject.toml` 的版本，并使用对应版本的构建产物路径。构建产物位于 `dist/`。本仓库不包含凭据。

## 接口依据

- [AI Studio OCR 文档](https://ai.baidu.com/ai-doc/AISTUDIO/Kmfl2ycs0)：OCR 结果中的 `prunedResult`。
- [PaddleOCR 官方 API 客户端测试](https://github.com/PaddlePaddle/PaddleOCR/blob/main/tests/api_client/test_core.py)：任务结果 JSONL 的 `result.ocrResults[].prunedResult.rec_texts` 结构。
- [PaddleOCR 官方 API Python SDK](https://www.paddleocr.ai/latest/version3.x/inference_deployment/serving/paddleocr_official_api/python.html)：使用 `AsyncPaddleOCRClient.ocr(file_path=..., model=Model.PP_OCRV6)` 一次调用取得完成后的结果。真实服务验收另行进行。

放大对照实验见 [OCR 放大实验记录](docs/ocr-scale-experiment.md)。
