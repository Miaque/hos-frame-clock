# 更新记录

## 0.6.0（2026-09-23）

### 新增

- 新增 `PADDLEOCR_BACKEND=self_hosted` 与 `PADDLEOCR_SELF_HOSTED_URL`，通过环境配置切换自部署 `/ocr` 服务，保持 `recognize_frame` 的调用和返回值不变。自部署模式不需要 Token，也不使用线上 Token 队列。

## 0.5.0（2026-09-22）

### 新增

- 服务端限流（HTTP 429）抛出新异常 `OCRRateLimitedError`，它是 `OCRServiceError` 的子类，既有按 `OCRServiceError` 捕获的调用方不受影响。库仍不自动重试，调用方据此降低并发或退避后自行重试；同时捕获两者时须把 `OCRRateLimitedError` 排在前面。

## 0.4.1（2026-09-22）

### 修复

- 当 OCR 日期与时间之间缺少空白时，在 `raw_text` 中补为单个空格，避免调用方写入 `2026-09-2121:50:35` 格式的数据；已有空白保持不变。

## 0.4.0（2026-09-18）

### 变更

- 按 Token 限制在途请求：每个 Token 同时最多服务 `PADDLEOCR_CONCURRENCY_PER_TOKEN`（默认 1）个请求，最大并发为 Token 数乘以该值，超出的调用排队等待最先空闲的 Token，等待时间计入 `timeout`。
- 移除 `recognize_frame` 的 `token` 参数，Token 只来自 `PADDLEOCR_TOKENS` 环境变量或当前工作目录的 `.env`。
- 新增 `crop_box=(left, top, right, bottom)` 参数指定识别区域（帧宽高的比例），默认 `(0.80, 0.0, 0.98, 0.12)` 与原行为一致；`examples/recognize.py` 对应新增 `--crop-box`。
- `PADDLEOCR_CONCURRENCY_PER_TOKEN` 不是 ≥1 的整数时，在首次导入阶段抛出 `pydantic.ValidationError`。

### 升级说明

从 0.3.0 升级时，删除调用中的 `token=` 参数（继续传入会得到 `TypeError`），改为在配置中提供 Token。默认配额 1 意味着并发上限等于 Token 数，原先依赖无限并发的调用方应按需调高 `PADDLEOCR_CONCURRENCY_PER_TOKEN` 或增加 Token，否则超出部分会排队直至超时。排队等待绑定首个需要等待的事件循环，同一进程多次 `asyncio.run()` 不受支持。

## 0.3.0（2026-09-17）

### 变更

- Token 配置改为数组：环境变量由 `PADDLEOCR_TOKEN` 改名为 `PADDLEOCR_TOKENS`，值为 JSON 数组。
- 未显式传入 `token` 时按配置顺序轮询取用下一个 Token，并发调用因而分散到不同凭据。
- 配置为空数组或未配置时仍抛出 `ValueError`；值不是合法 JSON 数组时，在首次导入阶段抛出 `pydantic_settings.SettingsError`。

### 升级说明

从 0.2.1 升级时，将 `.env` 或部署环境中的 `PADDLEOCR_TOKEN=你的Token` 改写为 `PADDLEOCR_TOKENS=["你的Token"]`；旧变量名不再读取，也不作为回退。显式 `token` 参数的用法和优先级不变，传入时不消耗轮询。轮询不做可用性检查，失效 Token 不会被跳过或重试，调用方仍需自行处理 `OCRServiceError`。

## 0.2.1（2026-09-14）

- 裁剪后使用 LANCZOS 统一放大 3 倍，改善小字和复杂背景时间识别，保持单次提交。
- 允许 OCR 日期与时间之间缺少空白，保留原文及严格日期校验。
- 测试示例将实际上传图片保存到 examples/tmp，输出路径供手动查看。
- 记录 13 张样图、78 次真实 OCR 放大对照结果。

## 0.2.0（2026-09-14）

### 变更

- Token 配置改用 pydantic-settings，在首次导入库时加载一次。
- 自动读取的 `.env` 限于当前工作目录，不再向父目录查找；显式 Token 参数仍优先。
- 异常提示和代码说明统一为简体中文。

### 升级说明

从 0.1.1 升级时，请在导入库前设置环境变量，并从包含 `.env` 的项目目录启动。导入后的配置变化需重启进程生效；需要逐次指定 Token 时使用显式 `token` 参数。若需恢复旧的配置读取行为，可将依赖固定回 0.1.1。
