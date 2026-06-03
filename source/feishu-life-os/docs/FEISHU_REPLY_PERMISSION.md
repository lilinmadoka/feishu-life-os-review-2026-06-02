# 飞书机器人回复权限

Agent-first 私聊入口需要应用具备机器人主动发消息权限。

如果调用 `/im/v1/messages` 返回 `99991672`，说明应用缺少以下任一权限：

```text
im:message:send_as_bot
im:message:send
im:message
```

建议在飞书开放平台的“权限管理”中搜索并添加：

```text
im:message:send_as_bot
```

添加权限后重启本机 FastAPI，刷新 `tenant_access_token`。
