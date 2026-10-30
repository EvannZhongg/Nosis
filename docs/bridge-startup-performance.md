# 常驻 Bridge 与会话启动耗时

测量日期：2026-10-30。基线：`1b450e6093fbc2800a738fdfdebb234d39e0d248`。
同一 Windows 机器、同一个 Python 环境；基线代码从 Git 导出到临时目录，
使用原先每个会话新进程的路径。优化版在一个常驻进程中连续创建独立 Runtime。

## 真实模型请求

Provider 为本机配置的 `deepseek`，模型 `deepseek/deepseek-flash`。每种实现
各三个独立会话，每个会话请求一次简短的 `OK` 回复，禁用工具、MCP、长期记忆
和标题生成，生成上限 256 tokens、SDK 重试次数为零。

计时点包括客户端发送 open、收到 session_ready、提交 Turn、Runtime running、
HTTPX request hook、收到第一条 assistant_delta 和 Turn 结束。网络请求起点在
HTTP 客户端准备完成之后、实际发送之前；第一条文本事件包括网络和模型推理时间。
这些是 stdio 路径的实测，不包含 React 渲染或浏览器网络开销。

三个样本的中位数：

| 阶段 | 基线 | 常驻 Bridge |
| --- | ---: | ---: |
| 打开会话 | 623.8 ms | 13.2 ms |
| 提交 Turn → Runtime running | 1,921.5 ms | 10.3 ms |
| 提交 Turn → 发出 HTTP 请求 | 3,390.2 ms | 19.8 ms |
| 请求发出 → 首字 | 688.6 ms | 739.9 ms |
| 提交 Turn → 首字 | 4,567.2 ms | 878.0 ms |
| 提交 Turn → 完成 | 4,587.2 ms | 890.3 ms |

常驻进程首次启动和预热为 2,463.7 ms，发生在 GUI 宿主启动时，不能从冷启动
总耗时中扣除。预热之后三个会话的打开耗时分别为 14.8 / 12.4 / 13.2 ms，
提交到 HTTP 请求分别为 142.5 / 19.8 / 16.7 ms。第一个会话仍有少量首次调用开销。
基线首次打开为 4,109.4 ms，后两个为 623.8 / 475.4 ms；文件缓存、临时目录和
系统负载会影响冷启动，所以不把首个样本当作稳定启动指标。

## 当前插件和 MCP 配置

另一次测试保留本机 Agent、工具、Skills、五个 Plugin 和路由配置，使用真实项目
Workspace，每种实现两个会话。执行 `runtime_refresh` 装配完整执行面，不发出
模型请求、不执行 Agent Tool，不复制或启动用户的定时任务。每个执行面均连接
成功两个 MCP Server，未出现 unavailable。

| 阶段 | 基线两次 | 常驻 Bridge 两次 |
| --- | --- | --- |
| 打开会话 | 1,179.0 / 442.5 ms | 11.1 / 8.8 ms |
| 完整执行面装配 | 4,692.7 / 3,757.9 ms | 3,498.1 / 2,106.0 ms |

独立 Runtime 仍独立连接 MCP，连接和工具发现需要时间。上面的 20 ms 模型请求
准备时间不能套用到启用了 MCP 的首次 Turn；这部分并未通过跨会话共享连接或
跳过工具发现来隐藏。MCP 启动期间现在也能协作取消，无需等待完整启动超时。

## 实现与生命周期

- GUI 宿主只启动一个 Bridge；关闭会话只关闭通道，退出宿主才结束进程。
- TUI 同样只启动一个 Bridge，为访问过的各会话保留独立 Runtime 通道；切回时
  查询 `session_snapshot`，不重新绑定或装配旧 Runtime。SDK 显式指定每次发送和
  取消的通道，支持一个进程中的多个独立 Runtime。
- 每个通道有独立 RuntimeHost、执行面、Journal 写者、命令队列、事件序号、
  审批等待、取消、Jobs 与 MCP 客户端；阻塞的模型请求不阻塞其他通道。
- RuntimeServices 共享配置服务、环境重载器和一个调度器。SDK 模块和 tokenizer
  词表在同一进程复用；词表初始化串行，避免并发重复构建。
- 默认原生 Provider 的协议资源与 TLS 信任上下文在启动时预热；预热不需要
  API 凭据，也不发送模型请求。
- 请求仍持有独立 HTTP 客户端和流，关闭与取消不影响其他会话。共享的是 TLS
  信任上下文，保留证书和主机名验证；显式证书路径或文件版本变化会重新加载。
- 未连接 MCP 时不导入 MCP SDK。执行面需要 MCP 时，仍走原有并行连接和工具发现。
- 单通道 fatal 只结束该通道；进程故障广播给全部通道，清理后台进程，并允许在
  用户下一次显式打开会话时重新启动 Bridge。不会重放未确认的模型请求。

协议已升级为 major 3，stdio 使用 channel_command / channel_event envelope，
不接受旧格式。Python、TypeScript、Node SDK、TUI 和 GUI 已同步，见
[协议参考](bridge-protocol.md)。

## 复现

以下脚本会使用指定 Provider 的真实 API，读取本机配置但只修改临时副本。
输出不含凭据或完整上游错误响应。每个 API 样本只请求一个短回复。

```powershell
.\.venv\Scripts\python.exe scripts/benchmark_bridge_startup.py --provider deepseek --samples 3 --output tmp/bridge-startup.json
.\.venv\Scripts\python.exe scripts/benchmark_bridge_startup.py --assembly-only --samples 2 --output tmp/bridge-assembly.json
```

`--baseline` 可以指定其他 Git revision。对比性能时不要同时运行编译、测试或
其他 CPU 密集任务；短样本只说明这台机器和这些配置下的结果。

## 验证

Python 全量回归通过 1,186 项测试及 693 个 subtest，18 项按平台或环境条件跳过。
前端 233 项测试、全部 workspace 的 TypeScript 类型检查和生产构建均通过。
安装后的 Python wheel、打包后的 Node SDK 也经过仓库外的 stdio 集成测试。

TUI 的通道切换测试还覆盖切回原 Runtime、忽略非当前通道事件，以及目标会话
打开失败后的原视图恢复。安装后的 SDK 多 Runtime 测试通过同一个真实 Bridge
进程运行两个会话：一个 HTTP 流阻塞时另一个完成请求，取消和关闭前者不影响
后者，原通道关闭后可重新打开 Journal。

新增真实子进程测试覆盖并发请求、同名 Turn / 审批 ID 的隔离、独立 Workspace
文件读取、Journal 冲突、单通道错误、全进程异常退出、通道关闭重开和 MCP
启动中取消。GUI 注册表另验证了取消释放请求时的同会话串行关闭，且不阻塞
其他会话；传输覆盖大于 64 KiB 的分片 UTF-8 检查点。
