// 应用版本号 + 更新日志单一真值源。
// 发版规范：每次更新 → ① 升 APP_VERSION ② 在 CHANGELOG 顶部新增一条（最新在最前）。
// 升版后用户首次打开会在主页看到一次「更新提示」，并可点版本号查看完整日志。

export const APP_VERSION = "1.0.0";

export interface ChangelogEntry {
  version: string;
  date: string;   // YYYY-MM-DD
  items: string[];
}

export const CHANGELOG: ChangelogEntry[] = [
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
