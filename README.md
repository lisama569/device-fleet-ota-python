# device-fleet-ota-python

基于 FastAPI 的设备机群固件 / 配置下发管理服务。使用本地内存存储模拟设备上报与版本下发，保证在线状态、灰度（分批）下发与回滚在多设备并发场景下一致、可追踪。

## 运行与验证

```bash
uv sync                       # 安装依赖
uv run python main.py         # 启动服务（127.0.0.1，随机端口）
uv run pytest tests/ -v --log-cli-level=INFO   # 运行测试并查看判定日志
```

## 架构

| 模块 | 职责 |
| --- | --- |
| `app/models.py` | 领域模型：`Device` / `Instruction` / `Rollout`，状态与拒绝原因枚举 |
| `app/store.py` | 线程安全内存存储，所有变更在 `RLock` 内完成，单调 ID 生成 |
| `app/service.py` | 业务逻辑：注册、心跳、下发、上报、回滚；`FleetError` 携带可区分拒绝原因 |
| `app/api.py` | FastAPI 路由，参数解析与错误映射（业务拒绝返回 409 + `reason`） |

## 在线判定

- 设备通过 `POST /devices/{id}/heartbeat` 上报心跳，携带 `firmware_version` 与设备侧时间戳 `ts`。
- 在线条件：`now - last_heartbeat_ts <= online_threshold_sec`（默认 30s，构造 `FleetStore` 时可配）。
- 心跳按时间戳**单调推进**：`ts <= last_heartbeat_ts` 的乱序 / 重复上报被拒绝为 `STALE_HEARTBEAT`，不计数、不回退状态。并发到达的相同时间戳心跳在锁内串行化，全机群只会计数一次。
- 可区分的拒绝原因：
  - `UNKNOWN_DEVICE`：设备未注册；
  - `INVALID_PAYLOAD`：缺少 `firmware_version` 或 `ts` 缺失 / 非正数；
  - `STALE_HEARTBEAT`：时间戳不晚于已记录值。

## 下发规则

1. `POST /firmware` / `POST /config` 注册版本仓库。
2. `POST /rollouts` 创建下发批次：`{kind: firmware|config, version, targets: [...]}`。
3. 校验顺序与拒绝原因（全部校验通过后才会原子创建指令，任一目标非法则整批拒绝，不产生半成品）：
   - `INVALID_PAYLOAD`：非法 `kind`；
   - `VERSION_NOT_FOUND`：版本不在仓库；
   - `EMPTY_TARGETS`：目标列表为空；
   - `UNKNOWN_DEVICE`：目标设备未注册；
   - `TARGET_OFFLINE`：目标设备离线；
   - `CONFLICTING_INSTRUCTION`：目标设备已有 PENDING 指令——同一设备同一时间最多一条未完成指令，不会收到互相冲突的指令。
4. 设备侧通过 `POST /instructions/{id}/report` 上报结果：成功则设备版本推进到目标版本，失败则设备保持当前有效版本并记录 `reason`。重复上报幂等返回当前终态。
5. 批次状态：全部成功 `COMPLETED`，存在失败 `PARTIAL_FAILED`，进行中 `IN_PROGRESS`。

## 回滚语义

`POST /rollouts/{id}/rollback` 对批次内每条指令按其实际状态处理，并返回逐设备报告（设备标识、动作、原因、回滚后版本）：

| 指令状态 | 动作 | 原因 | 设备版本 |
| --- | --- | --- | --- |
| `APPLIED` | `reverted` | `reverted_to_previous` | 回退到下发前版本（指令创建时快照的 `prev_version`） |
| `PENDING` | `cancelled` | `cancelled_before_apply` | 保持当前有效版本 |
| `FAILED` | `no_action` | 保留设备上报的失败原因 | 本就未变更 |

- **幂等**：重复触发回滚返回首次报告并附带 `replay_reason=ROLLOUT_ALREADY_ROLLED_BACK`，不会二次改动设备版本。
- **并发安全**：回滚与设备上报在锁内串行化，指令只会被终结一次；回滚完成后不存在 PENDING 指令，已应用的设备必然被回退。
- 部分成功、下发中途失败（设备未上报）都不会破坏设备当前有效版本。

## 测试与验证方法

`tests/test_fleet.py` 覆盖：

- **并发心跳**：20 线程同时间戳心跳，仅 1 次计数，其余判 `STALE_HEARTBEAT`；乱序心跳不回退状态。
- **冲突下发**：同一设备并发两批下发只有一个成功；逐设备冲突时整批拒绝且不影响其他设备。
- **回滚**：部分成功回滚后逐设备动作 / 原因可区分；重复触发幂等；回滚与上报并发时版本最终一致、无悬挂指令。
- 测试日志统一打印 `device=<设备标识> reported_version=<上报版本> basis=<判定依据>`，用日志可追踪每次判定。

运行：`uv run pytest tests/ -v --log-cli-level=INFO`。
