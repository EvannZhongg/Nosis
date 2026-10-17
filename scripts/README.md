# Scripts

`scripts/` 存放手动执行的一次性维护脚本。它们不属于 `nosis-agent` 包（`pyproject.toml` 只打包 `agent_core`、`agent_runtime` 和 `interfaces`），也不参与运行时装配，只在数据格式需要一次性迁移时使用。

| 脚本 | 用途 |
| --- | --- |
| `migrate_sessions.py` | 将历史 Session Journal 迁移为当前 Job 与缓存用量格式 |

## migrate_sessions.py

Session Journal 是 append-only 的 JSONL 记录。历史日志可能缺少 Job 的 `details`、`elapsed_seconds`、`result_summary`、`progress`，或模型用量的 `cache_read_tokens`、`cache_write_tokens`。当前 Runtime 按新格式直接读取，缺少字段的旧日志必须先迁移。升级后、首次启动 Nosis 之前执行（需要 Python >= 3.11）：

```bash
# 先完全退出 Nosis（TUI、GUI 与 Bridge），再执行
python scripts/migrate_sessions.py
python scripts/migrate_sessions.py --sessions-dir /path/to/sessions
```

默认目录为 `~/.nosis/sessions`。脚本行为：

- 逐行校验 JSON；遇到非法事件立即中止，出错日志不改写。
- 只处理会话自身的日志（文件名与所在目录同名），Artifact 等其它 `.jsonl` 不受影响。
- 为 `model_completed.usage` 补充缺失的缓存读写字段，值为 `null`；已有数值（含 `0`）和 `usage: null` 保持不变。历史缓存命中量无法从总 token 数还原，不纳入会话命中率的分子和分母。
- 需要改写的日志先保存一份 `<name>.jsonl.bak`（内容是这次迁移前的日志），后续再次需要迁移时会覆盖该备份。
- 通过同目录临时文件加原子替换写回，并保留原文件权限。
- 幂等：字段齐备的日志不改写也不生成备份，运行结果输出 `Migrated 0 Session journal(s), skipped N already up to date.`。

迁移只在脚本中执行，Runtime 不补字段、不自动迁移旧日志。
