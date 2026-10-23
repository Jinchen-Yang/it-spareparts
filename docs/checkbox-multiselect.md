# CheckboxMultiSelect：下拉复选筛选

决策：D-0025。用于需要“搜索 → 方框勾选 → 确定”的筛选；与即时生效的多选 Select 区分。

基于项目已有 Ant Design 的 Popover、Checkbox、Input、Button 封装，不引入新组件库，也不使用表格内部筛选私有 API。Ant Design Table 的多选/搜索/确认筛选是交互参考，本组件可独立放在工具栏。

## 状态与事件

页面的受控 value 是已应用值；面板打开时复制到草稿。勾选、搜索与面板重置只改草稿，onChange 仅在“确定”时触发。点击外部/Escape关闭则丢弃草稿。受控值被外部重置、导航修改或组件禁用时丢弃旧草稿。选项刷新本身不丢失当前勾选，缺失候选的已选值仍可显示和取消。

重置代表“清空选择和搜索”，不代表“恢复上次已应用值”；点击重置后若直接关闭，已应用值不变。

## 属性与变量

| 名称 | 类型 | 用途 | 为什么这样定义 |
|---|---|---|---|
| T | string 或 number | 选项值类型 | 保留原始类型，字符串1不与数字1混淆 |
| label | string | 字段名与可访问名称 | 各页面复用同一组件而不写死销售业务 |
| value | T[] | 已应用选择 | 页面/URL保持最终真值，组件不发请求 |
| options | CheckboxMultiSelectOption<T>[] | 候选列表 | 选项加载和数据权限留给调用页 |
| option.label | string | 姓名/选项名 | 搜索与关闭态摘要保持可读 |
| option.description | ReactNode，可选 | 辅助信息，如单量 | 不把计数拼进姓名挤占摘要 |
| option.disabled | boolean，可选 | 禁止切换某候选 | 支持业务侧控制选项是否可选 |
| onChange | (value: T[]) => void | 确认提交 | 一次提交到页面已有patch/分页/URL逻辑 |
| disabled | boolean，可选 | 禁用整个控件 | 权限或会话失效时阻止旧草稿提交 |
| loading | boolean，可选 | 候选加载状态 | 与选择状态分离，避免误删深链接选择 |
| selectionUnit | string，可选，默认项 | 多选数量单位 | 人员可传人，其他类型保持通用 |
| onSearch | (query: string) => void，可选 | 将搜索词交给调用方取远程候选 | 组件不持有网络接口，不对服务端搜索结果再次本地过滤 |
| knownOptions | CheckboxMultiSelectOption<T>[]，可选 | 保留不在当前候选页的已选名称 | 远程候选最多50条时，已选ID与完整姓名不丢失 |

关闭时单人直接显示姓名，多人显示数量；打开面板可核对完整已选名单。搜索不改变选择集合，不提供默认全选以避免误扩大筛选。

## 调用方

- 销售分析：将 value 绑定 state.salespeople，确定后沿原 patch 写 salesperson_name 并归一分页；单量只放 description。确认时复核 ownerToken，防止面板打开期间切换账号后提交旧草稿。
- 采购分析：将 value 绑定 state.salesperson，确定后沿原 patch 写 salesperson 并归一分页；同样在确认时复核 ownerToken。
- 账号页客户归属：以稳定客户ID为值，规范名相同时用ID区分，原始名称作辅助说明。onSearch 只请求候选客户，不改已确认归属；knownOptions 保留当前候选页外的已选名称。面板确定更新编辑草稿，外层「保存授权」才提交后端。

共享的是交互，不是两页的URL参数名。其他维度仍使用现有控件。

## 验证

组件测试验证未确认零提交、确认、重置再确认、外部关闭、Escape、外部value/禁用变化、候选刷新、未知已选值、数字类型与搜索保留。页面测试验证URL/API只在确定后更新。真实浏览器补充桌面/390px视口、长名、面板边界与可见复选框；jsdom通过不等于真实布局已验收。

此前独立筛选候选本地检查：组件11项、两页79项、颜色守卫3项通过，`npm run build`通过。独立预览中的真实浏览器检查覆盖上述交互、键盘与长姓名换行，控制台无错误或警告。样例数据预览不等于生产验证；当前待用户点按验收，未部署。页面测试的jsdom下载导航提示和构建的大chunk提示不影响通过结果。

参考：[Ant Design Table](https://ant.design/components/table/)、[Popover](https://ant.design/components/popover/)。

远程搜索扩展不改变销售/采购人员筛选的本地搜索与确认提交语义。

### 权限与人员筛选合并候选验证

本次合并候选：共享组件与销售/采购两页针对性回归 **3 个文件、94 条测试通过**；`tsc --noEmit` 与 `npm run build` 通过；全前端 **161 个文件、1939 条测试通过**。日志分别为 `/tmp/authz-person-selector-targeted.log`、`/tmp/authz-person-selector-typecheck.log`、`/tmp/authz-person-selector-build.log`、`/tmp/authz-person-selector-full.log`。

根代理在独立本地候选的真实浏览器验证了销售人员草稿确认后结果由2单变1单、收起显示姓名，以及390px视口面板无溢出；截图为 `output/playwright/authz-sales-person-confirmed.png` 与 `output/playwright/authz-sales-person-mobile.png`。这是开发期验证，尚不等于用户点按验收或生产部署。
