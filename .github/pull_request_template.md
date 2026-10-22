## 变更与验证

决策: 无

- 变更范围：
- 验证结果：
- 目标分支：近期正式功能选 `main`；仅明确安排的远期实验选 `test`；热修从当前生产 `release-` 标签开 `release/*` / `hotfix/*` 分支。

## 仅当目标为 main

- 发布候选 head SHA：
- 测试环境验收证据：
- 数据库迁移 / 备份 / 回滚方案：
- 跨模型独立复审结论：
- 合并条件：两项必需 CI 全绿、分支与 main 同步、讨论全部解决（不再需要 GitHub APPROVED review，见 `docs/BRANCH_POLICY.md`）。
- 合并不等于上线：生产部署需管理员单独授权，并按精确 SHA 打 `release-` 标签；禁止强推、删分支或绕过保护。
