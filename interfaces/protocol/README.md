# Protocol

该目录提供 TUI 和 GUI 共用的 TypeScript 协议类型，不包含 Agent 执行逻辑。

消息结构定义在 [`src/protocol.ts`](src/protocol.ts)，并与 Python 端的 [`../bridge/protocol.py`](../bridge/protocol.py) 保持同步。修改协议时还需更新两个前端的消息处理与相关测试。
