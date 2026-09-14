# hos-frame-clock 调用方使用文档

适用版本：`0.1.1`。运行环境：Python ≥3.12、asyncio。

本库识别视频单帧右上角显示的完整日期时间。调用方负责抽帧，库负责裁剪、调用 PP-OCRv6、轮询及解析结果。无需部署 OCR 服务或安装本地模型，但运行环境需要能够访问 AI Studio 任务端点及其返回的结果下载地址。

## 1. 安装

在调用项目目录执行：

```powershell
uv add --index http://172.18.6.206:8081/repository/pypi-hosted/simple/ "hos-frame-clock==0.1.1"
```

安装包名为 `hos-frame-clock`，Python 导入名为 `hos_frame_clock`。需要能访问内网 Nexus；如仓库要求认证，在调用方的包管理器中配置。安装和调用不需要 Maven 配置，Maven 凭据仅用于本库的发布脚本。

## 2. 配置 OCR Token

在**调用项目自己的 `.env`** 中填写真实 Token：

```dotenv
PADDLEOCR_TOKEN=填写你的真实Token
```

在调用项目的 `.gitignore` 中添加：

```gitignore
.env
```

直接启动应用：

```powershell
uv run python main.py
```

未传 `token` 时，库优先读取进程环境变量 `PADDLEOCR_TOKEN`；不存在时，从当前工作目录向父目录查找最近的 `.env` 并读取。调用方无需手动加载。显式 `token` 参数优先级最高，已有空值会报错，不会回退。建议从调用项目根目录启动；不会按库的安装位置查找配置，也不会修改进程环境变量。部署时可直接注入同名环境变量。

## 3. 完整调用示例

将以下代码保存为调用项目的 `main.py`，准备一张完整视频帧 `frame.jpg`，按上面的启动命令运行：

```python
import asyncio
from pathlib import Path

from hos_frame_clock import OCRServiceError, recognize_frame


async def main() -> None:
    image_bytes = Path("frame.jpg").read_bytes()

    try:
        result = await recognize_frame(image_bytes, timeout=10.0)
    except TimeoutError:
        print("识别超时")
        return
    except OCRServiceError:
        print("OCR 服务调用失败")
        return
    except (ValueError, OSError):
        print("输入参数或图片无效")
        return

    if result is None:
        print("未识别到唯一有效时间")
        return

    print(result.timestamp.strftime("%Y-%m-%d %H:%M:%S"))
    print(result.raw_text)


if __name__ == "__main__":
    asyncio.run(main())
```

样图成功输出的时间为 `2026-09-14 05:25:36`。缺少 Token 时库抛出 `ValueError`；本地图片不存在时，示例会在调用库之前报错。

已有异步服务、任务或事件循环时，在其异步函数内直接 `await recognize_frame(...)`，不要在运行中的事件循环里嵌套调用 `asyncio.run()`。

## 4. 接口参数

公共接口：`await recognize_frame(image_bytes, *, token=None, timeout=10.0, job_url=默认任务地址)`。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `image_bytes` | `bytes` | 是 | 完整单帧图片的 JPEG/PNG 编码内容 |
| `token` | `str \| None` | 否 | 默认自动读取环境变量或 `.env`；显式传入时覆盖自动配置 |
| `timeout` | `float` | 否 | 全过程超时秒数，默认 10，必须为有限正数 |
| `job_url` | `str` | 否 | 兼容同一任务协议的端点地址，一般无需修改 |

默认任务地址：`https://paddleocr.aistudio-app.com/api/v2/ocr/jobs`。

输入不接受文件路径、图片 URL、视频文件或 OpenCV 数组。文件需要先读取为字节；已有 OpenCV 帧时，应由调用方先编码为 JPEG/PNG，再传入编码后的字节，而不是原始像素数组的 `tobytes()`。

## 5. 裁剪范围与画面要求

**传入完整帧，不要先裁剪右上角**，否则库会再次按比例裁剪，可能丢失时间文字。

- 以图片左上角为原点，识别区域为横向 80%–98%、纵向 0%–12%。
- 对于 1728×536 图片，像素区域为 `(1382, 0, 1694, 65)`，右下边界不包含在内。
- 该范围针对当前单路画面确定，适用于相同布局的同比例缩放。
- `0.1.1` 不提供裁剪区域参数。接入其他布局的摄像头前，需要确认时间文字完整落在该区域内。
- 支持的画面时间格式为 `YYYY-MM-DD HH:MM:SS`，可清理分隔符附近空白；不猜测替换 `O/0` 等字符，不补日期，不支持 ISO `T` 或毫秒格式。

## 6. 返回值与异常

成功返回不可变的 `FrameTime` 对象：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `timestamp` | `datetime` | 画面显示的日期时间，`tzinfo` 为 `None` |
| `raw_text` | `str` | 匹配到的 OCR 原文，可能包含空白或换行 |

`timestamp` 不是 Unix 时间戳，也不代表 UTC。需要时区或 epoch 秒时，调用方必须先根据摄像头配置明确时区，不能直接把无时区值当作 UTC 使用。返回接口字符串时可使用 `result.timestamp.strftime("%Y-%m-%d %H:%M:%S")`。

| 结果或异常 | 含义 | 调用方处理 |
| --- | --- | --- |
| `None` | 未找到合法完整时间，或存在多个不同的有效时间 | 按业务标记本帧未识别，不能当作服务故障 |
| `TimeoutError` | 超过总期限或发生 HTTP 超时 | 按业务决定是否稍后重试 |
| `OCRServiceError` | HTTP、网络、鉴权、任务失败或响应结构异常 | 记录异常类别并检查配置或服务状态 |
| `ValueError` | 空输入、空 Token、非法超时或不支持的图片格式等 | 修正输入 |
| `OSError` | 图片解码失败等 Pillow 错误 | 检查图片内容 |
| `asyncio.CancelledError` | 调用任务被取消 | 保留取消语义，让异常向上传播 |

相同时间重复出现仍可返回结果。错误信息不是稳定的细分错误码，不应通过匹配错误文本区分鉴权失败和其他服务错误。

## 7. 超时与重试

默认 10 秒覆盖图片准备、任务提交、轮询、结果下载和解析，不是每次 HTTP 请求分别获得 10 秒。调用前自行抽帧、下载原图、读取文件的时间不计入此期限。

每次调用提交一个远端任务，内部每 0.5 秒轮询一次，不自动重试，也不提供缓存或批量入口。超时或取消只结束本地等待，不代表远端任务已取消；调用方再次调用会提交新任务。多帧处理时由调用方管理调用频率和并发。

本库不写输出文件、不打印日志；是否保存时间、原文及耗时由调用方决定。
