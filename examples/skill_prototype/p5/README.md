# 已发布的受控 Dream UPDATE Skill 示例

本目录保存真实 `management-native-3` 导出，修订号以 `c7fb3c895b2d` 开头，导出摘要为 `d8f6bf61cd7ac6bafbe8df465f4ad77995fac4dff2f8a9ea9175b8fbc4dcf947`。内容来自三个真实 MemorySnapshot：Dream 成功经验支持最小参数修复，失败经验和 scoped-read 经验用于限定归因及适用范围；后经有留痕的开发者修订收窄执行要求。

此工件用于审阅实际产出和复现固定版本导出，不能直接证明当前部署仍准入。正常使用必须由服务端核对正式 Release、来源与工具观察、当前内容/导出摘要及任务范围，再由适配器导出到隔离项目。不要安装到全局 Claude 配置，也不要把它当成通用 Python 修复 Skill。

适配器提供 `run_visible_checks` 与 `record_rule` MCP 工具；模型不能读取宿主隐藏验收。原生发现、Skill 展开、工具调用、实际交付和规则声明均记录，再由独立验收绑定最终补丁。执行命令和发布/恢复步骤见 [最小闭环操作协议](../../../docs/skill-prototype-p5-minimal.md)。

经验性动作只有一项：在权威 `update_node(id, fields, user_name=None)` 合同及当前失败支持下，删除 `fields` 前多余的位置参数。健康、范围外、缺证据、无进一步允许修复、工具异常的处理属于规范性判断，不能包装成五种新的修复经验。
