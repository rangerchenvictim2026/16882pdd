"""拼多多竞品检索的输入、快照与结果约定。"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .models import Product


class CompetitionRequest(BaseModel):
    source_task_id: str = Field(min_length=1, max_length=80)
    keywords: dict[str, str] = Field(default_factory=dict, max_length=5)

    @model_validator(mode="after")
    def check_keywords(self):
        if any(not value.strip() or len(value) > 80 for value in self.keywords.values()):
            raise ValueError("搜索关键词不能为空，且不能超过80个字")
        return self


class CompetitionResume(BaseModel):
    allow_title_fallback: bool | None = None


class SessionValidation(BaseModel):
    keyword: str | None = Field(default=None,min_length=1,max_length=80)


class Competitor(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    id: str = Field(default="", max_length=40)
    title: str = Field(min_length=1, max_length=1000)
    image: str = ""
    url: str = ""
    price_min: float | None = Field(default=None, ge=0)
    price_max: float | None = Field(default=None, ge=0)
    price_text: str = ""
    sales_text: str = ""
    sales_value: float | None = Field(default=None, ge=0)
    sales_unit: str = ""
    sales_scope: Literal["product", "store", "unknown"] = "unknown"
    sales_approximate: bool = False
    observed_at: str
    page_position: int = Field(default=0, ge=0)
    locator: str = ""
    raw_text: str = Field(default="", max_length=5000)


class SearchBatch(BaseModel):
    candidates: list[Competitor] = Field(default_factory=list)
    search_url: str = ""
    empty_verified: bool = False
    native_sales_sort: bool = False
    scanned_count: int | None = Field(default=None, ge=0)
    excluded: dict[str, int] = Field(default_factory=dict)


class CollectionCheckpoint(BaseModel):
    keyword: str = Field(max_length=80)
    collected_at: str
    search_url: str
    native_sales_sort: bool = False
    rows: list[Competitor] = Field(default_factory=list, max_length=20)
    resolved_positions: list[int] = Field(default_factory=list, max_length=20)


class CompetitionItem(BaseModel):
    source: Product
    keyword: str = Field(default="", max_length=80)
    status: Literal["pending", "running", "completed", "no_results", "insufficient_data", "failed", "cancelled"] = "pending"
    method: Literal["image", "title"] | None = None
    image_status: Literal["pending", "found", "empty", "unsupported", "missing"] = "pending"
    fallback_reason: str = ""
    message: str = "等待检索"
    search_url: str = ""
    candidates_count: int = 0
    sort_scope: str = "本次读取的单品销量展示值降序；不同单位分别处理"
    excluded: dict[str, int] = Field(default_factory=dict)
    matches: list[Competitor] = Field(default_factory=list, max_length=5)
    checkpoint: CollectionCheckpoint | None = None


class CompetitionJob(BaseModel):
    id: str
    source_task_id: str
    status: Literal["queued", "running", "paused", "completed", "failed", "cancelled", "interrupted"]
    allow_title_fallback: bool = True
    message: str
    created_at: str
    updated_at: str
    current_index: int = Field(default=0, ge=0, le=5)
    action: Literal["login", "verification", "title_fallback"] | None = None
    items: list[CompetitionItem] = Field(min_length=1, max_length=5)
    events: list[dict[str, str]] = Field(default_factory=list, max_length=200)
    @model_validator(mode="after")
    def unique_sources(self):
        if len({i.source.id for i in self.items})!=len(self.items):
            raise ValueError("竞品任务不能包含重复货源")
        return self
