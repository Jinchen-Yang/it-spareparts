# 前端设计 token（#408）

决策: 无。纯展示层收口，不改变业务口径、权限或 PR1/PR2 的交互。

## 镜像结构

`spareparts-shop` 后续镜像以下完整键名结构，取值可不同，不引入本仓业务词。`color.status` 的值均为实色字符串：pending/manual 用提醒色、ai 用强调色、confirmed 用成功色、failed 用危险色。为保留当前外观，在任务要求的基础键上补充通用的禁用文字、悬停、滚动条、强调底叠层及标签浅色组。

```text
tokens
├─ color
│  ├─ page, surface, inset, header
│  ├─ accent, accentStrong, accentSoft, accentSoftBorder
│  ├─ text, text2, text3, text4
│  ├─ border, borderSoft, danger, warning, success, shadow
│  ├─ hover, scrollbar, scrollbarHover, onAccentSoft, onAccentSoftBorder
│  ├─ tag
│  │  ├─ danger: { text, background, border }
│  │  ├─ warning: { text, background, border }
│  │  ├─ success: { text, background, border }
│  │  └─ info: { text, background, border }
│  └─ status: { pending, ai, confirmed, manual, failed }
├─ space: { xs, sm, md, lg, xl, xxl }
├─ fontSize: { xs, sm, md, lg, xl, xxl }
├─ radius: { sm, md, lg }
├─ shadow: { card, input, none }
├─ breakpoint: { xs, sm, md, lg, xl, xxl }
└─ layout: { headerHeight, contentPadding, mobileContentPadding }
```

- `space` 为 4/8/12/16/24/32；`fontSize` 为 12/13/14/16/20/24。尺寸数字单位为 px。本仓 radius 为 4/8/12，断点为 480/576/768/992/1200/1600，壳层为 60/24/12。不批量重写既有局部尺寸。
- `COLORS === tokens.color`、`SHELL_LAYOUT === tokens.layout`；`COLORS.shadow` 是 `tokens.shadow.card` 的兼容别名。`TAG_STYLES` 将 `color.tag` 映射为 React style，不新增颜色。
- `tokenCssVariables` 将颜色直接映射为 `--mb-*`，其它组加组名前缀，如 `--mb-space-xs`、`--mb-font-size-md`。驼峰/数字边界转短横线（`text2` → `--mb-text-2`），嵌套状态为 `--mb-status-*`，标签为 `--mb-tag-*-text/background/border`。
- `index.css` 的 `:root` 是该映射的静态副本，测试逐键逐值核对。壳层继续按响应式状态覆盖 `--mb-shell-content-padding`；媒体查询不能直接使用 CSS 变量，AntD 断点由 ThemeConfig 消费 `breakpoint`。
- `color-baseline.json` 只用于存量迁移，禁止增加条目或数量；颜色减少时同时缩小/删除配额。新文件和本次清零的文件没有额度。守卫随 `npm run test` 在既有 CI 中执行，无新增依赖。

## 金额兼容

`formatMoney(value, options)` 默认是无币种、zh-CN 分组、固定两位、空值「—」。`mode: fixed` 保留 `toFixed`（池管理），`mode: raw` 保留后端字符串（项目卡）；其余用 `locale` 模式。`prefix/suffix`、`empty`、`emptyStringAsEmpty`、`minimumFractionDigits/maximumFractionDigits`、`useGrouping`、`signPosition` 和 `invalid: preserve` 显式表达原显示差异。旧 `money/moneyExact/moneyIncTax` 均为兼容封装。权限/数据状态的中文解释留在调用页面，不能用格式化默认值替代。
