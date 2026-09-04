"""修复模式（以本表为准）**删除侧**的唯一权威。

`loader` 执行作废、`pipeline` 判门禁、（后续的）导入前预演都只消费这里的函数：
规则只写一处，预演与实际执行在结构上不可能分叉。这不是整洁性偏好——「预演说作废
0 行、实际作废 500 行」比没有预演更糟，而防止它的唯一结构性办法就是不存在第二份
实现。

## 删除侧的两道抑制（都是 fail-closed，抑制的只是删除，同键覆盖不受影响）

**1. 本表有行被排除（`dropped_no_contract > 0`）**
重建范围是**合同粒度**的：`contracts` 只由存活行贡献，但既有行按合同被整段拉进来。
于是一条被判 `missing_link` 而排除的行，会落进别人贡献的合同范围里被软作废——文件里
它还在，系统当它消失了，批次却报 success。
不做「按身份逐行豁免」是因为**任何一次不匹配都不构成「这条旧行真的消失了」的证据**：
源系统改一次金额，内容签名就对不上；旧行还可能是在源表尚无「数据ID」列的年代按内容
键入库的，此时连最强的一族也匹配不上。（初版做过三族比对，Codex P1 指出弱签名假阴性，
复核后发现前提在任何一族上都不成立，整个拆掉。）

**2. 本次触及多个合同（`len(contracts) > 1`）**
「以本表为准」的删除承诺只有在「本表完整覆盖了它触及的合同」时才成立，而这一点文件
自己证明不了。修复模式的设计形态是**单合同**的工作簿报销页往返（renderer 的说明即
如此）；多合同全公司导出触发合同级全量替换是实现的副作用，不是设计意图——它会把这些
合同名下**本表未覆盖时段**的历史行一并作废。

第 2 道是 2026-09-04 实测出来的：客户按回执提示把「销售订单」列为空的行滤掉后重导，
零错误、`dropped_no_contract=0`，第 1 道失效，789 个合同全域作废全速执行，实测
作废 431 行、¥497,806.94 从存活金额里消失，批次报 success。那 431 行是报销单一单多行
时靠单头继承 XSDD 的明细行——用户按单元格是否为空来过滤，看不出它们与公司开销行的区别。
"""
from dataclasses import dataclass
from typing import Iterable, Mapping

from app.etl.transform import TransformResult

# 已经不生效的状态：再作废一次没有意义，也不该计入回执
VOID_STATUSES = frozenset({"已作废", "作废"})

# 修复模式门禁唯一放行的错误类型。其余错误全部在 transform 求出 xsdd **之前**
# 就 continue，那些行可能带着完整合同号，error_type 对「这行属不属于某个被重建
# 的合同」零信息量，故一律拦批。duplicate_key 更要拦：撞键的第一行已进 lines 并
# 会 UPDATE 掉库里的记录，是静默覆盖而非漏入库。
NON_BLOCKING_ERROR_TYPES = frozenset({"missing_link"})

SUPPRESS_DROPPED = "dropped_no_contract"
SUPPRESS_MULTI_CONTRACT = "multi_contract"
SUPPRESS_UNANCHORED = "unanchored"


@dataclass(frozen=True)
class VoidInputs:
    """作废判定的全部输入。只依赖 TransformResult，与库状态无关。"""

    upsert: bool
    incoming_ids: frozenset
    contracts: frozenset
    dropped_no_contract: int
    # 每张报销页都带页级锚（系统导出的项目工作簿报销页形态）。这是「设计形态 =
    # 单合同工作簿报销页往返」的结构信号：无锚的逐行表即使只写了一个合同号，
    # 「本表完整覆盖该合同」也只是用户断言——把多合同导出按合同拆成多份单合同
    # 逐行表分次上传，就能绕过多合同抑制重现丢账路径（对抗核验实跑证实）。
    anchored: bool = False

    @property
    def suppressed_reason(self) -> str | None:
        """非 None ⇒ 本批不执行任何作废（退化为「覆盖同键、只增不删」）。

        contracts 用原串比较（与 scope 扩宽的精确匹配一致，不走归属侧的
        normalize_contract_no）：同一合同写成 XSDD-1 / xsdd-1 / 缺前缀 的混合形态
        会被算作多合同——方向是 fail-closed，回执与预检文案需提示这一点。
        """
        if not self.upsert:
            return None
        if self.dropped_no_contract:
            return SUPPRESS_DROPPED
        if len(self.contracts) > 1:
            return SUPPRESS_MULTI_CONTRACT
        if self.contracts and not self.anchored:
            return SUPPRESS_UNANCHORED
        return None


@dataclass(frozen=True)
class VoidDecision:
    void_ids: tuple          # 本次真正置「已作废」的 raw_line_id
    protected_ids: tuple     # 本该作废、但因抑制而保留的
    already_void_ids: tuple  # 本来就已作废，不重复计数
    suppressed_reason: str | None


def dropped_no_contract(result: TransformResult) -> int:
    """本批因缺销售订单被排除的行数。"""
    return sum(1 for e in result.errors if e.error_type == "missing_link")


def blocking_errors(result: TransformResult) -> list:
    """修复模式下会导致整批拒绝的错误行（供 pipeline 门禁与预演共用）。"""
    return [e for e in result.errors
            if e.error_type not in NON_BLOCKING_ERROR_TYPES]


def plan_inputs(result: TransformResult, *, mode: str) -> VoidInputs:
    return VoidInputs(
        upsert=(mode == "upsert"),
        incoming_ids=frozenset(
            str(ln["raw_line_id"]) for ln in result.lines if ln.get("raw_line_id")),
        contracts=frozenset(
            ln["linked_sales_order_no"] for ln in result.lines
            if ln.get("linked_sales_order_no")),
        dropped_no_contract=dropped_no_contract(result),
        anchored=bool(result.expense_anchors) and all(result.expense_anchors),
    )


def classify(existing: Mapping, inputs: VoidInputs) -> VoidDecision:
    """既有行（raw_line_id -> 行对象/投影）→ 本次的作废判定。

    `existing` 只需每个值有 `.data_status`；ORM 实体与 Core 投影行都满足，
    因此预演可以走无锁投影、loader 走加锁实体，判定逻辑仍是同一份。
    """
    if not inputs.upsert:
        return VoidDecision((), (), (), None)
    reason = inputs.suppressed_reason
    missing = sorted(set(existing) - set(inputs.incoming_ids))
    already, candidates = [], []
    for raw_id in missing:
        if getattr(existing[raw_id], "data_status", None) in VOID_STATUSES:
            already.append(raw_id)
        else:
            candidates.append(raw_id)
    if reason is not None:
        return VoidDecision((), tuple(candidates), tuple(already), reason)
    return VoidDecision(tuple(candidates), (), tuple(already), None)


def scope_contracts(inputs: VoidInputs) -> list:
    """upsert 时按合同扩宽既有行范围所用的合同列表（空 ⇒ 不扩宽）。

    抑制在查库前就已知时不扩宽：否则多合同全公司导出仍会把 790 个合同名下的全部
    旧行锁住、逐行跑归属同步（可能因无关旧行的完整性错误拒批），并把整个合同域报
    成「覆盖范围」。被保留的行数由 loader 用 COUNT 单独统计。
    """
    if not inputs.upsert or not inputs.contracts or inputs.suppressed_reason:
        return []
    return sorted(inputs.contracts)


def iter_error_types(errors: Iterable) -> set:
    return {e.error_type for e in errors}
