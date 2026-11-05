# Yi 多模型任务主控 · V1

一个可以部署到 Render 免费 Python Web Service 的轻量任务编排器，**默认只使用 OpenRouter 免费模型**。面向手机浏览器和可选 Telegram Bot。不是 ChatGPT 官方客户端的后台任务，也不能使用 ChatGPT Plus 订阅作为 API 余额。

## 已实现

- 任务队列（并发两个完整任务）、主控拆解（1-2 个并行子任务）、并发执行、失败重试、最终汇总与去重
- 网页任务状态、执行日志、子任务输出、停止后续执行、任务结果
- OpenRouter 免费路由 `openrouter/free`（或指定 `:free` 模型），默认拒绝付费模型；限制每分钟与每日调用次数
- 管理令牌强制验证；没有令牌，任务 API 不对外开放
- 可选 Telegram Bot Webhook，限制 Telegram 用户 ID，收到消息后创建任务，完成后回消息
- 标准库实现（无 pip 依赖），`python app.py` 即可运行

## Render 部署

1. Render → New Web Service，连接仓库 `yinanan77/Video-relay`，部署 `agent-orchestrator` 分支。
2. Runtime: Python；Build Command: `echo ready`；Start Command: `python app.py`；Instance: `Free`。
3. 环境变量 `ORCH_TOKEN` 设置为至少 32 位随机字符串，`OPENROUTER_API_KEY` 设置自己的 API Key；不得把密钥提交进 Git。
4. 打开部署后的 `https://你的服务.onrender.com/`，输入 ORCH_TOKEN，填写目标，创建任务；如未设模型密钥，也可在网页填写单次任务密钥。
5. 测试 `GET /health` 返回 JSON `status=ok`。真正模型工作需要 API Key。

## Telegram 可选接入

在 Render Environment 增加：

- `TG_BOT_TOKEN`：BotFather 创建的 Bot Token
- `TG_ALLOWED_USER_IDS`：允许使用者的 Telegram **数字用户 ID**，多个用英文逗号分隔；没填会拒绝所有 TG 消息
- `TG_WEBHOOK_SECRET`：随机字母数字等 Telegram 可接受的字符（1-256，A-Z a-z 0-9 _ -），不可与 token 相同
- `PUBLIC_BASE_URL`：本服务的 `https://xxxx.onrender.com`

配置完成后，携带 `X-Orch-Token` 请求 `POST /api/telegram/register` 来向 Telegram 注册 Webhook。Bot 收到私聊文字就建立任务；/status ID 查询。避免用用户本人账号或 Telegram 登录会话做代理，Bot Token 权限更小。

## 边界

- **Render Free 休眠**：15 分钟无入站请求后休眠，无法保证子任务无限持续执行；在途任务可能中断。
- **临时任务数据**：V1 在内存保存最多 50 条任务，重启、休眠、重新部署都会清空；不是永久队列。需要真正全天候系统应接入持久数据库、常驻 Worker 与任务重放。不要假定 V1 已实现持续监控。
- **免费 API 限额**：OpenRouter 免费模型有请求次数与速率限制。默认每个目标约 4 次模型调用（拆解 1、并行 2、汇总 1），一次重试会多消耗次数；每日最多 35 次尝试（环境可调整，但上限 50）。根据平台政策还存在账号限制。
- **费用风险**：默认禁止非免费模型。Render 免费实例仍受带宽、构建等配额约束；有支付方式时超配额可能收费，请在 Render Dashboard 查看额度。
- **不具备联网调研等工具**：V1 子模型仅能生成文字回答，无法自行浏览网页、操作 GitHub、执行代码或改动用户文件。任何这类任务都必须另外开发最小权限的工具执行器与批准机制。
- **数据隐私**：交给第三方模型的提示词会发往 OpenRouter 及模型供应商；不要上传密钥、身份证件或隐私内容。
- **安全**：仅 HTTPS 部署、令牌为高熵随机值，确保不要在公开截图/视频中暴露。网页单次提交的 API Key 只用于任务，不写入日志或任务记录。

## API

- `GET /health` 公共健康检查
- `GET /api/config` 需 `X-Orch-Token`
- `POST /api/jobs` 同上，body `{"goal":"分析方案"}`；可选 `api_key`
- `GET /api/jobs`、`GET /api/jobs/{id}` 同上
- `POST /api/jobs/{id}/cancel` 同上
- `POST /api/telegram/register` 同上，按需配置
- `POST /telegram/webhook` Telegram 平台使用，需 Telegram 签名头

## 本地测试

```sh
ORCH_TOKEN=some_secure_random_value_123456789012 python app.py
python -m unittest discover -s tests -v
```