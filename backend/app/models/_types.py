"""共享列类型别名，统一精度与时区（§0/§5）。"""
from sqlalchemy import DateTime, Numeric

Money = Numeric(14, 2)    # 金额
Qty = Numeric(14, 3)      # 数量
Rate = Numeric(5, 4)      # 税率
Margin = Numeric(10, 4)   # 毛利率
TZDateTime = DateTime(timezone=True)  # TIMESTAMPTZ，统一存 UTC
