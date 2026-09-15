# PARTFLOW MCP 开发设计

状态：**设计已完成，MCP 服务端尚未实现，不可直接部署。**

本目录保存基于现有进销存、维保、单据导入导出代码的开发方案，以及未启用的 WorkBuddy Skills 草案。部署前需要实现身份、文件通道、业务适配、确认和可靠审计，并完成客户端与生产环境验收。

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
