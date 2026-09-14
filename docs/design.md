# 视频帧时间识别模块设计

状态：模块已实现。0.1.1 调整为自动读取 Token：显式参数优先，其次进程环境变量，再从当前工作目录向上查找最近的 `.env`；不修改进程环境变量。以下历史验收记录保留当时的调用方式。

## 已确认

- 本项目作为可供其他项目依赖的模块。
- 输入为调用方已经提取的单帧图片，视频解码和抽帧由调用方负责。
- 接口接收 JPEG/PNG 编码的图片字节 `bytes`。
- 成功返回 Python `datetime` 和匹配的 OCR 原文，不自行推断时区。
- 提供 `async/await` 异步接口，内部完成提交任务、轮询和获取识别结果。
- 未识别到有效日期时间返回 `None`；鉴权失败、服务失败和超时抛出异常。
- 默认总超时为 10 秒，覆盖提交、轮询和获取结果的整个识别过程；允许调用方调整。
- Python 最低版本保持 3.12。
- 通过 Nexus 私有 Python 包仓库发布和分发。
- 时间解析只清理空白，按 `YYYY-MM-DD HH:MM:SS` 解析并校验日期合法性，不猜测替换 OCR 字符；发现多个不同的有效时间时返回 `None`。
- 识别图片右上角叠加的完整日期时间；缺少日期时不自动补齐当天日期。
- OCR 服务使用 AI Studio PP-OCRv6，参考文档：https://ai.baidu.com/ai-doc/AISTUDIO/Kmfl2ycs0 。
- 用户提供的调用示例通过 `/api/v2/ocr/jobs` 提交任务，轮询状态，并在完成后下载 JSONL 结果。

## 当前单路画面的裁剪方案

- 用户授权根据样图确定区域。样图分辨率为 1728×536，目视时间为 `2026-09-14 05:25:36`，格式为 `YYYY-MM-DD HH:MM:SS`。
- 默认裁剪横向 80%–98%、纵向 0%–12% 的区域，坐标以左上角为原点。左上边界向下取整，右下边界向上取整；样图对应 `(1382, 0, 1694, 65)`，右下边界不包含在内。
- 该区域完整包含时间文字并保留边缘留白。使用相对坐标适配同比例缩放；不保证摄像头叠字布局改变后仍然适用。
- 首版使用这一固定区域，不要求调用方传入裁剪参数。先裁剪再提交 OCR，保留颜色和原始裁剪分辨率，不默认二值化或放大。
- 当前仅完成目视区域判断，尚未进行真实 OCR 验证。

## 实施前需核实的外部信息

- 官方文档与客户端测试确认文字路径为 `result.ocrResults[].prunedResult.rec_texts`；真实响应仍需凭据验证。
- Nexus 上传地址已确认：`http://172.18.6.206:8081/repository/pypi-hosted/`，安装索引为该地址加 `simple/`；凭据使用当前用户 Maven `settings.xml` 中的 `nexus` server。
- 本地 OCR 示例通过 `uv run --env-file .env` 加载 `PADDLEOCR_TOKEN`，模块仍接收调用方传入的 Token。
- 真实 OCR 验证需要可用的服务凭据；10 秒内能否完成需实测。

## 本地验证记录

- 2026-09-14：9 项 unittest 测试通过，覆盖裁剪上传、时间解析、任务状态流转、错误、累计总超时和取消。
- wheel 与 sdist 构建成功，独立环境安装 wheel 后版本为 0.1.0，公共异步接口与类型标记可用。
- Nexus hosted 地址匿名读取返回 HTTP 200，当前包的 simple 路径返回 HTTP 404；这不代表已有上传权限。
- 当前进程未配置 `PADDLEOCR_TOKEN`、`UV_PUBLISH_USERNAME`、`UV_PUBLISH_PASSWORD`，未执行真实 OCR 调用或 Nexus 上传。
- 后续配置：已添加被 Git 忽略的 `.env` 空模板；OCR Token 尚未填写，真实识别待验证。
- 已使用 `~/.m2/settings.xml` 的 `nexus` 凭据将 0.1.0 wheel/sdist 上传到上述 Nexus hosted 仓库，并通过该索引在独立环境安装，确认版本 0.1.0 和异步接口可用。两种发布产物均不包含 `.env` 或 `settings.xml`。
- 真实 OCR 验收：用户填写 `.env` 后，通过 `uv run --env-file .env python examples/recognize.py` 识别所提供的 1728×536 样图，返回 `2026-09-14 05:25:36`，OCR 原文一致，与目视时间相符。单次全过程耗时 1.719 秒，未超过默认 10 秒期限。该结果证明本次样图与真实服务链路成功，不代表所有请求均能在 10 秒内完成。
- 发布包验收：通过 `uv run --isolated --no-project --env-file .env --refresh-package hos-frame-clock --index http://172.18.6.206:8081/repository/pypi-hosted/simple/ --with hos-frame-clock==0.1.0` 运行样图示例，使用 Nexus 已发布包而非本地 editable 包，再次返回 `2026-09-14 05:25:36`，耗时 1.000 秒。
