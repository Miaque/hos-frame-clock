# 仓库协作指南

## 沟通与工作方式

- 使用简体中文沟通和编写说明，代码标识符沿用项目现有命名。
- 异常提示信息、代码注释和文档字符串（docstring）均使用简体中文；类型注解中的类型名称和代码标识符保持原样。
- 开始修改前检查 `git status --short`，保留用户已有的修改和未跟踪文件。
- 先读相关实现与测试，明确假设和验收条件；多步骤任务给出简短计划。影响接口或范围的歧义应先澄清。
- 只修改完成请求所必需的内容，沿用现有风格；优先简单实现，不引入未经需求支持的抽象、依赖或配置。
- 完成后说明改动、验证结果和未验证的边界；区分模拟测试、构建检查与真实服务验收。

## 项目定位与阅读入口

本项目是 Python 3.12+ 异步库，通过 AI Studio PP-OCRv6 识别单帧图片右上角的完整日期时间。视频解码和抽帧由调用方负责。

- 修改识别逻辑前阅读 `README.md` 的契约和 `src/hos_frame_clock/__init__.py`；公共接口为 `recognize_frame`、`FrameTime`、`OCRServiceError`。
- 涉及领域含义时阅读 `CONTEXT.md`，区分画面时间、视频播放进度和文件时间。
- 修改调用方式或公共契约时检查并同步 `docs/caller-guide.md`、`README.md` 和 `examples/recognize.py`。
- 需要设计依据或历史验收背景时阅读 `docs/design.md`；其中历史版本和验收记录不代表当前运行状态。
- 测试集中在 `tests/test_recognize.py`，发布入口为 `scripts/publish.py`。
- `.agents/skills/` 提供按任务使用的技能；`.specify/` 提供规格工作流。目前 `.specify/memory/constitution.md` 仍是占位模板，不作为已确认的项目规则。

## 修改时需保持的边界

- 输入保持为 JPEG/PNG 编码的 `bytes`；裁剪区域、取整规则和上传格式以现有契约为准。
- 画面时间不推断时区、不补日期、不猜测纠正 OCR 字符。无有效时间或存在多个不同有效时间返回 `None`。
- 保持无匹配、服务错误、超时和参数错误的区别；调用方取消继续传播。
- 总超时覆盖图片准备、任务提交、轮询及结果下载解析；保持任务只提交一次的语义。
- Token 优先级为显式参数、进程环境变量、当前工作目录的 `.env`；读取配置不修改进程环境。鉴权信息只发送到任务端点，不附带到结果下载请求。
- 库本身不写文件、不打印日志。凭据不得写入源码、测试、日志或提交；配置示例使用占位值。

## 开发与验证

在仓库根目录使用 PowerShell 和 `uv`：

```powershell
uv sync --locked
uv run ruff check src tests scripts examples
uv run ruff format --check src tests scripts examples
uv run python -m unittest discover -s tests -v
uv build
```

- 行为修改应增加或调整能验证需求的测试；修复缺陷时先确认测试能复现问题。
- 测试沿用 `unittest.IsolatedAsyncioTestCase`、真实图片编码和 HTTP 模拟响应，不依赖真实 OCR 凭据。
- 涉及包元数据、依赖或发布内容时执行构建检查；依赖变更同步维护 `pyproject.toml` 与 `uv.lock`。
- Ruff 已加入开发依赖，用于静态检查和格式检查；修改 Python 文件后运行上述 Ruff 命令。需要自动修复或格式化时，仅处理本次涉及的文件，并检查差异。
- 区分本次引入的问题与已有告警，不为通过检查而修改无关代码或削弱规则。Ruff 不替代类型检查，当前未配置独立类型检查工具。
- 纯文档修改检查内容、引用路径和差异即可。
- 真实 OCR 验收使用 `examples/recognize.py`，记录样图结果和实际耗时；单次成功不代表服务始终满足时限。

## Git 与发布

- 需要新建分支时默认使用 `feature/` 前缀。按明确文件范围暂存，提交前检查 staged diff。
- 用户要求提交时遵循 `GIT_COMMIT_RULES.md`：英文 Conventional Commits 类型、中文标题、空行及 1～3 行中文正文，内容严格对应实际提交。
- 发布时先阅读 `README.md` 的 Nexus 说明和 `scripts/publish.py`，核对版本及对应构建产物；该脚本会实际上传包，不作为普通验证命令运行。
- `.env`、本机 Maven 凭据和构建产物保持在版本控制之外。
