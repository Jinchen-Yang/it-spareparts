import { lazy } from "react";
import type { ComponentType, LazyExoticComponent, ReactNode } from "react";
import {
  CloudUploadOutlined,
  ControlOutlined,
  DashboardOutlined,
  DeploymentUnitOutlined,
  DollarOutlined,
  FundOutlined,
  FileSearchOutlined,
  InboxOutlined,
  LineChartOutlined,
  ProfileOutlined,
  FileDoneOutlined,
  RobotOutlined,
  ScheduleOutlined,
  SearchOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
  ShoppingCartOutlined,
  TeamOutlined,
  ToolOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { readMaintenanceCapabilities } from "./components/maintenance/maintenancePermissions";

/** 读登录时写入的权限快照（与 App.tsx readPerms 同源），供 visibleWhen 判断动作键。 */
function readPermissionMap(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem("permissions") || "{}");
  } catch {
    return {};
  }
}

/**
 * 导航单一真值源：路由、侧栏菜单、面包屑、页面标题都从这里生成。
 * 新增页面只改这一处，避免"菜单有入口但路由/权限没跟上"的漂移。
 *
 * P0 壳层约定：只挂现有页面，不放"敬请期待"的空菜单项。
 */

export interface NavItem {
  /** 菜单 key，沿用旧版 page 状态字符串，保证权限/习惯连续 */
  key: string;
  /** 路由路径（可刷新、可收藏、可分享） */
  path: string;
  label: string;
  icon: ReactNode;
  /** 后端 page_* 权限键；缺省且无 anyPerm 表示仅管理员可见（如账号管理） */
  perm?: string;
  /** 任一权限即可见（与 perm 互斥；perm 优先）：给"多个动作权限共享一个入口"的页面用。
   * 互通PN池管理页 manage / set_policy 两权限各管一半操作，任何一个开都必须能进页面。 */
  anyPerm?: string[];
  /** 组合权限可见性；用于必须同时满足多项数据与动作权限的业务入口。 */
  visibleWhen?: () => boolean;
  /** 服务端总闸与实名白名单共同签发的 Beta 能力。 */
  betaFeature?: "maintenance" | "replenishment" | "maintenance_boss";
  page: LazyExoticComponent<ComponentType>;
  /** 与 page 共用同一 import() 工厂：空闲时预取，点菜单即秒开 */
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
const loadReplenishmentBeta = () => import("./pages/ReplenishmentBetaPage");
const loadPurchaseAnalysis = () => import("./pages/purchases/PurchaseAnalysisPage");
const loadPurchaseExceptions = () => import("./pages/purchases/PurchaseExceptionsPage");
const loadPurchaseRecords = () => import("./pages/purchases/PurchaseRecordsPage");
const loadProjectCost = () => import("./pages/ProjectCostPage");
const loadProjectDownloads = () => import("./pages/ProjectDownloadsPage");
const loadProjectReminders = () => import("./pages/ProjectRemindersPage");
const loadMaintenanceProjectMaster = () => import("./pages/MaintenanceProjectMasterPage");
// 维保展示板（plan v1.3 §5.1）：flag maintenance_boss 关闭时整组隐藏（后端同时 404）
const loadBossOverview = () => import("./pages/maintenance/boss/BossOverviewPage");
const loadBossProjectList = () => import("./pages/maintenance/boss/BossProjectListPage");
const loadBossProjectDrill = () => import("./pages/maintenance/boss/BossProjectDrillPage");
const loadBossUploadConsole = () => import("./pages/maintenance/boss/BossUploadConsolePage");
const loadBossProjectMaster = () => import("./pages/maintenance/boss/BossProjectMasterPage");
const loadMaintenanceWorkbench = () => import("./pages/maintenance/MaintenanceWorkbenchPage");
const loadMaintenanceSalesDashboard = () => import("./pages/maintenance/MaintenanceSalesDashboardPage");
const loadMaintenanceDemands = () => import("./pages/maintenance/MaintenanceDemandManagementPage");
const loadMaintenanceWarehouse = () => import("./pages/maintenance/MaintenanceWarehouseWorkbenchPage");
const loadMaintenanceSourceOrderAssignments = () => import("./pages/maintenance/MaintenanceSourceOrderAssignmentsPage");
const loadMaintenanceProjects = () => import("./pages/maintenance/MaintenanceProjectsPage");
const loadMaintenanceProjectWorkspace = () => import("./pages/maintenance/MaintenanceProjectWorkspacePage");
const loadMaintenanceProjectUpdates = () => import("./pages/maintenance/MaintenanceProjectUpdatesPage");
const loadMaintenanceManagerWorkbook = () => import("./pages/maintenance/MaintenanceManagerWorkbookPage");
const loadMaintenanceAcceptance = () => import("./pages/maintenance/MaintenanceAcceptancePage");
const loadMaintenanceCostRefill = () => import("./pages/maintenance/MaintenanceCostRefillPage");
const loadMaintenanceMigration = () => import("./pages/maintenance/MaintenanceMigrationPage");
const loadMaintenanceCollectionReminders = () => import("./pages/maintenance/MaintenanceCollectionRemindersPage");
const loadMaintenanceBetaCompat = () => import("./pages/maintenance/MaintenanceBetaCompatRedirect");
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
const ReplenishmentBetaPage = lazy(loadReplenishmentBeta);
const PurchaseAnalysisPage = lazy(loadPurchaseAnalysis);
const PurchaseExceptionsPage = lazy(loadPurchaseExceptions);
const PurchaseRecordsPage = lazy(loadPurchaseRecords);
const ProjectCostPage = lazy(loadProjectCost);
const ProjectDownloadsPage = lazy(loadProjectDownloads);
const ProjectRemindersPage = lazy(loadProjectReminders);
const MaintenanceProjectMasterPage = lazy(loadMaintenanceProjectMaster);
const BossOverviewPage = lazy(loadBossOverview);
const BossProjectListPage = lazy(loadBossProjectList);
const BossProjectDrillPage = lazy(loadBossProjectDrill);
const BossUploadConsolePage = lazy(loadBossUploadConsole);
const BossProjectMasterPage = lazy(loadBossProjectMaster);
const MaintenanceWorkbenchPage = lazy(loadMaintenanceWorkbench);
const MaintenanceSalesDashboardPage = lazy(loadMaintenanceSalesDashboard);
const MaintenanceDemandManagementPage = lazy(loadMaintenanceDemands);
const MaintenanceWarehouseWorkbenchPage = lazy(loadMaintenanceWarehouse);
const MaintenanceSourceOrderAssignmentsPage = lazy(loadMaintenanceSourceOrderAssignments);
const MaintenanceProjectsPage = lazy(loadMaintenanceProjects);
const MaintenanceProjectWorkspacePage = lazy(loadMaintenanceProjectWorkspace);
const MaintenanceProjectUpdatesPage = lazy(loadMaintenanceProjectUpdates);
const MaintenanceManagerWorkbookPage = lazy(loadMaintenanceManagerWorkbook);
const MaintenanceAcceptancePage = lazy(loadMaintenanceAcceptance);
const MaintenanceCostRefillPage = lazy(loadMaintenanceCostRefill);
const MaintenanceMigrationPage = lazy(loadMaintenanceMigration);
const MaintenanceCollectionRemindersPage = lazy(loadMaintenanceCollectionReminders);
const MaintenanceBetaCompatRedirect = lazy(loadMaintenanceBetaCompat);
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
      { key: "replenishment-beta", path: "/sales/replenishment-beta", label: "补库申请", icon: <ShoppingCartOutlined />, perm: "page_replenishment_beta", betaFeature: "replenishment", page: ReplenishmentBetaPage, load: loadReplenishmentBeta },
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
    key: "grp-maintenance",
    label: "维保项目（旧版）",
    items: [
      { key: "maintenance", path: "/maintenance", label: "项目数据", icon: <ToolOutlined />, perm: "page_maintenance", page: ProjectCostPage, load: loadProjectCost },
      { key: "maintenance-downloads", path: "/maintenance/downloads", label: "下载中心", icon: <CloudUploadOutlined />, perm: "page_maintenance", page: ProjectDownloadsPage, load: loadProjectDownloads },
      { key: "maintenance-reminders", path: "/maintenance/reminders", label: "项目提醒", icon: <WarningOutlined />, perm: "page_maintenance", page: ProjectRemindersPage, load: loadProjectReminders },
    ],
  },
  {
    key: "grp-maintenance-beta",
    label: "维保工作台",
    items: [
      // 维保模块第一入口：登录后的维保落地页（我的维保）。
      { key: "maintenance-workbench", path: "/maintenance/beta/workbench", label: "我的维保", icon: <ProfileOutlined />, perm: "page_maintenance_beta", betaFeature: "maintenance", page: MaintenanceWorkbenchPage, load: loadMaintenanceWorkbench },
      // 销售看板：仅 admin/boss 可见（visibleWhen 按角色判断，不绑定可委派权限键）。
      { key: "maintenance-sales-dashboard", path: "/maintenance/beta/sales-dashboard", label: "销售看板", icon: <LineChartOutlined />, visibleWhen: () => {
        const role = localStorage.getItem("role") || "";
        return role === "admin" || role === "boss";
      }, page: MaintenanceSalesDashboardPage, load: loadMaintenanceSalesDashboard },
      { key: "maintenance-projects", path: "/maintenance/beta/projects", label: "项目总览", icon: <DashboardOutlined />, perm: "page_maintenance_beta", betaFeature: "maintenance", page: MaintenanceProjectsPage, load: loadMaintenanceProjects },
      { key: "maintenance-updates", path: "/maintenance/beta/updates", label: "月度项目更新", icon: <CloudUploadOutlined />, betaFeature: "maintenance", visibleWhen: () => {
        const capabilities = readMaintenanceCapabilities();
        return capabilities.canUseBeta && capabilities.canApplyRoundtrip;
      }, page: MaintenanceProjectUpdatesPage, load: loadMaintenanceProjectUpdates },
      { key: "maintenance-manager-workbook", path: "/maintenance/beta/project-manager/monthly-workbook", label: "经理月报", icon: <CloudUploadOutlined />, betaFeature: "maintenance", visibleWhen: () => readMaintenanceCapabilities().canUseManagerWorkbook, page: MaintenanceManagerWorkbookPage, load: loadMaintenanceManagerWorkbook },
      { key: "maintenance-acceptance", path: "/maintenance/beta/acceptance", label: "验收与结项", icon: <FileDoneOutlined />, perm: "page_maintenance_beta", betaFeature: "maintenance", page: MaintenanceAcceptancePage, load: loadMaintenanceAcceptance },
      { key: "maintenance-collection-reminders", path: "/maintenance/beta/collection-reminders", label: "回款提醒", icon: <ScheduleOutlined />, perm: "page_maintenance_beta", betaFeature: "maintenance", page: MaintenanceCollectionRemindersPage, load: loadMaintenanceCollectionReminders },
    ],
  },
  {
    // 维保展示板（plan v1.3）：老板/管理层看数决策入口。整组受服务端总闸
    // maintenance_boss_dashboard_enabled 控制（关闭时前端隐藏 + 后端 404 双保险）。
    key: "grp-maintenance-boss",
    label: "维保展示板",
    items: [
      { key: "maintenance-boss-overview", path: "/maintenance/boss", label: "展示板首屏", icon: <DashboardOutlined />, anyPerm: ["page_maintenance_boss", "page_maintenance"], betaFeature: "maintenance_boss", page: BossOverviewPage, load: loadBossOverview },
      { key: "maintenance-boss-projects", path: "/maintenance/boss/projects", label: "全部项目", icon: <ProfileOutlined />, anyPerm: ["page_maintenance_boss", "page_maintenance"], betaFeature: "maintenance_boss", page: BossProjectListPage, load: loadBossProjectList },
      { key: "maintenance-boss-uploads", path: "/maintenance/boss/uploads", label: "数据上传", icon: <CloudUploadOutlined />, betaFeature: "maintenance_boss", visibleWhen: () => {
        const perms = readPermissionMap();
        return !!perms.action_maintenance_wbdd_import || !!perms.action_maintenance_doc_import;
      }, page: BossUploadConsolePage, load: loadBossUploadConsole },
      { key: "maintenance-boss-master", path: "/maintenance/boss/master", label: "项目归属确认", icon: <DeploymentUnitOutlined />, betaFeature: "maintenance_boss", visibleWhen: () => readMaintenanceCapabilities().canManageProject, page: BossProjectMasterPage, load: loadBossProjectMaster },
    ],
  },
  {
    key: "grp-maintenance-admin",
    label: "维保数据维护",
    items: [
      // 后端 require_admin：无 perm + 无 visibleWhen → 仅 admin 可见入口
      // （App 层过滤：admin 恒短路通过，非 admin 无权限键则为 false）

      { key: "maintenance-project-master", path: "/maintenance/beta/project-master", label: "项目主档维护", icon: <ProfileOutlined />, perm: "page_maintenance_beta", betaFeature: "maintenance", page: MaintenanceProjectMasterPage, load: loadMaintenanceProjectMaster },
      { key: "maintenance-demands", path: "/maintenance/beta/demands", label: "异常维保单处理", icon: <FileSearchOutlined />, perm: "page_maintenance_beta", betaFeature: "maintenance", page: MaintenanceDemandManagementPage, load: loadMaintenanceDemands },
      { key: "maintenance-warehouse", path: "/maintenance/beta/warehouse", label: "仓库单据核对", icon: <InboxOutlined />, perm: "page_maintenance_beta", betaFeature: "maintenance", page: MaintenanceWarehouseWorkbenchPage, load: loadMaintenanceWarehouse },
      { key: "maintenance-cost-refill", path: "/maintenance/beta/cost-refill", label: "领用缺价补录", icon: <DollarOutlined />, betaFeature: "maintenance", visibleWhen: () => readMaintenanceCapabilities().canManageProject, page: MaintenanceCostRefillPage, load: loadMaintenanceCostRefill },
      { key: "maintenance-migration", path: "/maintenance/beta/migration", label: "历史数据迁移核对", icon: <SafetyCertificateOutlined />, betaFeature: "maintenance", visibleWhen: () => readMaintenanceCapabilities().canReviewMigration, page: MaintenanceMigrationPage, load: loadMaintenanceMigration },
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
  /** 与母页同门；anyPerm 用于「任一权限即可进」的入口（如维保展示板）。 */
  perm?: string;
  anyPerm?: string[];
  betaFeature?: "maintenance" | "replenishment" | "maintenance_boss";
  menuKey: string;
  page: LazyExoticComponent<ComponentType>;
  load: () => Promise<{ default: ComponentType }>;
}

export const DETAIL_ROUTES: DetailRoute[] = [
  {
    key: "maintenance-source-order-assignments",
    path: "/maintenance/beta/project-master/source-orders",
    pattern: /^\/maintenance\/beta\/project-master\/source-orders$/,
    label: "历史维保单归属",
    perm: "page_maintenance_beta",
    betaFeature: "maintenance",
    menuKey: "maintenance-project-master",
    page: MaintenanceSourceOrderAssignmentsPage,
    load: loadMaintenanceSourceOrderAssignments,
  },
  {
    key: "maintenance-boss-project-drill",
    path: "/maintenance/boss/projects/:projectId",
    pattern: /^\/maintenance\/boss\/projects\/[^/]+$/,
    label: "项目单据证据",
    anyPerm: ["page_maintenance_boss", "page_maintenance"],
    betaFeature: "maintenance_boss",
    menuKey: "maintenance-boss-projects",
    page: BossProjectDrillPage,
    load: loadBossProjectDrill,
  },
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
    path: "/maintenance/beta/projects/:projectId",
    pattern: /^\/maintenance\/beta\/projects\/[^/]+$/,
    label: "维保项目详情",
    perm: "page_maintenance_beta",
    betaFeature: "maintenance",
    menuKey: "maintenance-projects",
    page: MaintenanceProjectWorkspacePage,
    load: loadMaintenanceProjectWorkspace,
  },
  ...[
    ["maintenance-projects-compat", "/maintenance/projects", /^\/maintenance\/projects$/],
    ["maintenance-project-workspace-compat", "/maintenance/projects/:projectId", /^\/maintenance\/projects\/[^/]+$/],
    ["maintenance-project-master-compat", "/maintenance/project-master", /^\/maintenance\/project-master$/],
    ["maintenance-source-orders-compat", "/maintenance/project-master/source-orders", /^\/maintenance\/project-master\/source-orders$/],
    ["maintenance-demands-compat", "/maintenance/demands", /^\/maintenance\/demands$/],
    ["maintenance-warehouse-compat", "/maintenance/warehouse", /^\/maintenance\/warehouse$/],
    ["maintenance-manager-workbook-compat", "/maintenance/project-manager/monthly-workbook", /^\/maintenance\/project-manager\/monthly-workbook$/],
    ["maintenance-acceptance-compat", "/maintenance/acceptance", /^\/maintenance\/acceptance$/],
    ["maintenance-updates-compat", "/maintenance/updates", /^\/maintenance\/updates$/],
    ["maintenance-cost-refill-compat", "/maintenance/cost-refill", /^\/maintenance\/cost-refill$/],
    ["maintenance-migration-compat", "/maintenance/migration", /^\/maintenance\/migration$/],
  ].map(([key, path, pattern]) => ({
    key: key as string,
    path: path as string,
    pattern: pattern as RegExp,
    label: "维保版本兼容入口",
    perm: "page_maintenance",
    menuKey: "maintenance",
    page: MaintenanceBetaCompatRedirect,
    load: loadMaintenanceBetaCompat,
  })),
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
  // 采购拆分：旧 /purchases 收藏跳到默认采购子页（采购分析）
  { from: "/purchases", to: "/purchases/analysis", perm: "page_purchases" },
  // 集成期曾使用 /maintenance/legacy；发布 Beta 后恢复 /maintenance 为稳定版默认入口。
  { from: "/maintenance/legacy", to: "/maintenance", perm: "page_maintenance" },
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
  "maintenance", "inventory", "master", "governance", "accounts",
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
