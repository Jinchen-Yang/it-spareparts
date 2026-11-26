"""机型备件池的命令边界与预览基线。"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


def valid_id(value: str) -> str:
    if int(value) > 2**31 - 1:
        raise ValueError("编号超出范围")
    return value


# ECMAScript String.trim() 的 WhiteSpace 和 LineTerminator。U+FEFF 也会被员工复制进料号。
PN_EDGE_SPACE = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def trim_raw_pn(value):
    return value.strip(PN_EDGE_SPACE) if isinstance(value, str) else value


Id = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,9}$"), AfterValidator(valid_id)]
RawPn = Annotated[str, StringConstraints(max_length=256), BeforeValidator(trim_raw_pn)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=192)]
Source = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2048)]
Position = Annotated[str, StringConstraints(strip_whitespace=True, max_length=512)]


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*")
    @classmethod
    def valid_postgres_text(cls, value):
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str):
                if "\x00" in item:
                    raise ValueError("资料中不能包含空字符")
                try:
                    item.encode("utf-8")
                except UnicodeEncodeError:
                    raise ValueError("文本包含无效的字符编码") from None
        return value


class Candidates(Payload):
    pns: list[RawPn] | None = Field(default=None, min_length=1, max_length=1000)
    q: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)] | None = None

    @model_validator(mode="after")
    def one_search_mode(self):
        if (self.pns is None) == (self.q is None):
            raise ValueError("请选择精确核对清单或输入搜索词")
        return self


class Command(Payload):
    requestId: UUID
    expectedRevision: int = Field(strict=True, ge=0, le=2**31 - 1)


class Create(Command):
    action: Literal["create"]
    brand: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    name: Name
    aliases: list[Name] = Field(default_factory=list, max_length=60)


class ImportRow(Payload):
    line: int = Field(strict=True, gt=0, le=2**31 - 1)
    rawPn: RawPn
    position: Position = ""
    condition: Position = ""
    source: Source = ""
    reviewDecision: Annotated[str, StringConstraints(max_length=192)] | None = None
    ignore: bool = False
    partId: Id | None = None
    candidateIds: list[Id] = Field(max_length=1000)


class Import(Command):
    action: Literal["import"]
    modelId: Id
    rows: list[ImportRow] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def distinct_lines(self):
        if len({row.line for row in self.rows}) != len(self.rows):
            raise ValueError("清单原行号重复，请重新预览")
        return self


class Resolve(Command):
    action: Literal["resolve"]
    pendingId: Id
    partId: Id
    source: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]


class Remove(Command):
    action: Literal["remove"]
    modelId: Id
    partId: Id
    expectedMembershipVersion: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


CommandBody = Annotated[Create | Import | Resolve | Remove, Field(discriminator="action")]
