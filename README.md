> [!CAUTION]
> AI + 缝合项目，极度危险，谨慎使用！！！！


| Secret | 说明 |
| --- | --- |
| `PUSHPLUS_TOKEN` | PushPlus Token 通知使用 |

### Epic

只是通知，不能自动领取

| Secret | 说明 |
| --- | --- |
| `EPIC_COOKIE` | 【非必须】Epic 账户 Cookie，需包含 `XSRF-AM-TOKEN` |
| `EPIC_FORCE_ORDER_REFRESH` | 【非必须】强制更新 JSON |

### GLaDOS

> forked from [HGD7764/GLaDOS_checkin_auto](https://github.com/HGD7764/GLaDOS_checkin_auto)

| Secret | 说明 |
| --- | --- |
| `GLADOS_COOKIE` | GLaDOS Cookie；多个账号可用 `&` 分隔 |

### 移动云盘

> forked from [tianjian518/mcloud-ai-bean](https://github.com/tianjian518/mcloud-ai-bean)

| Secret / Variable | 说明 |
| --- | --- |
| `MCLOUD_COOKIES` | 必需；移动云盘 Authorization 和手机号，例如 `Basic xxxxxx#13800138000`；多账号用 `&` 分隔 |
| `MCLOUD_AUTH_ENCRYPTION_KEY` | 必需；加密密钥，可通过 `python -c "import secrets; print(secrets.token_hex(32))"` 生成 |
