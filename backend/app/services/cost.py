"""成本时间回放引擎（§7.2，甲方口径：移动加权 + FIFO）。

纯函数,不碰 DB,便于单测。对单个 pn_std,把采购"入"与销售"出"按日期排队回放,
一次回放同时产出每笔销售的「移动加权」与「FIFO」单位成本。

约定:
- 价格均为不含税单价(由 profit 层换算后传入)。
- 同一天:采购先于销售(当天进的货当天可用)。
- 期初/扣穿兜底:fallback_price(该型号最近采购价,见 OPENING_COST_POLICY);
  无任何采购的型号 fallback=None → 该行成本 None、source='none'。
"""
from collections import deque
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

_CENT = Decimal("0.01")


@dataclass
class PurchaseEvent:
    on: date | None
    qty: Decimal
    price: Decimal       # 不含税单价


@dataclass
class SaleEvent:
    on: date | None
    sale_id: int
    qty: Decimal


@dataclass
class LineCost:
    moving_avg: Decimal | None
    fifo: Decimal | None
    source: str          # computed | fallback | none


def _key(d: date | None):
    return (0, d) if d is not None else (-1, date.min)


def replay(purchases: list[PurchaseEvent], sales: list[SaleEvent],
           fallback_price: Decimal | None) -> dict[int, LineCost]:
    """回放单个 pn 的事件,返回 {sale_id: LineCost}。"""
    events: list[tuple] = [( _key(p.on), 0, p) for p in purchases]
    events += [(_key(s.on), 1, s) for s in sales]
    events.sort(key=lambda e: (e[0], e[1]))

    layers: deque[list] = deque()       # FIFO 批次层 [qty, price]
    mq = Decimal(0)                      # 移动加权:在库数量
    mv = Decimal(0)                      # 移动加权:在库总值
    out: dict[int, LineCost] = {}

    for _, typ, ev in events:
        if typ == 0:                     # 采购入库
            if ev.qty and ev.qty > 0 and ev.price is not None:
                layers.append([ev.qty, ev.price])
                mq += ev.qty
                mv += ev.qty * ev.price
            continue

        qty = ev.qty or Decimal(0)
        used_fb = False

        # ---- 移动加权 ----
        if qty <= 0:
            mov_unit = None
        elif mq > 0:
            unit = mv / mq
            take = min(qty, mq)
            rem = qty - take
            cost = take * unit
            if rem > 0 and fallback_price is not None:
                cost += rem * fallback_price
                used_fb = True
            mov_unit = (cost / qty) if (rem == 0 or fallback_price is not None) else (cost / take)
            if rem > 0 and fallback_price is None:
                used_fb = True   # 扣穿且无兜底 → 标记(成本仅覆盖部分)
            mv -= take * unit
            mq -= take
            if mq <= 0:
                mq = Decimal(0)
                mv = Decimal(0)
        elif fallback_price is not None:
            mov_unit = fallback_price
            used_fb = True
        else:
            mov_unit = None

        # ---- FIFO ----
        need = qty
        cost = Decimal(0)
        covered = Decimal(0)
        while need > 0 and layers:
            layer = layers[0]
            t = min(need, layer[0])
            cost += t * layer[1]
            covered += t
            need -= t
            layer[0] -= t
            if layer[0] <= 0:
                layers.popleft()
        if need > 0 and fallback_price is not None:
            cost += need * fallback_price
            covered += need
            used_fb = True
        elif need > 0:
            used_fb = True
        if qty <= 0:
            fifo_unit = None
        elif covered > 0:
            fifo_unit = cost / covered
        elif fallback_price is not None:
            fifo_unit = fallback_price
        else:
            fifo_unit = None

        if mov_unit is None and fifo_unit is None:
            source = "none"
        elif used_fb:
            source = "fallback"
        else:
            source = "computed"

        out[ev.sale_id] = LineCost(
            moving_avg=mov_unit.quantize(_CENT) if mov_unit is not None else None,
            fifo=fifo_unit.quantize(_CENT) if fifo_unit is not None else None,
            source=source,
        )
    return out
