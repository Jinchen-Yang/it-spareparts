// 应用版本号 + 更新日志单一真值源。
// 发版规范：每次更新 → ① 升 APP_VERSION ② 在 CHANGELOG 顶部新增一条（最新在最前）。
// 升版后用户首次打开会在主页看到一次「更新提示」，并可点版本号查看完整日志。

export const APP_VERSION = "1.1.1";

export interface ChangelogEntry {
  version: string;
  date: string;   // YYYY-MM-DD
  items: string[];
}

export const CHANGELOG: ChangelogEntry[] = [
  {
    version: "1.1.1",
    date: "2026-06-30",
    items: [
      "导入：草稿/已取消订单里产品名为空的明细行不再算「错误」（属正常的未完成单），导入错误数只反映真正需处理的问题；批次详情的「明细」列现可正常显示错误说明",
    ],
  },
  {
    version: "1.1.0",
    date: "2026-06-30",
    items: [
      "新增「导入前检查」：上传采购/销售文件若缺少价格列（常因导出视图选错），会先提示并请二次确认，避免导入后无金额却无人察觉",
    ],
  },
  {
    version: "1.0.0",
    date: "2026-06-29",
    items: [
      "采购记录新增「流程状态」列与「取消单统计」（可按月/季/年查看取消、作废的采购单）",
      "导入更稳健：异常文件不再整批失败；可用「更新模式」重导同一份文件来修复/更新数据",
      "型号查询、采购列表等性能优化；新增页面版本号与更新提示",
    ],
  },
];

export const LATEST = CHANGELOG[0];
