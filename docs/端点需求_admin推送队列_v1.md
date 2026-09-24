# 端点需求:admin 推送队列查询 v1

> **提出方**:admin-web 前端(听匣管理后台重构 CP-NEW 系列)
> **承接方**:后端(content-service / user-service + api-gateway)
> **日期**:2026-09-24 · **优先级**:P2(页面已有降级形态,不阻塞发布;运营全量排障依赖本端点)
> **状态**:待排期

---

## 1. 背景与现状

管理后台「推送队列」页(`/push-notifications`)需要展示**全量推送任务**并按状态排障。当前后端没有匹配的实现:

| 现状 | 问题 |
|---|---|
| 网关仅注册 `GET /api/v1/notifications`(user-service,CP5.4a) | **用户侧端点**:`require_user` + 只返回当前登录者自己的推送(JWT `sub`),admin 登录态看到的不是全量队列 |
| 参数只有 `unread_only` / `limit` | 无 status 过滤(表有 pending/sent/failed 语义)、无 offset 分页、响应无 total |
| 响应 `{notifications, unread_count}` | 无 `user_id` / `status` 字段(用户侧视角不需要,admin 视角必需) |

前端已做降级(页面如实标注"仅当前账号自己的推送",路径回归已修,见 admin-web `838a162`),**运营全量排障能力等本端点**。

## 2. 需求:新增 admin 端点

### 2.1 `GET /api/v1/admin/push-notifications`

- **鉴权**:`require_admin_or_operator`(与现有 admin 端点一致)
- **归属服务**:user-service(`push_notifications` 表所在库;若跨库则由网关聚合,后端定)
- **不写审计**(只读,与 `/admin/consents` 同口径)

**查询参数**

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `status` | string | 空=全部 | 枚举 `pending` / `sent` / `failed`(与表内实际枚举对齐) |
| `user_id` | int | 空=全部 | 按接收者过滤 |
| `tag_slug` | string | 空=全部 | 按触发标签过滤 |
| `limit` | int | 50(cap 200) | 分页大小 |
| `offset` | int | 0 | 分页偏移 |

**响应**(统一信封 `{code, message, data}`,data 为分页对象,与 §接口文档 v1.1 既有约定一致):

```json
{
  "total": 1234,
  "limit": 50,
  "offset": 0,
  "items": [
    {
      "id": 881,
      "user_id": 42,
      "article_id": 103,
      "tag_slug": "ai-weekly",
      "title": "今日蒸馏完成",
      "body": "《…》已生成,点击收听",
      "deeplink": "stashbox://article/103",
      "status": "sent",
      "error": null,
      "created_at": "2026-09-24T09:00:00Z",
      "sent_at": "2026-09-24T09:00:05Z",
      "read_at": null
    }
  ]
}
```

字段口径:`status` / `error` / `sent_at` 以 `push_notifications` 表实际列为准,**有则回、无则补列**(补列需迁移,编号顺延 0030+)。排序固定 `created_at DESC`。

### 2.2 网关 ROUTES 注册(硬性)

`api-gateway/config.py` 显式注册 `Route("GET", "/api/v1/admin/push-notifications", ...)`。
**教训口径**:admin 段不在 fallback 前缀匹配里,漏注册 = 前端 404(接口文档 v1.1 §4 同款问题)。

### 2.3 (可选,P3)失败重推

`POST /api/v1/admin/push-notifications/{id}/retry` — 仅 `status=failed` 可重推,reason 必填写 `admin_operation_logs`。运营侧有真实诉求时再排期,不阻塞 2.1。

## 3. 前端承接(后端合入后,admin-web 半天内完成)

1. `src/api/admin/push.ts`:路径切回 `/api/v1/admin/push-notifications`,恢复 `normalizeList`(标准 items/total 形态),参数改 `limit/offset` 分页
2. `PushNotifications.tsx`:移除页头"仅当前账号"降级提示,状态 tab 的 `status` 参数从"传了也没用"变为真实生效,补分页控件
3. 类型 `PushNotificationRow` 对齐 §2.1 items 字段
4. 更新 `docs/endpoint-coverage-audit` 覆盖表 + 本需求文档标注"已落地"

## 4. 验收标准

- [ ] 无 token → 401;viewer token → 403;admin/super_admin/operator → 200
- [ ] `status=pending` 只回 pending 行;`user_id=42` 只回该用户行;组合过滤正确
- [ ] `limit=50&offset=50` 翻页不重不漏,`total` 为过滤后总数
- [ ] 空结果 → `{items: [], total: 0}`,200 不报错
- [ ] 网关 ROUTES 已注册(404 检查:`curl :8100/api/v1/admin/push-notifications` 带 token 返回 200)
- [ ] 查询不写 `admin_operation_logs`(只读口径)
- [ ] 响应过 `tests/` 契约用例(信封 + 分页字段命名与 §接口文档 v1.1 一致)

## 5. 工作量粗估(供排期参考)

端点 + 网关注册 + 测试:**约 0.5 天**(表已存在,纯查询聚合;若需补 `status/error/sent_at` 列则 +0.5 天含迁移)。

---

*需求方联系人:admin-web 前端重构会话(mvs_2cbb…);文档随接口文档 v1 系列归档。*