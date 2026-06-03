# 安全与隐私

这个项目处理大量私人信息：学校、家教、聊天、邮件、截图、通知、临时想法。默认安全策略必须比普通待办应用更谨慎。

## 1. 数据最小化

- 不全量同步聊天记录，只接收用户主动转发/发送给机器人的内容。
- 邮件入口只处理转发到系统的邮件。
- 学习平台通知只处理用户授权或主动转发的内容。
- 截图 OCR 后保留必要文本，图片原件可设置自动过期。

## 2. 原始证据保护

`raw_text` 和 `evidence_text` 可能包含隐私。建议：

- 飞书多维表格权限只给用户本人。
- 不把完整原始文本推送到群聊，只在私聊或个人群展示。
- 每日复盘中可只显示标题，点击再看证据。

## 3. Secret 管理

- `.env` 不提交 git。
- `FEISHU_APP_SECRET` 不打日志。
- `FEISHU_BOT_WEBHOOK` 不贴到文档或公开 issue。
- 生产环境用 secret manager。

## 4. 飞书事件安全

Codex 接入时必须做：

- 校验飞书回调签名/verification token/encrypt key。
- 对 URL verification 单独处理。
- 对 message_id 做幂等，防重复写入。
- 过滤非授权群或非授权用户。

## 5. 自动化保护

默认 `dry_run`，上线顺序：

1. 只写多维表格。
2. 只发送每日复盘。
3. 只对高置信度任务创建飞书任务。
4. 只对固定时间事件创建日历。
5. 最后再开全量同步。

强提醒规则：

- confidence < 0.55：不强提醒。
- intent=note：不强提醒。
- possible_duplicate：不强提醒，先进入待确认。
- 涉及敏感内容：只显示模糊标题。

## 6. 数据删除

建议提供：

- 删除 capture 时可选择级联删除 action。
- 删除图片/音频附件。
- 清空 sync_events 中的 request/response payload。
- 导出全部数据。

## 7. 日志

日志中不要包含：

- app_secret
- webhook URL
- raw_text 全文
- 邮件正文全文
- OCR 原文全文

可以记录：

- capture_id
- action_id
- source_type
- sync target
- error code

## 8. 权限边界

一人使用时，飞书空间建议建立私人群或个人工作台，不要把机器人加进大群。多人使用时，需要新增用户表、授权表和数据隔离；当前版本默认单用户。
