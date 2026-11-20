"""维修任务单写接口。所有负责人、来源事实与历史均由服务端解析。"""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

RepairRole = Literal["supervisor", "sales", "technician", "admin"]
RepairMode = Literal["internal", "external"]
RepairResult = Literal["fixed", "unfixed"]
RepairDestination = Literal["company", "shop", "retry"]
PositiveInt = Annotated[int, Field(strict=True, ge=1, le=2147483647)]
NonnegativeInt = Annotated[int, Field(strict=True, ge=0, le=2147483647)]
Username = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
OrderId = Annotated[str, StringConstraints(min_length=1, max_length=36)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]


class RepairInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateLine(RepairInput):
    pn: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=256)] = ""
    qty: PositiveInt
    sourceReceiptId: OrderId | None = None
    sourceVersion: PositiveInt | None = None


class CreateRepairOrder(RepairInput):
    mode: RepairMode
    assignee: Username | None = None
    note: Note = ""
    lines: Annotated[list[CreateLine], Field(min_length=1, max_length=100)]


class DispositionCorrection(RepairInput):
    kind: RepairDestination
    result: RepairResult
    qty: NonnegativeInt
    retryOrderId: OrderId | None = None


class EditLine(CreateLine):
    fixed: NonnegativeInt
    unfixed: NonnegativeInt
    dispositions: list[DispositionCorrection] | None = None


class Action(RepairInput):
    version: PositiveInt


class AssignAction(Action):
    type: Literal["assign"]
    assignee: Username


class ClaimAction(Action):
    type: Literal["claim"]


class ReturnAction(Action):
    type: Literal["return"]


class ResultAction(Action):
    type: Literal["result"]
    line: NonnegativeInt
    fixed: NonnegativeInt
    unfixed: NonnegativeInt


class EditAction(Action):
    type: Literal["edit"]
    assignee: Username | None = None
    creator: Username | None = None
    mode: RepairMode
    note: Note = ""
    lines: Annotated[list[EditLine], Field(min_length=1, max_length=100)]
    sourceIndices: Annotated[list[NonnegativeInt | None], Field(max_length=100)] | None = None


class DisposeAction(Action):
    type: Literal["dispose"]
    line: NonnegativeInt
    result: RepairResult
    kind: RepairDestination
    qty: PositiveInt


RepairOrderAction = Annotated[
    AssignAction | ClaimAction | ReturnAction | ResultAction | EditAction | DisposeAction,
    Field(discriminator="type"),
]
