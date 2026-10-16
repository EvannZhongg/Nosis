# Scripts

`scripts/` 存放手动执行的一次性维护脚本。它们不属于 `nosis-agent` 包（`pyproject.toml` 只打包 `agent_core`、`agent_runtime` 和 `interfaces`），也不参与运行时装配，只在数据格式需要一次性迁移时使用。

| 脚本 | 用途 |
| --- | --- |
| `migrate_sessions.py` | 把 V1 Session Journal 迁移为 V2 的 Job 事件格式 |

## migrate_sessions.py

Session Journal 是 append-only 的 JSONL 记录。自 V2 起，Job 事件携带 `details`、`elapsed_seconds`、`result_summary` 与 `progress` 字段，V1 日志没有这些字段，直接加载会失败。升级到该格式后、首次启动 Nosis 之前执行一次（需要 Python >= 3.11）：

```bash
# 先完全退出 Nosis（TUI、GUI 与 Bridge），再执行
python scripts/migrate_sessions.py
python scripts/migrate_sessions.py --sessions-dir /path/to/sessions
```

默认目录为 `~/.nosis/sessions`。脚本行为：

- 逐行校验 JSON；遇到非法事件立即中止，不改动任何文件。
- 只处理会话自身的日志（文件名与所在目录同名），Artifact 等其它 `.jsonl` 不受影响。
- 需要改写的日志先保存一份 `<name>.jsonl.v1.bak`（内容是这次迁移前的日志），重复运行会覆盖该备份。
- 通过同目录临时文件加原子替换写回，并保留原文件权限。
- 幂等：字段齐备的日志不改写也不生成备份，运行结果输出 `Migrated 0 Session journal(s), skipped N already up to date.`。

脚本只负责相邻版本（V1 → V2）的迁移；后续格式变更需要各自的迁移脚本，不要用它跨越多个版本。
