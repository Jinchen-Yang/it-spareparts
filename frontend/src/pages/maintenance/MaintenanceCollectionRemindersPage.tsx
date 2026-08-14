import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, ConfigProvider, Empty, Input, Pagination, Select, Space, Tag, message } from "antd";

import {
  getCollectionMilestones,
  searchCollectionReminders,
  type CollectionDirectoryRow,
  type CollectionFollowUpAction,
  type CollectionMilestoneRow,
  type CollectionOwnerScope,
  type CollectionProjectDetailResponse,
  type CollectionProjectRef,
  type CollectionReminderDirectoryResponse,
  type CollectionReminderState,
} from "../../api/maintenanceCollectionReminders";
import CollectionMilestoneFollowUpModal from "../../components/maintenance/CollectionMilestoneFollowUpModal";
import CollectionPlanImportModal from "../../components/maintenance/CollectionPlanImportModal";
import CollectionReminderDetail from "../../components/maintenance/CollectionReminderDetail";
import "../../components/maintenance/maintenanceCollectionReminders.css";
import {
  COLLECTION_FOLLOW_UP,
  COLLECTION_PAGE,
  COLLECTION_STATE_LABELS,
  COLLECTION_STATE_OPTIONS,
} from "../../components/maintenance/maintenanceLanguage";
import { readMaintenanceCapabilities } from "../../components/maintenance/maintenancePermissions";
import MobileDetailDrawer from "../../components/MobileDetailDrawer";
import type { DetailField } from "../../components/MobileDetailDrawer";
import PageHeader from "../../components/PageHeader";

const PAGE_SIZE = 24;

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const mql = window.matchMedia(query);
    setMatches(mql.matches);
    const handler = (event: MediaQueryListEvent) => setMatches(event.matches);
    mql.addEventListener("change", handler);
    return () => mql.removeEventListener("change", handler);
  }, [query]);
  return matches;
}

function managerName(project: CollectionProjectRef): string {
  const { display_name, username } = project.manager_assignment;
  return display_name || username || "—";
}

function stateTag(state: CollectionReminderState) {
  const item = COLLECTION_STATE_LABELS[state] ?? { label: state };
  return <Tag color={item.color}>{item.label}</Tag>;
}

export default function MaintenanceCollectionRemindersPage() {
  const [directory, setDirectory] = useState<CollectionReminderDirectoryResponse | null>(null);
  const [directoryRows, setDirectoryRows] = useState<CollectionDirectoryRow[]>([]);
  const [detail, setDetail] = useState<CollectionProjectDetailResponse | null>(null);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [q, setQ] = useState("");
  const [ownerScope, setOwnerScope] = useState<CollectionOwnerScope>("me");
  const [stateFilter, setStateFilter] = useState<CollectionReminderState | undefined>();
  const [page, setPage] = useState(1);
  const [loadingDirectory, setLoadingDirectory] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState(false);
  const [followUp, setFollowUp] = useState<{
    row: CollectionMilestoneRow;
    action: CollectionFollowUpAction;
  } | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const directoryGeneration = useRef(0);
  const detailGeneration = useRef(0);
  const detailControllerRef = useRef<AbortController | null>(null);
  const followUpTriggerRef = useRef<HTMLElement | null>(null);
  const capabilities = useMemo(readMaintenanceCapabilities, []);
  const isDesktop = useMediaQuery("(min-width: 768px)");
  const isWide = useMediaQuery("(min-width: 1200px)");

  const loadDirectory = useCallback((nextPage: number, query: string) => {
    const request = ++directoryGeneration.current;
    const controller = new AbortController();
    setLoadingDirectory(true);
    setError(null);
    void searchCollectionReminders({
      q: query,
      owner_scope: ownerScope,
      reminder_state: stateFilter ?? null,
      page: nextPage,
      page_size: PAGE_SIZE,
    }, { signal: controller.signal }).then(({ data }) => {
      if (request !== directoryGeneration.current) {
        return;
      }
      setDirectory(data);
      setPage(data.page);
      setDirectoryRows(data.rows);
      setSelectedProjectId((current) => {
        if (current && data.rows.some((row) => row.project.project_id === current)) return current;
        return data.rows[0]?.project.project_id ?? "";
      });
    }).catch((reason_: unknown) => {
      if (controller.signal.aborted || request !== directoryGeneration.current) return;
      setDirectory(null);
      const status = (reason_ as { response?: { status?: number } })?.response?.status;
      setError(
        status === 403
          ? COLLECTION_PAGE.permissionDenied
          : status === 409
            ? COLLECTION_PAGE.versionConflict
            : COLLECTION_PAGE.loadFailed,
      );
    }).finally(() => {
      if (request === directoryGeneration.current) setLoadingDirectory(false);
    });
    return () => controller.abort();
  }, [ownerScope, stateFilter]);

  const loadDetail = useCallback((projectId: string) => {
    if (!projectId) {
      setDetail(null);
      setDetailError(false);
      return undefined;
    }
    const request = ++detailGeneration.current;
    detailControllerRef.current?.abort();
    const controller = new AbortController();
    detailControllerRef.current = controller;
    setLoadingDetail(true);
    setDetail(null);
    setDetailError(false);
    void getCollectionMilestones(projectId, { signal: controller.signal }).then(({ data }) => {
      if (request !== detailGeneration.current) return;
      if (data.project.project_id !== projectId) {
        setDetailError(true);
        return;
      }
      setDetail(data);
    }).catch(() => {
      if (controller.signal.aborted || request !== detailGeneration.current) return;
      setDetail(null);
      setDetailError(true);
    }).finally(() => {
      if (request === detailGeneration.current) setLoadingDetail(false);
    });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const cancel = loadDirectory(page, q);
    return cancel;
  }, [loadDirectory, page, q]);

  useEffect(() => {
    const cancel = loadDetail(selectedProjectId);
    return cancel;
  }, [loadDetail, selectedProjectId]);

  const selectProject = (projectId: string) => {
    setSelectedProjectId(projectId);
    if (!isDesktop) setDrawerOpen(true);
  };

  const openFollowUp = (
    row: CollectionMilestoneRow,
    action: CollectionFollowUpAction,
    event: React.MouseEvent<HTMLElement>,
  ) => {
    followUpTriggerRef.current = event.currentTarget;
    setFollowUp({ row, action });
  };

  const handleFollowUpSubmitted = () => {
    setFollowUp(null);
    message.success(COLLECTION_FOLLOW_UP.submitSuccess);
    void loadDirectory(page, q);
    void loadDetail(selectedProjectId);
  };

  const drawerFields: DetailField[] = detail ? [
    { label: COLLECTION_PAGE.managerLabel, value: managerName(detail.project) },
    {
      label: COLLECTION_PAGE.servicePeriodLabel,
      value: detail.project.service_period.service_start
        ? `${detail.project.service_period.service_start} ~ ${detail.project.service_period.service_end ?? "—"}`
        : "—",
    },
    {
      label: COLLECTION_PAGE.contractsLabel,
      value: detail.project.contracts.map((c) => c.contract_no).filter(Boolean).join("、") || "—",
    },
  ] : [];

  const directoryList = (
    <div>
      <div className="mcr-directory-list" style={{ display: "grid", gap: 8, minWidth: 0 }}>
        {directoryRows.map((row) => {
          const selected = row.project.project_id === selectedProjectId;
          const next = row.next_actionable_milestone;
          return (
            <Button
              key={row.project.project_id}
              block
              data-testid={`mcr-row-${row.project.project_id}`}
              aria-current={selected ? "true" : undefined}
              type={selected ? "primary" : "default"}
              className="mcr-directory-row"
              onClick={() => selectProject(row.project.project_id)}
            >
              <span className="mcr-directory-row-main">{row.project.display_name}</span>
              <span className="mcr-directory-row-sub">
                {row.project.project_code} · {managerName(row.project)}
              </span>
              <span className="mcr-directory-row-sub">
                {next ? (
                  <>
                    {stateTag(next.reminder_state)}
                    {next.planned_month ?? "—"}
                    {" · "}
                    {COLLECTION_PAGE.sequenceOf(next.sequence)}
                  </>
                ) : (
                  COLLECTION_PAGE.noActionable
                )}
              </span>
            </Button>
          );
        })}
        {!loadingDirectory && directoryRows.length === 0 && (
          <Empty description={COLLECTION_PAGE.emptyDirectory} />
        )}
      </div>
      <Pagination
        current={page}
        pageSize={PAGE_SIZE}
        total={directory?.total ?? 0}
        showSizeChanger={false}
        onChange={(nextPage) => setPage(nextPage)}
        style={{ marginTop: 12 }}
      />
    </div>
  );

  return (
    <ConfigProvider button={{ autoInsertSpace: false }}>
      <div
        data-testid="collection-reminders-page"
        className="collection-reminders-page"
        style={{ maxWidth: "100%", overflowX: "hidden" }}
      >
      <PageHeader
        title={COLLECTION_PAGE.title}
        subtitle={COLLECTION_PAGE.subtitle}
        extra={(
          <Button
            disabled={!capabilities.canImportCollectionPlan}
            onClick={() => setImportOpen(true)}
          >
            {COLLECTION_PAGE.importPlan}
          </Button>
        )}
      />
      <Space wrap>
        <Input.Search
          aria-label={COLLECTION_PAGE.searchLabel}
          allowClear
          placeholder={COLLECTION_PAGE.searchPlaceholder}
          style={{ width: 320, maxWidth: "100%" }}
          onSearch={(value) => {
            setPage(1);
            setQ(value.trim());
          }}
        />
        <span>
          <label htmlFor="mcr-owner-scope" style={{ marginRight: 8 }}>
            {COLLECTION_PAGE.ownerScopeLabel}
          </label>
          <Select
            id="mcr-owner-scope"
            value={ownerScope}
            virtual={false}
            style={{ width: 140 }}
            options={[
              { label: COLLECTION_PAGE.ownerScopeMe, value: "me" },
              ...(directory?.allowed_owner_scopes?.includes("all")
                ? [{ label: COLLECTION_PAGE.ownerScopeAll, value: "all" as const }]
                : []),
            ]}
            onChange={(value: CollectionOwnerScope) => {
              setPage(1);
              setOwnerScope(value);
            }}
          />
        </span>
        <span>
          <label htmlFor="mcr-state-filter" style={{ marginRight: 8 }}>
            {COLLECTION_PAGE.stateFilterLabel}
          </label>
          <Select
            id="mcr-state-filter"
            allowClear
            virtual={false}
            placeholder={COLLECTION_PAGE.stateAll}
            style={{ width: 160 }}
            options={COLLECTION_STATE_OPTIONS.filter((option) => option.value !== "all")}
            onChange={(value?: CollectionReminderState) => {
              setPage(1);
              setStateFilter(value);
            }}
          />
        </span>
      </Space>
      {error && (
        <div style={{ marginTop: 12 }}>
          <Alert type="error" showIcon message={error} />
          <Button style={{ marginTop: 8 }} onClick={() => void loadDirectory(page, q)}>
            {COLLECTION_PAGE.retry}
          </Button>
        </div>
      )}
      {isDesktop ? (
        <div
          data-testid="mcr-master-detail"
          className="mcr-master-detail"
          style={{
            display: "grid",
            gap: 16,
            minWidth: 0,
            gridTemplateColumns: isWide
              ? "minmax(0, 38fr) minmax(0, 62fr)"
              : "minmax(0, 42fr) minmax(0, 58fr)",
          }}
        >
          <div className="mcr-pane" style={{ minWidth: 0 }}>{directoryList}</div>
          <div className="mcr-pane" style={{ minWidth: 0 }}>
            <CollectionReminderDetail
              detail={detail}
              loading={loadingDetail}
              error={detailError}
              selected={selectedProjectId !== ""}
              capabilities={capabilities}
              actionsHidden={followUp != null}
              onFollowUp={openFollowUp}
              onImportPlan={() => setImportOpen(true)}
              onRetry={() => void loadDetail(selectedProjectId)}
            />
          </div>
        </div>
      ) : (
        <>
          <div className="mcr-pane" style={{ minWidth: 0, marginTop: 12 }}>{directoryList}</div>
          <MobileDetailDrawer
            open={drawerOpen}
            title={COLLECTION_PAGE.title}
            fields={drawerFields}
            height="100%"
            onClose={() => setDrawerOpen(false)}
          >
            {detail && (
              <CollectionReminderDetail
                detail={detail}
                loading={loadingDetail}
                error={detailError}
                selected={selectedProjectId !== ""}
                capabilities={capabilities}
                actionsHidden={followUp != null}
                onFollowUp={openFollowUp}
                onImportPlan={() => setImportOpen(true)}
                onRetry={() => void loadDetail(selectedProjectId)}
              />
            )}
          </MobileDetailDrawer>
        </>
      )}
      <CollectionMilestoneFollowUpModal
        open={followUp != null}
        milestone={followUp?.row ?? null}
        action={followUp?.action ?? null}
        triggerRef={followUpTriggerRef}
        onClose={() => setFollowUp(null)}
        onSubmitted={handleFollowUpSubmitted}
      />
        <CollectionPlanImportModal
          open={importOpen}
          onClose={() => setImportOpen(false)}
          onApplied={() => {
            setImportOpen(false);
            void loadDirectory(page, q);
            void loadDetail(selectedProjectId);
          }}
        />
      </div>
    </ConfigProvider>
  );
}
