# 飞书群机器人通知

国内飞书支持两种通知渠道：`feishu_app` 用于有 App ID / App Secret 的自建应用机器人，`feishu` 用于群自定义机器人的 Webhook。原来的 `lark` 类型保留给国际版 Lark，已有配置不自动迁移。

## 应用机器人（App ID / App Secret）

1. 在飞书开放平台为自建应用开启机器人能力，申请 `im:message:send_as_bot` 权限，并按平台要求发布版本。
2. 把应用机器人添加到接收通知的群中，取得该群的 `chat_id`（以 `oc_` 开头）。
3. 在 PanWatch 的「设置 → 通知渠道」新增「飞书应用机器人」，填写 App ID 和 App Secret。
4. 接收类型选择 `chat_id`，填写接收群 ID。保存、启用并设为默认渠道，再点击「测试」。
5. 测试成功后，原有报告和提醒即可按各任务的通知设置发送到该群。

也支持向用户的 `open_id` 或 `user_id` 发送通知。用户必须在应用可用范围内；使用 `user_id` 还需要 `contact:user.employee_id:readonly` 权限。应用机器人自身的 `open_id` 不是你的个人接收 ID。

App Secret 只用于服务端换取租户访问令牌。令牌保存在内存中并提前过期；仅在明确的令牌失效错误下刷新并重试一次，两次发送使用同一 UUID 以防重复。权限或网络错误会明确报告，不会自动重复发送。App Secret、访问令牌不应写入 Git 或公开截图。

官方说明：[获取自建应用访问令牌](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal)、[发送消息与所需权限](https://open.feishu.cn/document/server-docs/im-v1/message/create)。

## 群自定义机器人（Webhook）

1. 在飞书群中添加自定义机器人，复制其完整 Webhook 地址。
2. 在 PanWatch 的「设置 → 通知渠道」新增「飞书」，填写名称及完整 Webhook 地址：
   `https://open.feishu.cn/open-apis/bot/v2/hook/<机器人 token>`。
3. 如果机器人开启了签名校验，填写飞书提供的签名密钥；未开启时留空。
4. 保存、启用并设为默认渠道，然后点击「测试」。测试操作会向该群发送一条消息。
5. 如果飞书开启了关键词或 IP 白名单限制，确保测试消息符合关键词规则，或将 PanWatch 服务器的出站 IP 加入白名单。

Webhook 和签名密钥具有发送消息的权限，不要提交到 Git、截图公开或放进前端源码。

## 行为与范围

- 使用国内飞书官方地址，发送纯文本消息；Markdown 排版会转为可读文本。
- 开启签名时，每次请求生成时间戳和 HMAC-SHA256 签名。
- 同时检查 HTTP 状态和飞书返回的业务错误码，HTTP 200 不会被直接视为发送成功。
- 飞书单条请求有大小限制，过长文本会截断并提示在 PanWatch 查看完整报告。
- 默认渠道接收 PanWatch 原有报告、提醒等通知；具体是否发送仍取决于对应任务的设置。
- Jev 判断目前由用户手动运行，结果显示在股票详情并保存历史，**不会自动推送飞书**。

## 排查

- 地址错误：必须填写完整的国内飞书自定义机器人地址，国际 Lark 请选择原来的 Lark 类型。
- 签名错误：检查密钥、机器人签名开关及服务器时间是否准确。
- 关键词或 IP 被拒绝：检查机器人的安全设置；不要仅凭 HTTP 200 判断成功。
- 连接失败：检查服务器能否通过 HTTPS 访问 `open.feishu.cn`。

官方说明：[飞书自定义机器人使用指南](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)。
