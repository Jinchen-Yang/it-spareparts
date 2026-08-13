import { lazy } from "react";
import type { ComponentType, LazyExoticComponent, ReactNode } from "react";
import {
  CloudUploadOutlined,
  ControlOutlined,
  DashboardOutlined,
  DeploymentUnitOutlined,
  DollarOutlined,
  FileDoneOutlined,
  FileSearchOutlined,
  FundOutlined,
  InboxOutlined,
  LineChartOutlined,
  ProfileOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SearchOutlined,
  SettingOutlined,
  ShoppingCartOutlined,
  TeamOutlined,
  ToolOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { readMaintenanceCapabilities } from "./components/maintenance/maintenancePermissions";

/**
 * 导航单一真值源：路由、侧栏菜单、面包屑、页面标题都从这里生成。
 * 新增页面只改这一处，避免"菜单有入口但路由/权限没跟上"的漂移。
 *
 * 维保导航已从旧的 flat group + beta group 重构为两个正式业务组：
 *   grp-maintenance-workbench — 维保工作台（所有维保用户可见）
 *   grp-maintenance-admin      — 维保数据维护（仅 admin 及受权用户可见）
 */

export interface NavItem {
  key: string;
  path: string;
  label: string;
  icon: ReactNode;
  perm?: string;
  anyPerm?: string[];
  visibleWhen?: () => boolean;
  page: LazyExoticComponent<ComponentType>;
  load: () => Promise<{ default: ComponentType }>;
}

export interface NavGroup {
  key: string;
  /** 业务域分组标题；null = 顶部独立快捷入口（不属于任何业务域） */
  label: string | null;
  items: NavItem[];
}

// 路由级懒加载：每个页面独立 chunk，首包不再背 1.6MB。
// import() 工厂单独存一份（load 字段）供空闲预取——lazy 与预取命中同一模块缓存
const loadBossBoard = () => import("./pages/BossBoardPage");
const loadPools = () => import("./pages/PoolsPage");
const loadPoolAnalysis = () => import("./pages/PoolAnalysisPage");
const loadPartSearch = () => import("./pages/PartSearchPage");
const loadProfit = () => import("./pages/ProfitPage");
const loadPurchaseAnalysis = () => import("./pages/purchases/PurchaseAnalysisPage");
const loadPurchaseExceptions = () => import("./pages/purchases/PurchaseExceptionsPage");
const loadPurchaseRecords = () => import("./pages/purchases/PurchaseRecordsPage");
const loadProjectCost = () => import("./pages/ProjectCostPage");
const loadMaintenanceProjectMaster = () => import("./pages/MaintenanceProjectMasterPage");
const loadMaintenanceProjects = () => import("./pages/maintenance/MaintenanceProjectsPage");
const loadMaintenanceProjectWorkspace = () => import("./pages/maintenance/MaintenanceProjectWorkspacePage");
const loadMaintenanceProjectUpdates = () => import("./pages/maintenance/MaintenanceProjectUpdatesPage");
const loadMaintenanceCostRefill = () => import("./pages/maintenance/MaintenanceCostRefillPage");
const loadMaintenanceAcceptance = () => import("./pages/maintenance/MaintenanceAcceptancePage");
const loadMaintenanceDemands = () => import("./pages/maintenance/MaintenanceDemandManagementPage");
const loadMaintenanceWarehouse = () => import("./pages/maintenance/MaintenanceWarehouseWorkbenchPage");
const loadMaintenanceSourceOrders = () => import("./pages/maintenance/MaintenanceSourceOrderAssignmentsPage");
const loadMaintenanceMigration = () => import("./pages/maintenance/MaintenanceMigrationPage");
const loadMaintenanceProjectImport = () => import("./pages/maintenance/MaintenanceProjectImportPage");
const loadMaintenanceDownloadsCompat = () => import("./pages/maintenance/MaintenanceDownloadsCompatRedirect");
const loadMaintenanceRemindersCompat = () => import("./pages/maintenance/MaintenanceRemindersCompatRedirect");
const loadInventory = () => import("./pages/InventoryPage");
const loadImport = () => import("./pages/ImportPage");
const loadMasterData = () => import("./pages/MasterDataPage");
const loadGovernance = () => import("./pages/GovernancePage");
const loadPoolManagement = () => import("./pages/PoolManagementPage");
const loadChat = () => import("./pages/ChatPage");
const loadAccounts = () => import("./pages/AccountsPage");
const loadSystemSettings = () => import("./pages/SystemSettingsPage");

const BossBoardPage = lazy(loadBossBoard);
const PoolsPage = lazy(loadPools);
const PoolAnalysisPage = lazy(loadPoolAnalysis);
const PartSearchPage = lazy(loadPartSearch);
const ProfitPage = lazy(loadProfit);
const PurchaseAnalysisPage = lazy(loadPurchaseAnalysis);
const PurchaseExceptionsPage = lazy(loadPurchaseExceptions);
const PurchaseRecordsPage = lazy(loadPurchaseRecords);
const ProjectCostPage = lazy(loadProjectCost);
const MaintenanceProjectMasterPage = lazy(loadMaintenanceProjectMaster);
const MaintenanceProjectsPage = lazy(loadMaintenanceProjects);
const MaintenanceProjectWorkspacePage = lazy(loadMaintenanceProjectWorkspace);
const MaintenanceProjectUpdatesPage = lazy(loadMaintenanceProjectUpdates);
const MaintenanceCostRefillPage = lazy(loadMaintenanceCostRefill);
const MaintenanceAcceptancePage = lazy(loadMaintenanceAcceptance);
const MaintenanceDemandManagementPage = lazy(loadMaintenanceDemands);
const MaintenanceWarehouseWorkbenchPage = lazy(loadMaintenanceWarehouse);
const MaintenanceSourceOrderAssignmentsPage = lazy(loadMaintenanceSourceOrders);
const MaintenanceMigrationPage = lazy(loadMaintenanceMigration);
const MaintenanceProjectImportPage = lazy(loadMaintenanceProjectImport);
const MaintenanceDownloadsCompatRedirect = lazy(loadMaintenanceDownloadsCompat);
const MaintenanceRemindersCompatRedirect = lazy(loadMaintenanceRemindersCompat);
const InventoryPage = lazy(loadInventory);
const ImportPage = lazy(loadImport);
const MasterDataPage = lazy(loadMasterData);
const GovernancePage = lazy(loadGovernance);
const PoolManagementPage = lazy(loadPoolManagement);
const ChatPage = lazy(loadChat);
const AccountsPage = lazy(loadAccounts);
const SystemSettingsPage = lazy(loadSystemSettings);

// 组 key 加 grp- 前缀与 item key 隔离命名空间（防止将来改用 SubMenu 时 keyPath 相撞）
export const NAV_GROUPS: NavGroup[] = [
  {
    key: "grp-board",
    label: null,
    items: [
      { key: "boss", path: "/boss", label: "经营看板", icon: <DashboardOutlined />, perm: "page_boss_board", page: BossBoardPage, load: loadBossBoard },
    ],
  },
  {
    key: "grp-quick",
    label: null,
    items: [
      { key: "chat", path: "/chat", label: "AI 助手", icon: <RobotOutlined />, perm: "page_chat", page: ChatPage, load: loadChat },
    ],
  },
  {
    key: "grp-price-analysis",
    label: "价格分析",
    items: [
      { key: "pools", path: "/pools", label: "互通池", icon: <DollarOutlined />, perm: "page_pool_analysis", page: PoolsPage, load: loadPools },
    ],
  },
  {
    key: "grp-sales",
    label: "销售管理",
    items: [
      { key: "parts", path: "/parts", label: "型号查询", icon: <SearchOutlined />, perm: "page_parts", page: PartSearchPage, load: loadPartSearch },
      { key: "profit", path: "/profit", label: "利润分析", icon: <LineChartOutlined />, perm: "page_profit", page: ProfitPage, load: loadProfit },
    ],
  },
  {
    key: "grp-purchase",
    label: "采购管理",
    items: [
      { key: "purchases-analysis", path: "/purchases/analysis", label: "采购分析", icon: <FundOutlined />, perm: "page_purchases", page: PurchaseAnalysisPage, load: loadPurchaseAnalysis },
      { key: "purchases-exceptions", path: "/purchases/exceptions", label: "采购异常", icon: <WarningOutlined />, perm: "page_purchases", page: PurchaseExceptionsPage, load: loadPurchaseExceptions },
      { key: "purchases-records", path: "/purchases/records", label: "采购明细", icon: <ShoppingCartOutlined />, perm: "page_purchases", page: PurchaseRecordsPage, load: loadPurchaseRecords },
    ],
  },
  {
    key: "grp-maintenance-workbench",
    label: "维保工作台",
    items: [
      { key: "maintenance-projects", path: "/maintenance/projects", label: "项目总览", icon: <DashboardOutlined />, perm: "page_maintenance", page: MaintenanceProjectsPage, load: loadMaintenanceProjects },
      { key: "maintenance-updates", path: "/maintenance/monthly-updates", label: "月度项目更新", icon: <CloudUploadOutlined />, visibleWhen: () => readMaintenanceCapabilities().canApplyRoundtrip, page: MaintenanceProjectUpdatesPage, load: loadMaintenanceProjectUpdates },
      { key: "maintenance-acceptance", path: "/maintenance/acceptance", label: "验收与结项", icon: <FileDoneOutlined />, perm: "page_maintenance", page: MaintenanceAcceptancePage, load: loadMaintenanceAcceptance },
    ],
  },
  {
    key: "grp-maintenance-admin",
    label: "维保数据维护",
    items: [
      { key: "maintenance-project-import", path: "/maintenance/admin/project-import", label: "项目资料同步", icon: <CloudUploadOutlined />, visibleWhen: () => readMaintenanceCapabilities().canManageProject, page: MaintenanceProjectImportPage, load: loadMaintenanceProjectImport },
      { key: "maintenance-project-master", path: "/maintenance/admin/project-master", label: "项目主档维护", icon: <ProfileOutlined />, visibleWhen: () => readMaintenanceCapabilities().canManageProject, page: MaintenanceProjectMasterPage, load: loadMaintenanceProjectMaster },
      { key: "maintenance-demands", path: "/maintenance/admin/demands", label: "异常维保单处理", icon: <FileSearchOutlined />, visibleWhen: () => readMaintenanceCapabilities().canManageProject, page: MaintenanceDemandManagementPage, load: loadMaintenanceDemands },
      { key: "maintenance-warehouse", path: "/maintenance/admin/warehouse", label: "仓库单据核对", icon: <InboxOutlined />, visibleWhen: () => readMaintenanceCapabilities().canManageProject, page: MaintenanceWarehouseWorkbenchPage, load: loadMaintenanceWarehouse },
      { key: "maintenance-source-orders", path: "/maintenance/admin/source-orders", label: "历史单据归属", icon: <ToolOutlined />, visibleWhen: () => readMaintenanceCapabilities().canManageProject, page: MaintenanceSourceOrderAssignmentsPage, load: loadMaintenanceSourceOrders },
      { key: "maintenance-migration", path: "/maintenance/admin/migration", label: "历史数据迁移核对", icon: <SafetyCertificateOutlined />, visibleWhen: () => readMaintenanceCapabilities().canReviewMigration, page: MaintenanceMigrationPage, load: loadMaintenanceMigration },
      { key: "maintenance-cost-refill", path: "/maintenance/admin/cost-refill", label: "领用缺价补录", icon: <DollarOutlined />, visibleWhen: () => readMaintenanceCapabilities().canManageProject, page: MaintenanceCostRefillPage, load: loadMaintenanceCostRefill },
    ],
  },
  {
    key: "grp-stock",
    label: "库存管理",
    items: [
      { key: "inventory", path: "/inventory", label: "库存查询", icon: <InboxOutlined />, perm: "page_inventory", page: InventoryPage, load: loadInventory },
    ],
  },
  {
    key: "grp-data",
    label: "数据中心",
    items: [
      { key: "import", path: "/import", label: "数据导入", icon: <CloudUploadOutlined />, perm: "page_import", page: ImportPage, load: loadImport },
      { key: "master", path: "/master", label: "备件主数据", icon: <ProfileOutlined />, perm: "page_master_data", page: MasterDataPage, load: loadMasterData },
      { key: "governance", path: "/governance", label: "数据治理", icon: <ControlOutlined />, perm: "page_governance", page: GovernancePage, load: loadGovernance },
      // 池维护（action_pool_manage）与约束价设置（action_pool_set_policy）是两种独立授权：
      // 任一权限都必须能进页面（页面内再按各自权限开放对应操作区）
      { key: "pool-mgmt", path: "/pool-management", label: "互通PN池管理", icon: <DeploymentUnitOutlined />, anyPerm: ["action_pool_manage", "action_pool_set_policy"], page: PoolManagementPage, load: loadPoolManagement },
    ],
  },
  {
    key: "grp-admin",
    label: "管理中心",
    items: [
      // 权限中心 v2：入口由 page_accounts 键驱动（admin 在 App 层恒短路通过，行为不变）；
      // 管理员可把「查看」受控委派给骨干员工，写操作另需 action_account_manage（后端把关）
      { key: "accounts", path: "/accounts", label: "账号与权限", icon: <TeamOutlined />, perm: "page_accounts", page: AccountsPage, load: loadAccounts },
      // 不绑定可委派的 page_* 键：系统级业务默认值只能由 admin 看到入口和注册路由。
      // 项目成本页读取默认值仍由后端 page_maintenance 把关。
      { key: "system-settings", path: "/system-settings", label: "系统设置", icon: <SettingOutlined />, page: SystemSettingsPage, load: loadSystemSettings },
    ],
  },
];

export const NAV_ITEMS: NavItem[] = NAV_GROUPS.flatMap((g) => g.items);

/**
 * 带参详情路由（不进侧栏菜单，可深链/刷新/前进后退）。
 * menuKey 指向所属菜单项：详情页打开时侧栏仍高亮母页、面包屑挂在母页之下。
 * perm 与母页一致——注册与菜单可见性共用同一权限门，不越权暴露详情路由。
 */
export interface DetailRoute {
  key: string;
  /** react-router 带参路径，如 /pool-analysis/:groupId */
  path: string;
  /** 匹配当前地址用（menu 高亮/标题/面包屑），与 path 的参数段对应 */
  pattern: RegExp;
  label: string;
  perm: string;
  menuKey: string;
  page: LazyExoticComponent<ComponentType>;
  load: () => Promise<{ default: ComponentType }>;
}

export const DETAIL_ROUTES: DetailRoute[] = [
  {
    key: "pool-analysis",
    path: "/pool-analysis/:groupId",
    pattern: /^\/pool-analysis\/\d+$/,
    label: "池分析详情",
    perm: "page_pool_analysis",
    menuKey: "pools",
    page: PoolAnalysisPage,
    load: loadPoolAnalysis,
  },
  {
    key: "maintenance-project-workspace",
    path: "/maintenance/projects/:projectId",
    pattern: /^\/maintenance\/projects\/[^/]+$/,
    label: "项目详情",
    perm: "page_maintenance",
    menuKey: "maintenance-projects",
    page: MaintenanceProjectWorkspacePage,
    load: loadMaintenanceProjectWorkspace,
  },
  {
    key: "maintenance-downloads-compat",
    path: "/maintenance/downloads",
    pattern: /^\/maintenance\/downloads$/,
    label: "下载中心兼容入口",
    perm: "page_maintenance",
    menuKey: "maintenance-projects",
    page: MaintenanceDownloadsCompatRedirect,
    load: loadMaintenanceDownloadsCompat,
  },
  {
    key: "maintenance-reminders-compat",
    path: "/maintenance/reminders",
    pattern: /^\/maintenance\/reminders$/,
    label: "项目提醒兼容入口",
    perm: "page_maintenance",
    menuKey: "maintenance-projects",
    page: MaintenanceRemindersCompatRedirect,
    load: loadMaintenanceRemindersCompat,
  },
  {
    key: "maintenance-legacy",
    path: "/maintenance/legacy",
    pattern: /^\/maintenance\/legacy$/,
    label: "旧维保项目数据",
    perm: "page_maintenance",
    menuKey: "maintenance-projects",
    page: ProjectCostPage,
    load: loadProjectCost,
  },
];

/** 找到 path 对应的详情路由（正则匹配参数段） */
export function matchDetailRoute(pathname: string): DetailRoute | undefined {
  return DETAIL_ROUTES.find((r) => r.pattern.test(pathname));
}

/**
 * 旧路径 → 新路径的兼容重定向：老收藏 / 老外链仍能落到正确页面。
 * from 需与 perm 权限门一致——无权限时 App 层不注册该重定向，交给 * 回 home。
 */
export interface NavRedirect { from: string; to: string; perm?: string }
export const NAV_REDIRECTS: NavRedirect[] = [
  { from: "/purchases", to: "/purchases/analysis", perm: "page_purchases" },
  // 旧维保首页 → 项目总览
  { from: "/maintenance", to: "/maintenance/projects", perm: "page_maintenance" },
  // Beta 路径兼容跳转
  { from: "/maintenance/beta/projects", to: "/maintenance/projects", perm: "page_maintenance" },
  { from: "/maintenance/beta/project-master", to: "/maintenance/admin/project-master", perm: "page_maintenance" },
  { from: "/maintenance/beta/demands", to: "/maintenance/admin/demands", perm: "page_maintenance" },
  { from: "/maintenance/beta/warehouse", to: "/maintenance/admin/warehouse", perm: "page_maintenance" },
  { from: "/maintenance/beta/updates", to: "/maintenance/monthly-updates", perm: "page_maintenance" },
  { from: "/maintenance/beta/acceptance", to: "/maintenance/acceptance", perm: "page_maintenance" },
  { from: "/maintenance/beta/cost-refill", to: "/maintenance/admin/cost-refill", perm: "page_maintenance" },
  { from: "/maintenance/beta/migration", to: "/maintenance/admin/migration", perm: "page_maintenance" },
  { from: "/maintenance/beta/project-manager/monthly-workbook", to: "/maintenance/monthly-updates", perm: "page_maintenance" },
];

/** 找到 path 对应的导航项（精确匹配；路由也按精确注册，加子路由时两处一起改） */
export function matchNavItem(pathname: string): NavItem | undefined {
  return NAV_ITEMS.find((it) => pathname === it.path);
}

// 旧版横向菜单的排列序——默认落地页兜底必须按这个序取第一个可见项，
// 保证与重构前"零行为变化"（旧逻辑：page_parts 优先，否则 menu[0]）。
// 采购从单页拆成三页后，兜底代表项 = 采购分析（默认采购落地页）。
const LEGACY_ORDER = [
  "import", "parts", "purchases-analysis", "chat", "profit",
  "maintenance-projects", "inventory", "master", "governance", "accounts",
];

/** 登录后默认落地页：优先型号查询；否则按旧菜单序取第一个可见项 */
export function defaultPath(allowed: NavItem[]): string {
  const byKey = new Map(allowed.map((it) => [it.key, it]));
  if (byKey.has("parts")) return byKey.get("parts")!.path;
  for (const key of LEGACY_ORDER) {
    const it = byKey.get(key);
    if (it) return it.path;
  }
  return allowed[0]?.path || "/parts";
}
