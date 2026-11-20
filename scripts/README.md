# Scripts

`scripts/` 存放手动执行的一次性维护脚本。它们不属于 `nosis-agent` 包（`pyproject.toml` 只打包 `agent_core`、`agent_runtime` 和 `interfaces`），也不参与运行时装配，只在数据格式需要一次性迁移时使用。

| 脚本 | 用途 |
| --- | --- |
| `migrate_sessions.py` | 将历史 Session Journal 迁移为当前 Job 与缓存用量格式 |
| `backfill_metadata.py` | 统一补全 Workspace 与 Session 元数据 |

## backfill_metadata.py

完全退出 Nosis（TUI、GUI、常驻宿主与 Bridge）后执行：

```bash
python scripts/backfill_metadata.py
python scripts/backfill_metadata.py --sessions-dir /path/to/sessions
```

默认目录为 `~/.nosis/sessions`。脚本扫描根目录下的 Workspace、各 Session 和嵌套 `subagents` 会话，跳过隐藏目录及 Artifact，不改写 Journal。每个 Workspace 先补 `workspace.json`，再补 Session 字段，避免迁移写入产生的文件修改时间影响 Workspace 历史时间推算。

已有 `workspace.json` 仅在 `name` 为空时补全名称，保留其余字段；名称非空的文件不改写。

生成文件包含 `workspace_id`（现有目录名，即绝对路径的 URL 编码）、`path`（解码后的绝对路径）、`name`（路径目录名；根目录使用路径 anchor，如 `F:\` 或 `/`）、`created`、`updated`。时间统一使用 UTC ISO 8601 格式：`created` 取各 Session 首条事件的最早时间；无事件的 Session 使用 Journal 或 `session.json` 的文件创建时间（系统不提供时使用修改时间）。`updated` 取这些文件的最近修改时间，且不早于 `created`。历史打开 Session 的时间未被记录，因此补全值只能由现有文件推算；补全后正常使用 Session 会继续刷新 `updated`。

已有 `session.json` 仅补充缺失的 `session_id`、`workspace_id`、`created`、`updated`，保留所有已有字段和值。Session ID 取目录名，Workspace ID 取所属 Workspace 目录名，子 Agent 继承同一 Workspace ID。时间按该 Session 的首条事件与文件时间推算，规则与 Workspace 相同；已有时间字段不覆盖。没有 Journal 或事件的元数据文件也会处理。缺少 `session.json` 的 Journal 可先用 `migrate_sessions.py` 生成基础元数据。

写入复用 Core 的元数据格式与原子文件替换。重复运行不会改写字段已齐备的文件，Runtime 不包含旧数据补全逻辑。

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
