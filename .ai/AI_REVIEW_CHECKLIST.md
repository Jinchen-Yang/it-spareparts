# AI Review Checklist

> 每次 AI Agent 完成代码修改后，必须逐项自审。任何一项不通过，不得提交。

---

## Architecture

- [ ] 是否破坏架构分层？（API → Service → Model 不可跳跃）
- [ ] 是否在 Router 里写了业务逻辑？
- [ ] 是否在 Service 里操作了 HTTP 请求/响应对象？
- [ ] 是否新增了不必要的模块依赖？
- [ ] Beta 功能是否加了总闸守卫？

## Code Quality

- [ ] 是否引入重复代码？（与已有代码功能相同但写法不同）
- [ ] 函数是否过长？（> 80 行需解释）
- [ ] 是否有不必要的抽象？（"未来可能需要"不是理由）
- [ ] 变量/函数命名是否清晰？（中文业务术语优先，英文技术术语次之）
- [ ] 是否遵守现有代码风格？（缩进、命名、注释密度）

## Type Safety

- [ ] Backend: 所有函数签名有 type hints？
- [ ] Backend: API 请求/响应有 Pydantic model？
- [ ] Frontend: 是否使用了 `any`？（特殊场景用 `unknown` + guard）
- [ ] 是否有 implicit any？

## Security

- [ ] 是否有硬编码的密钥/密码/token？
- [ ] 是否有 SQL 字符串拼接？
- [ ] 是否有 `dangerouslySetInnerHTML`？
- [ ] 敏感数据是否写入了日志？
- [ ] 新 API 端点是否加了鉴权？
- [ ] 文件上传是否有大小和类型校验？

## Testing

- [ ] 新功能是否有测试？
- [ ] Bug 修复是否有复现测试？
- [ ] Beta 功能是否测试了"总闸关闭"场景？
- [ ] 现有测试是否全部通过？

## Performance

- [ ] 是否引入了 N+1 查询？
- [ ] 列表查询是否有分页？
- [ ] 大文件处理是否流式/分块？
- [ ] 是否新增了同步阻塞调用？（应用场景不适合异步的除外）

## Business Rules

- [ ] 是否符合 `.ai/BUSINESS_RULES.md` 中的规则？
- [ ] 库存/价格计算是否正确？
- [ ] 成本取价链是否保持 append-only？
- [ ] 权限判断是否正确？

## Documentation & Traceability（留痕硬性要求，任一不满足即打回）

- [ ] 开工前是否在 `.ai/CURRENT_TASK.md` 登记了"改动前状态 / 预期改动 / 原因 / 是否架构变动"？
- [ ] `.ai/CHANGELOG.md` 是否已追加记录，且包含 before（改动前状态）/ after / 原因(Issue#) / 验证结果 / **commit SHA**？
- [ ] 架构变动是否写了 ADR（原有→新→原因→影响）并同步 `.ai/ARCHITECTURE.md`？
- [ ] API 变更是否更新了 `.ai/API_DESIGN.md`？
- [ ] 新模块是否更新了 `.ai/ARCHITECTURE.md`？
- [ ] 业务规则变更是否更新了 `.ai/BUSINESS_RULES.md`？
- [ ] 能否讲清"改动前是什么、改成了什么、为什么改、是否架构变动"？（讲不清 = 未完成）

## Git

- [ ] Commit message 是否符合规范（type(scope): 中文描述 (#issue)）且独立说明"改了什么、为什么"？
- [ ] 是否只改了相关文件？
- [ ] 是否有未跟踪的文件需要提交？
- [ ] 是否需要先 rebase main？
- [ ] 提交后 commit SHA 是否已回填到 `.ai/CHANGELOG.md`？

---

## 快速自审（最小集合）

如果时间紧迫，至少检查这 5 项：

1. [ ] **安全** — 没有硬编码密钥、SQL 拼接、权限绕过
2. [ ] **测试通过** — `pytest -q` 和 `npm run test` 全绿
3. [ ] **架构** — 没有跨层调用
4. [ ] **业务规则** — 没有破坏已有业务逻辑
5. [ ] **构建通过** — `tsc && vite build` 无错误
