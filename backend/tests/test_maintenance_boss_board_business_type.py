"""维保卡墙按业务类型筛选（2026-09-08 客户需求）。

客户口径：维保这摊子只有三种业务类型——整体维保 / 备件维保 / 算力运维；卡墙上要能按
业务类型筛，且与期限状态（进行中 / 已结束 / 期限缺失 / 回款已完成）**叠加**而不是互斥。

生产取证（2026-09-08，只读事务）：648 个项目里 **647 个 business_type 是 NULL**，
只有 1 个有值（「预交付-南京大数据…整体维保」，像是手工补的）。所以：

- **默认不排除任何一档**（后端默认 `all`、前端默认全选）。「默认排除非维保」在今天的
  生产数据上会筛掉 0 个项目，是白做；而如果为了挡住而把「未标注」也排除，卡墙会被筛空
  ——那是 R5「项目不得静默消失」事故。
- **`unlabeled` 必须是显式的一档**（NULL 与空串都归它），不能并进 `other` 被顺手挡掉。
- 隐藏必须**可计数、可撤销**：响应带 `business_type_hidden`，前端常驻「已隐藏 N 个」。

判定只读 `maintenance_project.business_type` 一列，不按挂靠单据派生：卡墙的行就是
MaintenanceProject 行，单列可直接下推 SQL，total / 分页 / 排序天然正确；派生会重演
card_status 的 total 失真，且对零单据项目给不出值。
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.config import get_settings
from app.models.maintenance_project import MaintenanceProjectContract
from app.models.maintenance_project_operations import MaintenanceCollectionSnapshot
from app.security import UserContext
from app.services import maintenance_boss_board as board
from tests.boss_board_helpers import boss_client, make_project
from tests.test_maintenance_boss_board_payment_complete import _collected, _contract
from tests.test_maintenance_boss_board_perf import count_sql


@pytest.fixture(autouse=True)
def _flag_on():
    settings = get_settings()
    original = settings.maintenance_boss_dashboard_enabled
    settings.maintenance_boss_dashboard_enabled = True
    try:
        yield
    finally:
        settings.maintenance_boss_dashboard_enabled = original


def _project(db, code, *, business_type, lifecycle="ongoing"):
    project = make_project(db, code=code, lifecycle=lifecycle)
    project.business_type = business_type
    db.commit()
    return project


def _ids(payload) -> set[str]:
    return {row["project_id"] for row in payload["rows"]}


def _get(client, **params) -> dict:
    query = "&".join(f"{key}={value}" for key, value in params.items())
    response = client.get(f"/api/maintenance/boss-board/projects?{query}")
    assert response.status_code == 200, response.text
    return response.json()


# ---------- 六档分派 ----------


def test_each_code_selects_exactly_its_own_projects(db):
    projects = {
        "overall": _project(db, "整体维保项目", business_type="整体维保"),
        "spare": _project(db, "备件维保项目", business_type="备件维保"),
        "computing": _project(db, "算力运维项目", business_type="算力运维"),
        "refit": _project(db, "拆改配服务项目", business_type="拆改配服务"),
        "other": _project(db, "整机销售项目", business_type="整机销售"),
        "blank": _project(db, "空串项目", business_type=""),
        "null": _project(db, "未标注项目", business_type=None),
    }
    client = boss_client(db)

    assert _ids(_get(client, lifecycle="all", business_type="overall")) == {
        projects["overall"].project_id
    }
    assert _ids(_get(client, lifecycle="all", business_type="refit")) == {
        projects["refit"].project_id
    }
    assert _ids(_get(client, lifecycle="all", business_type="other")) == {
        projects["other"].project_id
    }
    assert _ids(_get(client, lifecycle="all", business_type="unlabeled")) == {
        projects["blank"].project_id,
        projects["null"].project_id,
    }
    assert _ids(_get(client, lifecycle="all", business_type="overall,computing")) == {
        projects["overall"].project_id,
        projects["computing"].project_id,
    }

    every = {p.project_id for p in projects.values()}
    assert every <= _ids(_get(client, lifecycle="all", business_type="all"))
    assert every <= _ids(_get(client, lifecycle="all"))


def test_six_codes_partition_the_cohort_and_null_never_falls_out(db):
    """母集恒等式（分档版）：六档之和 == 全集，且两两不相交。

    这条是防「NULL 掉出所有档」的看门测试——SQL 三值逻辑下，无论写成 IN(三值) 还是
    NOT IN(三值)，NULL 两边都进不去，一旦这么实现，647 个生产项目会凭空消失。
    """

    _project(db, "整体维保项目", business_type="整体维保")
    _project(db, "备件维保项目", business_type="备件维保")
    _project(db, "算力运维项目", business_type="算力运维")
    _project(db, "拆改配服务项目", business_type="拆改配服务")
    _project(db, "整机销售项目", business_type="整机销售")
    _project(db, "空串项目", business_type="")
    _project(db, "未标注项目", business_type=None)
    client = boss_client(db)

    buckets = {
        code: _ids(_get(client, lifecycle="all", business_type=code))
        for code in ("overall", "spare", "computing", "refit", "other", "unlabeled")
    }
    everything = _ids(_get(client, lifecycle="all", business_type="all"))
    everything -= {board.UNASSIGNED_BUCKET}

    union = set().union(*buckets.values())
    assert union == everything
    for left in buckets:
        for right in buckets:
            if left < right:
                assert not (buckets[left] & buckets[right]), f"{left} 与 {right} 相交"


def test_trimmed_value_is_not_mistaken_for_another_business(db):
    """前后空格是氚云导出的常客：「 整体维保 」必须落 overall，不能落 other。"""

    padded = _project(db, "带空格项目", business_type=" 整体维保 ")
    client = boss_client(db)
    assert padded.project_id in _ids(
        _get(client, lifecycle="all", business_type="overall")
    )
    assert padded.project_id not in _ids(
        _get(client, lifecycle="all", business_type="other")
    )


# ---------- 与既有筛选叠加 ----------


def test_business_type_stacks_with_lifecycle(db):
    ongoing_overall = _project(db, "进行中整体维保", business_type="整体维保")
    _project(db, "进行中整机销售", business_type="整机销售")
    ended_overall = _project(
        db, "已结束整体维保", business_type="整体维保", lifecycle="ended"
    )
    client = boss_client(db)

    assert _ids(_get(client, lifecycle="ongoing", business_type="overall")) == {
        ongoing_overall.project_id
    }
    assert _ids(_get(client, lifecycle="ended", business_type="overall")) == {
        ended_overall.project_id
    }


def test_business_type_stacks_with_payment_complete_without_weakening_its_gate(db):
    """回款已完成那一档由合同财务推得，业务类型不得绕开它的权限门。"""

    paid = _project(db, "回款完成整体维保", business_type="整体维保")
    other_paid = _project(db, "回款完成整机销售", business_type="整机销售")
    for project in (paid, other_paid):
        relation = MaintenanceProjectContract(
            project_contract_id=str(uuid.uuid4()),
            project_id=project.project_id,
            contract_id=f"c-{uuid.uuid4().hex[:8]}",
            contract_no=f"XSDD-{uuid.uuid4().hex[:8].upper()}",
            amount_inc_tax=Decimal("1000.00"),
            contract_status="执行中",
            status_mapping_state="mapped",
            status_mapping_version="v1",
            included_in_total=True,
            effective_from=date(2020, 1, 1),
            source="synthetic_test",
            version=1,
        )
        db.add(relation)
        db.flush()
        db.add(
            MaintenanceCollectionSnapshot(
                collection_id=str(uuid.uuid4()),
                project_id=project.project_id,
                project_contract_id=relation.project_contract_id,
                report_month=date(2026, 8, 1),
                cumulative_amount=Decimal("1000.00"),
                status="confirmed",
                source="direct_api",
                version=1,
            )
        )
    db.commit()

    client = boss_client(db)
    assert _ids(
        _get(client, lifecycle="payment_complete", business_type="overall")
    ) == {paid.project_id}

    # 业务类型本身不需要合同财务权限
    no_profit = boss_client(db, username="board-no-profit", with_profit=False)
    assert (
        no_profit.get(
            "/api/maintenance/boss-board/projects?lifecycle=ongoing&business_type=overall"
        ).status_code
        == 200
    )
    # 但既有的 payment_complete 门禁不能被新参数绕开
    blocked = no_profit.get(
        "/api/maintenance/boss-board/projects"
        "?lifecycle=payment_complete&business_type=all"
    )
    assert blocked.status_code == 422


# ---------- 行字段与桶 ----------


def test_rows_carry_business_type_and_code(db):
    labelled = _project(db, "整体维保项目", business_type="整体维保")
    unlabelled = _project(db, "未标注项目", business_type=None)
    client = boss_client(db)

    by_id = {row["project_id"]: row for row in _get(client, lifecycle="all")["rows"]}
    assert by_id[labelled.project_id]["business_type"] == "整体维保"
    assert by_id[labelled.project_id]["business_type_code"] == "overall"
    assert by_id[unlabelled.project_id]["business_type"] is None
    assert by_id[unlabelled.project_id]["business_type_code"] == "unlabeled"


def test_unassigned_bucket_keeps_the_key_set_and_stays_out_of_filtered_views(db):
    _project(db, "整体维保项目", business_type="整体维保")
    client = boss_client(db)

    default_rows = _get(client, lifecycle="all")["rows"]
    bucket = next(r for r in default_rows if r["project_id"] == board.UNASSIGNED_BUCKET)
    assert bucket["business_type"] is None
    assert bucket["business_type_code"] == "unlabeled"

    filtered = _get(client, lifecycle="all", business_type="overall")
    assert board.UNASSIGNED_BUCKET not in _ids(filtered), (
        "桶不是项目，不该混进业务类型筛选结果"
    )


# ---------- 隐藏可计数、可撤销 ----------


def test_hidden_count_tells_the_user_what_the_filter_swallowed(db):
    _project(db, "整体维保项目", business_type="整体维保")
    _project(db, "整机销售项目一", business_type="整机销售")
    _project(db, "整机销售项目二", business_type="整机销售")
    client = boss_client(db)

    filtered = _get(client, lifecycle="all", business_type="overall")
    assert filtered["total"] == 1
    assert filtered["business_type_hidden"] == 2

    unfiltered = _get(client, lifecycle="all", business_type="all")
    assert unfiltered["business_type_hidden"] == 0


def test_hidden_count_uses_the_same_other_filters(db):
    """隐藏数必须是「同条件下去掉业务类型子句」的差，不能拿全库总数硬减。"""

    _project(db, "进行中整体维保", business_type="整体维保")
    _project(db, "进行中整机销售", business_type="整机销售")
    _project(db, "已结束整机销售", business_type="整机销售", lifecycle="ended")
    client = boss_client(db)

    ongoing = _get(client, lifecycle="ongoing", business_type="overall")
    assert ongoing["total"] == 1
    assert ongoing["business_type_hidden"] == 1, (
        "已结束那个不在 ongoing 条件内，不该计入"
    )


def _payment_pair(db, prefix, *, business_type, lifecycle):
    pair = []
    for paid in (False, True):
        project = _project(
            db,
            f"{prefix}-{int(paid)}",
            business_type=business_type,
            lifecycle=lifecycle,
        )
        _collected(db, project, _contract(db, project), "1000" if paid else "500")
        pair.append(project)
    db.commit()
    return pair  # unpaid, paid


@pytest.mark.parametrize(
    "lifecycle", ["payment_complete", "ongoing", "ended", "missing"]
)
def test_hidden_count_recomputes_payment_membership_across_business_types(
    db, lifecycle
):
    period = "ongoing" if lifecycle == "payment_complete" else lifecycle
    pairs = {
        code: _payment_pair(db, code, business_type=kind, lifecycle=period)
        for code, kind in (
            ("overall", "整体维保"),
            ("spare", "备件维保"),
            ("other", "整机销售"),
            ("unlabeled", None),
        )
    }
    client = boss_client(db)
    # Clearing only the business-type selection defines the hidden count.
    unfiltered = _get(client, lifecycle=lifecycle, business_type="all")
    assert unfiltered["total"] == 4
    index = int(lifecycle == "payment_complete")
    selected = {pairs[code][index].project_id for code in ("overall", "spare")}
    seen = set()
    for page in (1, 2, 3):
        filtered = _get(
            client,
            lifecycle=lifecycle,
            business_type="overall,spare",
            page=page,
            page_size=1,
        )
        assert filtered["total"] == 2
        assert (
            filtered["business_type_hidden"]
            == unfiltered["total"] - filtered["total"]
            == 2
        )
        assert len(filtered["rows"]) == (1 if page <= 2 else 0)
        seen |= _ids(filtered)
    assert seen == selected


@pytest.mark.parametrize(
    "lifecycle", ["payment_complete", "ongoing", "ended", "missing"]
)
def test_hidden_payment_count_preserves_query_scope_and_activity_filters(db, lifecycle):
    from tests.test_maintenance_return_receipts_api import _wbdd

    period = "ongoing" if lifecycle == "payment_complete" else lifecycle
    allowed = set()
    expected = {}
    for code, kind in (
        ("overall", "整体维保"),
        ("spare", "备件维保"),
        ("other", "整机销售"),
    ):
        pair = _payment_pair(db, f"MATCH-{code}", business_type=kind, lifecycle=period)
        allowed.update(p.project_id for p in pair)
        expected[code] = pair[int(lifecycle == "payment_complete")].project_id
    _payment_pair(db, "MATCH-outside-scope", business_type="整机销售", lifecycle=period)
    for prefix in ("OFF-query", "MATCH-activity", "MATCH-archived"):
        pair = _payment_pair(db, prefix, business_type="整机销售", lifecycle=period)
        allowed.update(p.project_id for p in pair)
        for index, project in enumerate(pair):
            if prefix == "MATCH-activity":
                _wbdd(db, project=project, order_no=f"WBDD-ACTIVITY-{index}")
            elif prefix == "MATCH-archived":
                project.is_active = False
        db.commit()
    args = dict(
        user_ctx=UserContext(user_id="viewer", role="boss"),
        lifecycle=lifecycle,
        q_text="MATCH",
        allowed_project_ids=allowed,
        has_activity=False,
        page_size=1,
    )
    unfiltered = board.projects(db, business_type="all", **args)
    filtered = board.projects(db, business_type="overall,spare", **args)
    assert unfiltered["total"] == 3
    assert filtered["total"] == 2
    assert (
        filtered["business_type_hidden"] == unfiltered["total"] - filtered["total"] == 1
    )
    assert _ids(filtered) <= {expected["overall"], expected["spare"]}


@pytest.mark.parametrize("lifecycle", ["ongoing", "ended", "missing"])
def test_hidden_count_without_contract_permission_never_computes_payment_membership(
    db, monkeypatch, lifecycle
):
    for code, kind in (("overall", "整体维保"), ("other", "整机销售")):
        _payment_pair(db, code, business_type=kind, lifecycle=lifecycle)

    def forbidden(*_args, **_kwargs):
        pytest.fail(
            "business-type hidden count must not derive payment identities without contract permission"
        )

    monkeypatch.setattr(board, "_payment_complete_ids", forbidden)
    client = boss_client(db, with_profit=False)
    unfiltered = _get(client, lifecycle=lifecycle, business_type="all")
    filtered = _get(client, lifecycle=lifecycle, business_type="overall", page_size=1)
    assert unfiltered["total"] == 4 and filtered["total"] == 2
    assert (
        filtered["business_type_hidden"] == unfiltered["total"] - filtered["total"] == 2
    )
    assert all(row["lifecycle"] == lifecycle for row in unfiltered["rows"])
    assert all(
        row["collection_preview_inc_tax"]["state"] == "restricted"
        for row in unfiltered["rows"]
    )


def test_hidden_payment_membership_uses_batched_queries_across_pages_and_project_counts(
    db,
):
    for code, kind in (("overall", "整体维保"), ("other", "整机销售")):
        _payment_pair(db, code, business_type=kind, lifecycle="ongoing")
    client = boss_client(db)
    args = dict(lifecycle="payment_complete", business_type="overall")
    with count_sql() as small:
        first = _get(client, **args, page_size=1)
    assert first["total"] == 1 and first["business_type_hidden"] == 1
    for number in range(10):
        for code, kind in (("overall", "整体维保"), ("other", "整机销售")):
            _payment_pair(
                db, f"{code}-{number}", business_type=kind, lifecycle="ongoing"
            )
    with count_sql() as larger:
        page = _get(client, **args, page_size=1)
    with count_sql() as bigger_page:
        full = _get(client, **args, page_size=50)
    assert page["total"] == full["total"] == 11
    assert page["business_type_hidden"] == full["business_type_hidden"] == 11
    assert len(full["rows"]) == 11
    assert small.selects == larger.selects == bigger_page.selects


# ---------- 参数校验 ----------


def test_chinese_literal_is_rejected(db):
    """参数值是 ASCII 码，不接受中文原文——库里是自由文本，脏值变体会被带进 URL。"""

    client = boss_client(db)
    response = client.get(
        "/api/maintenance/boss-board/projects?lifecycle=all&business_type=整体维保"
    )
    assert response.status_code == 422


def test_search_endpoint_takes_the_same_parameter(db):
    """有关键词时走 POST /projects/search，两个入口口径必须一致。"""

    overall = _project(db, "整体维保项目", business_type="整体维保")
    _project(db, "整机销售项目", business_type="整机销售")
    client = boss_client(db)

    response = client.post(
        "/api/maintenance/boss-board/projects/search",
        json={"lifecycle": "all", "business_type": "overall", "q": "项目"},
    )
    assert response.status_code == 200, response.text
    assert _ids(response.json()) == {overall.project_id}


def test_project_detail_is_never_filtered_out(db):
    """单卡详情不吃这个参数：链接与项目面板恒可达，不能因为筛选而 404。"""

    other = _project(db, "整机销售项目", business_type="整机销售")
    client = boss_client(db)
    response = client.get(f"/api/maintenance/boss-board/projects/{other.project_id}")
    assert response.status_code == 200, response.text


def test_filtering_only_ever_narrows_never_adds(db):
    """筛选只能收窄：任何一档的结果都必须是 business_type=all 结果的子集。

    防的是「把 or_ 拼错、筛选反而放进了本来看不到的项目」这一类越权旁路。
    """

    _project(db, "整体维保项目", business_type="整体维保")
    _project(db, "整机销售项目", business_type="整机销售")
    _project(db, "未标注项目", business_type=None)
    client = boss_client(db)

    everything = _ids(_get(client, lifecycle="all", business_type="all"))
    for code in board.BUSINESS_TYPE_CODES:
        assert _ids(_get(client, lifecycle="all", business_type=code)) <= everything
