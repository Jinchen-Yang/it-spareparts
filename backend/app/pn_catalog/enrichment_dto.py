"""Explicit human enrichment requests and bounded provider proposals."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator
from .dto import StrictDTO
from .media_dto import MediaUUID


class RecognizeInbox(StrictDTO):
    expected_revision: int = Field(ge=1)


class ResearchPN(StrictDTO):
    expected_identity_revision: int = Field(ge=0)
    expected_profile_revision: int = Field(ge=0)
    expected_content_revisions: dict[Literal["good", "bad"], int] = Field(default_factory=dict, max_length=2)
    capabilities: list[Literal["text", "images"]] = Field(min_length=1, max_length=2)
    expected_gallery_revision: int | None = Field(default=None, ge=0)
    max_searches: int = Field(default=2, ge=1, le=2)

    @field_validator("capabilities")
    @classmethod
    def unique_capabilities(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("capabilities must be unique")
        return sorted(value)

    @field_validator("expected_content_revisions")
    @classmethod
    def revisions(cls, value):
        if any(type(v) is not int or v < 0 for v in value.values()):
            raise ValueError("both content revisions must be nonnegative integers")
        return value


    @model_validator(mode="after")
    def required_baselines(self):
        if "text" in self.capabilities and set(self.expected_content_revisions) != {"good", "bad"}:
            raise ValueError("text research requires both content revisions")
        if "images" in self.capabilities and self.expected_gallery_revision is None:
            raise ValueError("image research requires a gallery baseline")
        return self


class CreateBundle(StrictDTO):
    expected_identity_revision: int = Field(ge=0)
    expected_gallery_revision: int = Field(ge=0)
    grade: Literal["good", "bad"]
    entry_ids: list[MediaUUID] | None = Field(default=None, min_length=1, max_length=1000)
    watermark: bool = False

    @field_validator("entry_ids")
    @classmethod
    def unique_entries(cls, value):
        if value is not None and len(value) != len(set(value)):
            raise ValueError("entries must be unique")
        return value


class RecognitionHint(StrictDTO):
    pn: str = Field(min_length=1, max_length=256)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)


class RecognitionResult(StrictDTO):
    candidates: list[RecognitionHint] = Field(max_length=10)
    item_category: str | None = Field(default=None, max_length=64)
    category_confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=200)
    tokens: int | None = Field(default=None, ge=0, le=1_000_000_000)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=500)
    url: HttpUrl
    snippet: str = Field(default="", max_length=10000)
    image_urls: list[HttpUrl] = Field(default_factory=list, max_length=20)

class RejectedEvidence(BaseModel):
    title: str = Field(max_length=500)
    url: HttpUrl
    reason: Literal[
        "homepage_or_listing",
        "pn_not_matched",
        "short_pn_context_missing",
        "compatibility_reference",
    ]

class SearchResult(BaseModel):
    evidence: list[Evidence] = Field(default_factory=list, max_length=30)
    rejected_evidence: list[RejectedEvidence] = Field(default_factory=list, max_length=30)
    provider: str = Field(default="", max_length=64)
    model: str = Field(default="", max_length=200)
    tokens: int = Field(default=0, ge=0)

FieldName = Literal[
    "brand",
    "manufacturer",
    "category_major",
    "category_minor",
    "summary",
    "specs",
    "good_title",
    "good_description",
    "bad_title",
    "bad_description",
]

class FieldProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: FieldName
    value: str | dict[str, str | int | float | bool]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    source_urls: list[HttpUrl] = Field(default_factory=list, max_length=10)

    @field_validator("value")
    @classmethod
    def bound_value(cls, value):
        import json

        if len(json.dumps(value, ensure_ascii=False, allow_nan=False)) > 20000:
            raise ValueError("补全内容过长")
        if isinstance(value, dict) and len(value) > 100:
            raise ValueError("参数项过多")
        return value

class ImageProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: HttpUrl
    source_url: HttpUrl
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    has_foreign_watermark: bool = False

class ImageValidationIssue(BaseModel):
    """适配器生成的诊断，只保留索引和校验类型，不记录原始值或网址。"""

    model_config = ConfigDict(extra="forbid")
    index: int | None = Field(default=None, ge=0, lt=20)
    reason: Literal["invalid_collection", "invalid_candidate"]
    error_types: list[str] = Field(default_factory=list, max_length=8)

class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: list[FieldProposal] = Field(default_factory=list, max_length=10)
    images: list[ImageProposal] = Field(default_factory=list, max_length=20)
    image_validation_errors: list[ImageValidationIssue] = Field(default_factory=list, max_length=20)
    provider: str = Field(default="", max_length=64)
    model: str = Field(default="", max_length=200)
    tokens: int = Field(default=0, ge=0)

    @field_validator("fields")
    @classmethod
    def unique_and_typed(cls, fields):
        if len({item.field for item in fields}) != len(fields):
            raise ValueError("同一字段不能重复补全")
        for item in fields:
            if (item.field == "specs") != isinstance(item.value, dict):
                raise ValueError("参数必须是对象，其他补全字段必须是文字")
            limit = (
                100
                if item.field in {"brand", "manufacturer", "category_major", "category_minor"}
                else 200
            )
            if (item.field.endswith("title") or limit == 100) and len(item.value) > limit:
                raise ValueError("补全字段超过商品字段长度")
        return fields
