import type { ThemeConfig } from "antd";

// 莫兰迪低饱和·浅蓝主题（UI 改版定稿）。
// 设计取向：同色系深浅分层（页底 < 卡面 < 内嵌）、卡片微阴影悬浮、单一灰蓝强调、语义色全部压成莫兰迪。
export const COLORS = {
  page: "#DCE5EC",        // 页面底（最深的中性蓝灰）
  surface: "#F7FAFC",     // 卡片/面板（浮起来的雾白蓝）
  inset: "#EAF0F4",       // 卡内嵌套块（凹陷的中间调）
  header: "#F2F6F8",      // 顶栏
  accent: "#5E8DA3",      // 强调灰蓝（主按钮/链接/选中）
  accentStrong: "#37708A",// 关键数字
  text: "#2B3E47",        // 主文字（深石板，非纯黑）
  text2: "#5F727A",       // 次级
  text3: "#92A1A8",       // 提示/弱
  border: "#DCE6EC",
  borderSoft: "#E4ECF1",
  danger: "#B5635A",      // 莫兰迪赤陶（负毛利/错误）
  warning: "#9A7B43",     // 莫兰迪赭（待复核/警告）
  success: "#5E7A60",     // 莫兰迪豆绿
  shadow: "0 1px 2px rgba(54,84,104,.07), 0 3px 10px rgba(54,84,104,.09)",
};

export const themeConfig: ThemeConfig = {
  token: {
    colorPrimary: COLORS.accent,
    colorInfo: COLORS.accent,
    colorSuccess: COLORS.success,
    colorWarning: COLORS.warning,
    colorError: COLORS.danger,
    colorBgLayout: COLORS.page,
    colorBgContainer: COLORS.surface,
    colorBgElevated: COLORS.surface,
    colorText: COLORS.text,
    colorTextSecondary: COLORS.text2,
    colorTextTertiary: COLORS.text3,
    colorTextQuaternary: "#AEBAC0",
    colorBorder: COLORS.border,
    colorBorderSecondary: COLORS.borderSoft,
    colorLink: COLORS.accentStrong,
    colorLinkHover: COLORS.accent,
    borderRadius: 8,
    borderRadiusLG: 12,
    controlHeight: 36,
    fontSize: 14,
    fontFamily:
      "'PingFang SC', -apple-system, BlinkMacSystemFont, 'Microsoft YaHei', 'Segoe UI', sans-serif",
  },
  components: {
    Layout: {
      headerBg: COLORS.header,
      headerColor: COLORS.text,
      headerHeight: 60,
      bodyBg: COLORS.page,
    },
    Menu: {
      horizontalItemSelectedColor: COLORS.accentStrong,
      horizontalItemHoverColor: COLORS.accent,
      itemColor: COLORS.text2,
      itemSelectedColor: COLORS.accentStrong,
    },
    Table: {
      headerBg: "#EBF1F4",
      headerColor: COLORS.text2,
      rowHoverBg: "#EBF2F5",
      borderColor: COLORS.borderSoft,
      headerSplitColor: COLORS.borderSoft,
    },
    Segmented: {
      itemSelectedBg: COLORS.surface,
      trackBg: "#E0E8ED",
      itemSelectedColor: COLORS.accentStrong,
    },
    Card: { colorBorderSecondary: COLORS.borderSoft },
    Statistic: { titleFontSize: 13 },
    Tabs: { inkBarColor: COLORS.accent, itemSelectedColor: COLORS.accentStrong },
  },
};
