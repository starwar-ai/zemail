# ZAI 邮件接入发送与查证

ZAI 负责实际业务身份、数据权限、原业务审批和冻结内容；zemail 负责把有原文依据的邮件事项送入 ZAI，以及按已确认内容发送邮件和查证原请求。两侧默认关闭，没有自动启用真实邮箱或业务授权。

## 配置

| 位置 | 配置 | 用途 |
| --- | --- | --- |
| zemail 环境 | `ZAI_API_URL`、`ZAI_API_KEY` | ZAI 服务根地址与原应用密钥，地址不带 `/api`；远程使用 HTTPS，本机测试可用 loopback HTTP |
| zemail 环境 | `ZAI_INTAKE_ENABLED=1`、`ZAI_INTAKE_USERS` | 启用原监控接入，明确逗号分隔的已有用户名；默认关闭、名单为空 |
| zemail 环境 | `ZAI_SEND_TOKEN`、`ZAI_SEND_USERS` | 独立发送服务凭据与允许发送的已有用户；默认均为空 |
| zemail 环境 | `SMTP_HOST`、`SMTP_PORT` | 隐式 TLS SMTP，默认端口 465 |
| zemail 环境 | `MAIL_SENT_FOLDER` | 已发送归档及查证文件夹，默认 `Sent` |
| 原用户邮箱设置 | 地址、口令及 IMAP 主机/端口 | 个人设置优先，缺项回退原服务配置；来源用户不存在时拒绝，不回退冒用默认账号 |
| ZAI 环境 | `ZEMAIL_API_URL`、`ZEMAIL_SEND_TOKEN` | zemail Web 服务根地址及与 `ZAI_SEND_TOKEN` 相同的发送凭据 |
| ZAI 应用管理 | `email:intake`、`email:delivery:claim` | 接收与发送领取能力；还须有相同应用、sourceUser 与实际经办人的已审批接入绑定 |

接入使用现有监控联系人、轮询和静默时段。首次为某联系人启用时记录历史基线，之后的新入站邮件按信息卡原句生成候选；不批量导入历史邮件。没有引文的旧待办不自动取得业务执行权。v3 信息卡带原句，所有报价及采购候选再次按正文逐项核对。

待投递请求及成功凭据复用 `data/monitor/<用户名>.json`。请求先保存再 HTTP 投递；超时或回执不完整保留原请求，重启继续同键重放。成功后只保留回执，ZAI 数据库负责最终去重。失败轮换处理且至少等待一分钟，重复相同错误合并为站内「邮件接入待处理」事件。关闭接入、撤销名单或删除原用户后暂停该人的新投递及重试。

## HTTP 协议

发送端点在 `web.create_app` 装配，均为 POST，使用 `Authorization: Bearer <发送服务凭据>`。原企微回调 `server.create_app` 不提供发送端点。请求体不接受自定义 SMTP 主机、口令、身份或权限。

| 路径 | 请求 | 回应 |
| --- | --- | --- |
| `/api/zai/quotation/account` | `{sourceUser}` | `{sender}`，当前可用的授权邮箱 |
| `/api/zai/quotation/send` | `{deliveryId,messageId,payload,token}` | `{status,receipt}` |
| `/api/zai/quotation/verify` | `{deliveryId,messageId,payload}` | `{status,receipt}`，不调用 SMTP |

`payload` 是 ZAI 冻结的原始 JSON 字符串，字段为 sourceUser、sender、recipient、subject、body、attachments。每个附件包含 name、mimeType、sha256、content（base64）；最多 5 个、解码总量不超过 4 MB。整个冻结字符串不超过 7 MB。指纹按原字符串 UTF-8 计算，不能解析后重新序列化再领取。

发送前调用固定 ZAI 地址 `/api/external/email-operations/delivery-claim`，使用 `x-api-key`，只携带 deliveryId、token、contentHash。只有明确 `claimed:true` 才发送；`false` 只查证，网络失败、拒绝、重定向和无效回执均不发送。凭据不写入本地文件或业务回执。

SMTP 明确拒绝返回 REJECTED；SMTP 接受返回 ACCEPTED；断连或结果不明返回 UNKNOWN。发送端不自行重试。ZAI 对未知结果仅查证原发送，明确拒绝后的再次发送须重新准备并由真人确认。

SMTP 接受后尝试将同一 MIME 追加到已发送文件夹。查证同时核对邮件标识、发送标识、内容指纹、收发件人、主题、正文及全部附件。找不到或无法核对返回 UNKNOWN，不能据此重发。SMTP 接受不代表客户收妥或阅读；SMTP、归档和 HTTP 回执同时丢失时，可能需要人工长期查证。

## 本机验收

在安装现有 requirements 的隔离 Python 环境中运行：

```bash
python -m unittest test_zai_bridge test_zai_sender test_zai_web -v
```

当前 47 项通过，覆盖真实 Web 装配、原监控入口、冻结投递恢复、引文约束、一次性领取、并发重复请求、收件人与附件校验、明确拒绝、未知结果、归档篡改及关闭发送后仍可查证。未启动后台监控线程或调用真实外部服务。

ZAI 跨进程验收设置 `ZEMAIL_SOURCE_DIR`、`ZEMAIL_TEST_PYTHON`；原收件验收另设 `ZEMAIL_PROJECT_PATH`。只使用新建本机隔离 PostgreSQL 验收库。`test_zai_sender.serve_fixture` 启动真实 loopback HTTP 和真实 ZAI 回调，SMTP/IMAP 为合成传输；测试控制端点不在生产蓝图中注册。
