# 运维

## 目录都在哪

| 东西 | 位置 |
|---|---|
| 配置文件 | 你给 `agent-recall-kit --config <path>` 的那个 JSON |
| 图快照 / 人工决定 | 配置里的 `state_root`（`CURRENT.json` + `snapshots/N.json`） |
| 连接码 / 布局 / 日志 / pid 记录 | 配置里的 `view_root` |
| 备份 | `agent-recall-kit-backup create --out <dir>` 指定的目录 |

程序不会往源码树/包安装目录写任何运行状态。

## 启停

```powershell
agent-recall-kit --config .\my-data\agent-recall-kit.json serve --port 18791   # 前台
agent-recall-kit-ctl --config .\my-data\agent-recall-kit.json start            # 后台（脱离终端）
agent-recall-kit-ctl --config .\my-data\agent-recall-kit.json status
agent-recall-kit-ctl --config .\my-data\agent-recall-kit.json stop
agent-recall-kit-ctl --config .\my-data\agent-recall-kit.json ensure           # 巡检：在跑就不动，没跑才拉起
```

- 同一端口**只有一个**实例（第二个进程会 bind 失败并退出，不会进服务循环）。
- 停止只针对"身份能对上"的实例：pid 记录 + `/v1/health` 指纹 + 端口归属三重核验，对不上就不杀。
- `ensure` 适合交给计划任务/守护进程；退出码：0 在跑或已拉起，2 端口被别人占，3 另一个入口正在拉起。

## 体检

```powershell
agent-recall-kit-doctor --config .\my-data\agent-recall-kit.json
```

检查：配置可读、来源目录存在、profile 可用、图是否已建、解析是否 0 错误、连接码是否已生成。只读，不改任何文件。

## 备份 / 校验 / 隔离恢复

```powershell
agent-recall-kit-backup create --config .\my-data\agent-recall-kit.json --out .\backups
agent-recall-kit-backup verify --archive .\backups\agent-recall-kit-backup-*.zip
agent-recall-kit-backup restore --archive .\backups\....zip --to .\restored-copy
```

备份里装：图快照、布局、配置副本（**不含**连接码）、逐项 sha256 的 `manifest.json`。
恢复只解到**空目录**，不覆盖正在运行的实例；恢复后把配置的 `state_root`/`view_root` 指过去再启动。

## 日志与保留

- `view_root/serve.log`：服务输出（长跑会增长，需要自行轮转）。
- `view_root/ensure.log`：巡检每次一行（时间/结果/pid/原因，不写连接码），超过 128 KB 自动只留最后 200 行。

## 可选：Windows 守护任务

`adapters/windows/guard_task.ps1` 是**示例**，装包时**不会**自动注册计划任务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File adapters\windows\guard_task.ps1 `
  -Action Install -ConfigPath D:\my-data\agent-recall-kit.json -EveryMinutes 10
powershell -NoProfile -ExecutionPolicy Bypass -File adapters\windows\guard_task.ps1 -Action Remove
```

它做两件事：每 N 分钟跑一次 `agent-recall-kit-ctl ensure`（`IgnoreNew`，不重叠）。删除任务不会停掉正在跑的服务。
不装它也能用，只是服务挂了要手动 `start`。

## 升级

1. 停服务（`stop`）；
2. 换包，`doctor` 看一下；
3. `build` 一次让快照带上新字段；
4. `start`。

图快照自带 `revision` 与哈希校验；旧快照可以被新版本读取，但**反向不保证**。
