import type { ThemeConfig } from "antd";

// 暖白 · 极简蓝点缀主题。
// 取向：白为主、底色一点点暖（米白）、用白的不同灰度分层（画布<卡面）、蓝只作少量提醒色。
export const COLORS = {
  page: "#F4F1EC",        // 画布底（暖浅灰，把白卡片衬出来）
  surface: "#FFFFFF",     // 卡片/面板（纯白，浮在画布上）
  inset: "#F6F3EE",       // 卡内嵌套 / 表头 / 悬浮（暖灰中间调）
  header: "#FFFFFF",      // 顶栏（白 + 发丝底线）
  accent: "#3E6FD1",      // 强调蓝（主按钮 / 选中 / 链接）——克制使用
  accentStrong: "#2C56AE",// 关键数字 / 链接强调
  accentSoft: "#EEF3FC",  // 蓝色提醒底（成交参考价条等）
  accentSoftBorder: "#DBE6F8",
  text: "#2A2722",        // 主文字（暖近黑，非纯黑）
  text2: "#6B665E",       // 次级（暖灰）
  text3: "#787264",       // 提示 / 弱（暖灰，4.78:1 过 WCAG AA；原 #9C968B 仅 2.94:1）
  border: "#E9E5DE",      // 发丝边（暖）
  borderSoft: "#F0EDE7",
  danger: "#BE564B",      // 暖红（负毛利 / 错误）
  warning: "#B07C33",     // 暖琥珀（待复核 / 警告）
  success: "#4F875C",     // 柔绿
  shadow: "0 1px 2px rgba(40,33,24,.05), 0 4px 14px rgba(40,33,24,.05)",
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
    colorTextQuaternary: "#B6B0A6",
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
      horizontalItemSelectedColor: COLORS.accent,
      horizontalItemHoverColor: COLORS.accent,
      itemColor: COLORS.text2,
      itemSelectedColor: COLORS.accentStrong,
      // 侧栏垂直菜单：选中项蓝底胶囊、悬停暖灰，分组标题弱化
      itemSelectedBg: COLORS.accentSoft,
      itemHoverBg: COLORS.inset,
      groupTitleColor: COLORS.text3,
      itemMarginInline: 8,
    },
    Table: {
      headerBg: COLORS.inset,
      headerColor: COLORS.text2,
      rowHoverBg: "#F7F5F1",
      borderColor: COLORS.borderSoft,
      headerSplitColor: COLORS.borderSoft,
    },
    Segmented: {
      itemSelectedBg: COLORS.surface,
      trackBg: COLORS.inset,
      itemSelectedColor: COLORS.accentStrong,
    },
    Card: { colorBorderSecondary: COLORS.border },
    Statistic: { titleFontSize: 13 },
    Tabs: { inkBarColor: COLORS.accent, itemSelectedColor: COLORS.accentStrong },
    Button: { primaryShadow: "none" },
  },
};
