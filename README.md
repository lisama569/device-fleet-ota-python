# 设备机群固件 / 配置下发（Device Fleet OTA）

基于 FastAPI 的本地内存版设备机群 OTA 管理服务，模拟：

- 设备注册与心跳上报（在线 / 离线判定）
- 固件（firmware）与配置（config）的**分批 / 灰度下发**
- 下发失败与回滚，保证设备当前有效版本不被破坏
- 多设备**并发心跳、并发下发、并发回滚**下的一致性与可追踪性

> 存储为进程内内存表（`app/store.py`），用设备级互斥锁 + 注册表锁模拟并发控制；
> 时钟可注入，测试中用可控时钟而不是真实 sleep。

## 目录结构

```
app/
  errors.py     # 业务错误码（每种拒绝原因一个 code）
  models.py     # 枚举、请求 / 响应 schema
  store.py      # 线程安全内存存储（设备/版本/批次/回滚单）
  service.py    # 业务规则：在线判定、下发状态机、回滚两阶段
  audit.py      # 审计日志（设备标识、上报版本、判定依据）
  api.py        # FastAPI 路由
  main.py       # 应用工厂 + 统一 OTAError 处理
  config.py     # 默认单例
tests/          # 单测与 HTTP 端到端测试
```

## 快速开始

```bash
# 运行服务
python main.py            # http://127.0.0.1:8080，交互式文档 /docs

# 运行测试
pytest                    # 30 个用例，含并发心跳/冲突下发/回滚失败
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/devices/register` | 注册设备（可带初始固件/配置版本） |
| POST | `/devices/heartbeat` | 心跳上报（携带设备自报版本） |
| POST | `/versions` | 注册固件/配置版本到仓库 |
| POST | `/deployments` | 创建下发批次（按 `target_ids` 或 `target_models` 圈选） |
| POST | `/deployments/{id}/results` | 设备上报某批次的安装结果 |
| POST | `/deployments/{id}/rollback` | 触发批次回滚 |
| GET  | `/deployments/{id}` | 查询批次逐设备状态与原因 |
| GET  | `/rollbacks/{id}` | 查询回滚单逐设备结果与原因 |
| GET  | `/devices` | 设备列表（在线状态、有效版本、在途指令） |

## 1. 在线判定语义

- 心跳请求必须包含 `device_id` 与 `ts`（秒级时间戳），缺失返回
  `HEARTBEAT_PAYLOAD_INVALID`（422），消息中指出缺哪个字段。
- 设备未注册返回 `DEVICE_NOT_FOUND`（404）。
- 版本号格式为 `[A-Za-z0-9._-]{1,64}`，非法返回
  `REPORTED_VERSION_INVALID`（422），与"缺字段"是**不同 code**。
- 在线阈值 `ONLINE_THRESHOLD_SECONDS = 30s`：`now - last_heartbeat_ts <= 30`
  判为 online（边界含等号），否则 offline。`GET /devices` 实时计算。
- **乱序心跳**：`ts < 已接受的最新 ts` 一律拒绝，返回
  `STALE_HEARTBEAT_REJECTED`（409），`last_heartbeat_ts` 不回退。
- **并发 / 重复心跳**：同一设备的心跳由设备锁串行化；
  - 不同线程携带相同 `ts` 同时到达时，恰好 1 个真正计数（`accepted=true`），
    其余幂等接受（`accepted=false, duplicate=true`），不会重复计数。
  - 心跳携带的版本只写入 `reported`（设备自报），**不直接**改变服务端
    `active`（有效版本）——有效版本只能通过下发成功推进。

## 2. 下发规则

下发请求体：

```json
{
  "kind": "firmware",            // 或 "config"
  "version": "fw-2.0",
  "target_ids": ["d1", "d2"],    // 显式设备列表，与 target_models 取并集
  "target_models": ["canary"]    // 按机型圈选，用于灰度
}
```

按以下顺序校验，每种失败都是**独立错误码**：

| 场景 | code | HTTP |
| --- | --- | --- |
| 两个范围字段都为空 | `EMPTY_TARGET_SCOPE` | 422 |
| 版本仓库中不存在该 `(kind, version)` | `VERSION_NOT_FOUND` | 404 |
| 范围解析后没有任何已注册设备 | `NO_MATCHING_TARGET` | 404 |
| 目标设备已有**不同版本**同类型在途指令，或有回滚在途 | `TARGET_VERSION_CONFLICT` | 409 |

并发安全保证：

- 冲突判定是**整批原子**的：一批中只要有一台设备冲突，整批拒绝，
  不会给其余设备产生半批指令（响应体 `conflicts` 列出每台冲突设备的原因）。
- 同一设备对同一 `kind` 同时至多一条在途指令；固件与配置互不干扰，可并存。
- 相同版本对相同目标集重复下发：幂等返回原批次，不生成重复指令。
- 部分设备已在同版本旧批次、部分空闲的"扩量"请求同样被拒绝，避免孤儿指令。
- 加锁顺序统一按 device_id 排序，两个批次交叉圈选时不会死锁。

设备执行结果通过 `/results` 上报：

- `success=true`：才把有效版本切换为目标版本，并备份上一有效版本；
- `success=false`：指令标记 `failed` 并携带失败 `reason`，**有效版本保持不变**，
  在途指令释放，允许重新下发；
- 对已终态的批次重复上报：幂等返回（`duplicate=true`），不会二次改写版本。

批次视图（`GET /deployments/{id}`）逐设备给出 `state`、设备当前
`active_version` 与失败 `reason`，全部终态后批次为 `complete`。

## 3. 回滚语义

`POST /deployments/{id}/rollback` 对批次逐设备回滚，回滚单给出每台设备的
实际结果与原因：

| 设备情形 | 回滚结果 | reason 含义 |
| --- | --- | --- |
| 批次 `succeeded` 且存在上一有效版本 | `succeeded`，恢复旧版本 | `restored previous version ...` |
| 批次 `failed`（安装失败） | `failed` | `active version was never changed`（本来就没被破坏，无需动作） |
| 批次仍 `pending`（下发中途） | `failed` | `deployment still in progress ... active version preserved`，在途指令保留，可继续上报结果 |
| 无上一版本（设备裸升） | `failed` | `no previous version available to restore` |
| 有效版本已不是该批次版本 | `failed` | 拒绝覆盖，保护更新的版本 |

其他保证：

- 回滚分两阶段：先在设备锁内登记 **in-flight 回滚窗口**并给出计划，
  释放锁后再逐台生效；生效前二次校验有效版本，窗口期间被其他动作改动则放弃恢复。
- 回滚窗口内同类型的新下发被 `TARGET_VERSION_CONFLICT` 拒绝。
- **重复触发**：窗口内再次回滚同一批次返回 `ROLLBACK_IN_PROGRESS`（409），
  只有一个执行者；回滚完成后再次调用则幂等返回同一张回滚单。
- 部分成功场景下，只有真正升级成功的设备会恢复旧版本；安装失败的设备
  有效版本自始至终不变。

## 4. 审计日志

`ota.audit` logger 对三类关键事件输出结构化文本，单测中用 `caplog` 断言：

```
heartbeat device_id=d1 ts=100.0 reported_versions={'firmware': 'fw-1.0'} decision=accepted reason=last_ts=100.0, within threshold => online
deploy    deployment_id=dep_xxx device_id=d1 kind=firmware version=fw-2.0 decision=rejected reason=batch conflict: ...
rollback  rollback_id=rb_xxx device_id=d2 kind=firmware decision=failed reason=deployment failed; active version was never changed
```

日志必含：设备标识、（上报/目标）版本、判定（accepted / rejected /
succeeded / failed / planned）与判定依据。

## 5. 验证方法

```bash
pytest                         # 全部用例
pytest tests/test_devices_heartbeat.py   # 在线判定/并发心跳/乱序/非法上报
pytest tests/test_deploy.py    # 灰度圈选/冲突原子拒绝/并发下发
pytest tests/test_rollback.py  # 部分成功/中途失败/重复触发/回滚窗口
pytest tests/test_api.py       # HTTP 错误码与端到端生命周期
```

重点覆盖：

- `test_concurrent_heartbeats_counted_once`：8 线程同时心跳，恰好 1 次计数。
- `test_out_of_order_heartbeat_rejected`：乱序心跳被拒且时间戳不回退。
- `test_concurrent_conflicting_deploys_only_one_wins`：两个版本并发下发，
  恰好一个成功，另一个收到 `TARGET_VERSION_CONFLICT`。
- `test_partial_success_rollback_restores_only_succeeded`：部分成功时只回滚
  成功设备，失败设备原因可区分。
- `test_concurrent_duplicate_rollback_triggers_once`：并发重复回滚恰好执行一次。
- `test_deploy_blocked_during_rollback_window`：回滚窗口内新下发被拦截。

## 6. 端到端手工演练

```bash
python - << 'PY'
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
c.post("/devices/register", json={"device_id":"d1","firmware_version":"fw-1.0"})
c.post("/versions", json={"kind":"firmware","version":"fw-2.0"})
dep = c.post("/deployments", json={"kind":"firmware","version":"fw-2.0","target_ids":["d1"]}).json()
c.post(f"/deployments/{dep['deployment_id']}/results",
       json={"device_id":"d1","kind":"firmware","success":True})
print(c.post(f"/deployments/{dep['deployment_id']}/rollback").json())
PY
```
