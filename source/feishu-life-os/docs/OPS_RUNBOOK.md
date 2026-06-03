# 运维手册

## 1. 本地开发

```bash
make dev
make test
make demo
make run
```

## 2. 数据库迁移

```bash
make migrate
```

数据库默认：`.data/lifeos.sqlite3`

## 3. 调试抽取

使用 `scripts/sample_payloads.http` 或 curl：

```bash
curl -X POST http://localhost:8000/api/captures \
  -H 'Content-Type: application/json' \
  -d '{"raw_text":"后天下午4点开组会，今晚整理 slides", "source_type":"manual"}'
```

## 4. 查看每日复盘

```bash
curl http://localhost:8000/api/reviews/daily
```

## 5. 飞书 dry-run 调试

保持：

```bash
FEISHU_SYNC_MODE=dry_run
```

此时同步不会调用飞书，只会写入 `sync_events` 表。可以用 sqlite 查看：

```bash
sqlite3 .data/lifeos.sqlite3 'select target, entity_type, status, request_payload from sync_events order by created_at desc limit 5;'
```

## 6. 切换到多维表格

```bash
FEISHU_SYNC_MODE=bitable
```

先只测试 1 条 capture，确认字段类型和权限无误。

## 7. 常见故障

### 7.1 tenant_access_token 获取失败

检查：

- app_id/app_secret 是否正确。
- 应用是否发布/启用。
- 网络代理是否影响 open.feishu.cn。

### 7.2 多维表格 403

检查：

- 应用是否有表格权限。
- 多维表格是否授权给应用。
- 如果开启高级权限，应用所在群是否有读写权限。

### 7.3 字段转换失败

临时解决：

- 把日期/多选字段改成文本。
- 确认字段名完全一致。
- 先同步最小字段：标题、状态、创建时间。

### 7.4 重复创建任务

当前版本还未完全实现飞书 task_guid 回填。接入任务前先补幂等：

- action 已有 `feishu_task_guid`：调用 update，不 create。
- sync_events success 后回填。
- 同一个 capture/source_ref 不重复处理。

## 8. 备份

```bash
cp .data/lifeos.sqlite3 .data/lifeos.$(date +%Y%m%d-%H%M).sqlite3
```

## 9. 部署建议

最小部署：

- 一台 VPS 或本地 NAS。
- HTTPS 反向代理。
- Uvicorn/Gunicorn。
- SQLite 起步，数据增长后换 PostgreSQL。
- `reminder_worker` 会在 `DEFAULT_MORNING_REVIEW_HOUR:DEFAULT_MORNING_REVIEW_MINUTE`
  的 15 分钟发送窗口内推送晨间任务汇总卡片。
- 晨间汇总卡片未确认时，`reminder_worker` 会按 `DAILY_REVIEW_FOLLOWUP_HOURS`
  触发强提醒；默认是每 2 小时一次，直到点击“已查看”或强提醒卡片里的停止按钮。
- 旧版每日复盘接口仍可用 cron 调用 `/api/reviews/daily/send`。

## 10. 定时任务示例

晨间任务汇总默认由 `reminder_worker` 内置调度。若仍要使用旧版 Webhook 复盘，可用：

```cron
0 21 * * * curl -X POST http://127.0.0.1:8000/api/reviews/daily/send
```

生产环境请使用内网地址或加鉴权。
