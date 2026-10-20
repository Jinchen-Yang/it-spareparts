import type { ThemeConfig } from "antd";

import { tokens } from "./tokens";

// 兼容 PR1/PR2 与既有消费方，取值只在 tokens.ts 维护。
export const SHELL_LAYOUT = tokens.layout;
export const COLORS = tokens.color;

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
    colorTextQuaternary: COLORS.text4,
    colorBorder: COLORS.border,
    colorBorderSecondary: COLORS.borderSoft,
    colorLink: COLORS.accentStrong,
    colorLinkHover: COLORS.accent,
    borderRadius: tokens.radius.md,
    borderRadiusLG: tokens.radius.lg,
    controlHeight: tokens.space.xxl + tokens.space.xs,
    fontSize: tokens.fontSize.md,
    screenXS: tokens.breakpoint.xs,
    screenSM: tokens.breakpoint.sm,
    screenMD: tokens.breakpoint.md,
    screenLG: tokens.breakpoint.lg,
    screenXL: tokens.breakpoint.xl,
    screenXXL: tokens.breakpoint.xxl,
    fontFamily:
      "'PingFang SC', -apple-system, BlinkMacSystemFont, 'Microsoft YaHei', 'Segoe UI', sans-serif",
  },
  components: {
    Layout: {
      headerBg: COLORS.header,
      headerColor: COLORS.text,
      headerHeight: SHELL_LAYOUT.headerHeight,
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
      itemMarginInline: tokens.space.sm,
    },
    Table: {
      headerBg: COLORS.inset,
      headerColor: COLORS.text2,
      rowHoverBg: COLORS.hover,
      borderColor: COLORS.borderSoft,
      headerSplitColor: COLORS.borderSoft,
    },
    Segmented: {
      itemSelectedBg: COLORS.surface,
      trackBg: COLORS.inset,
      itemSelectedColor: COLORS.accentStrong,
    },
    Card: { colorBorderSecondary: COLORS.border },
    Statistic: { titleFontSize: tokens.fontSize.sm },
    Tabs: { inkBarColor: COLORS.accent, itemSelectedColor: COLORS.accentStrong },
    Button: { primaryShadow: tokens.shadow.none },
  },
};

// 自定义 Tag 保持原先浅底/细边外观；Badge / Progress 使用 COLORS 的实色。
const tagStyle = (tone: { text: string; background: string; border: string }) => ({
  color: tone.text, background: tone.background, borderColor: tone.border,
});
export const TAG_STYLES = {
  info: tagStyle(COLORS.tag.info),
  warning: tagStyle(COLORS.tag.warning),
  success: tagStyle(COLORS.tag.success),
  danger: tagStyle(COLORS.tag.danger),
};
