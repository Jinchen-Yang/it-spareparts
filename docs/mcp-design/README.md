# PARTFLOW MCP 开发设计

状态：**首版实现已进入开发分支，默认关闭；实际范围与验收以 [实现说明](../mcp/README.md) 为准。**

本目录保留最初开发设计与历史Skills草案，不代表全部规划能力已开放。员工用的实际Skills位于 `connectors/workbuddy/partflow/skills/`。生产发布和员工客户端联调尚需按实现说明单独执行。

- [开发方案](开发方案.md)
- [工具清单](工具清单.md)：19 个设计工具，13 个首版核心、3 个可选、3 个后续扩展。
- [工具契约草案](tool-catalog.json)
- [审计与验收](审计与验收.md)
- [开发排期与交付](开发排期与交付.md)
- [代码证据索引](code-inventory.md)：固定到调研时的源码提交，非生产在线状态。
- [静态检查记录](validation.json)：不代表运行测试通过。

Skills 草案：

- [文件整理](skills/partflow-document-intake/SKILL.md)
- [导入预览](skills/partflow-import-review/SKILL.md)
- [导出与往返](skills/partflow-export-roundtrip/SKILL.md)
- [项目材料核对](skills/partflow-project-check/SKILL.md)
- [PN 与库存查询](skills/partflow-parts-lookup/SKILL.md)

源码调研提交：`ae847991e60e84d12c64b375fc07d30c99357efe`。文档独立分支基于同步后的 `origin/main` 创建，避免把先前功能分支历史混入本次提交。Skills 随文档保存，尚未安装到员工客户端。
