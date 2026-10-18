"""分析类接口的整数参数上界。

FastAPI 只校验 Python int，越过 PostgreSQL integer（id 列）或 bigint（OFFSET =
(page-1)*page_size）范围的值会一路打到数据库变成 500。统一在入口收紧，越界即 422。
"""
from typing import Annotated

from pydantic import Field

# PostgreSQL integer 上界：主键/外键 id 列均为 integer
PG_INT_MAX = 2**31 - 1
# 分页页码上界：page_size ≤ 200 时 OFFSET ≤ 约 200 万行，远低于 bigint，也远超实际数据量
ANALYTICS_PAGE_MAX = 10_000
ANALYTICS_PAGE_SIZE_MAX = 200

DbId = Annotated[int, Field(ge=1, le=PG_INT_MAX)]
