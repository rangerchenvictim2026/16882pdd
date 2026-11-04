"""数据约定：原始商品资料与运营记录分开保存。"""
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

CHINA = ZoneInfo("Asia/Shanghai")


def now() -> datetime:
    return datetime.now(CHINA)


def timestamp() -> str:
    return now().isoformat()


class SearchFilters(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    keyword: str = Field(min_length=1, max_length=100)
    new_days: Literal[15] = 15
    dropship: Literal[True] = True
    sales_days: Literal[30] = 30
    shipping: Literal["all", "free", "paid"] = "all"
    min_price: float | None = Field(default=None, ge=0, le=10000000)
    max_price: float | None = Field(default=None, ge=0, le=10000000)
    province: str = Field(default="", max_length=30)
    city: str = Field(default="", max_length=30)
    limit: int = Field(default=100, ge=1, le=500)
    selection_mode: Literal["strict", "platform"] = "strict"
    native_search_url: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def check_filters(self):
        self.keyword = self.keyword.strip()
        if not self.keyword:
            raise ValueError("请输入搜索关键词")
        if self.min_price is not None and self.max_price is not None and self.min_price > self.max_price:
            raise ValueError("最低价格不能高于最高价格")
        if self.selection_mode == "strict" and self.shipping != "all" and (not self.province.strip() or not self.city.strip()):
            raise ValueError("筛选包邮条件时，请填写收货省份和城市")
        if self.native_search_url:
            from .native_search import search_params, build_search_url
            search_params(self.native_search_url)
            build_search_url(self)
        return self


class Evidence(BaseModel):
    field: str
    text: str = Field(max_length=4000)
    url: str
    observed_at: str


class SKU(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    name: str
    price: float = Field(ge=0)


class Product(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    id: str = Field(pattern=r"^\d{5,30}$")
    title: str = Field(min_length=1, max_length=1000)
    url: str
    image: str = ""
    supplier_id: str = ""
    supplier_name: str = ""
    supplier_url: str = ""
    price_min: float | None = Field(default=None, ge=0)
    price_max: float | None = Field(default=None, ge=0)
    skus: list[SKU] = Field(default_factory=list)
    published_at: str | None = None
    native_new_days: int | None = None
    dropship: bool | None = None
    sales_30d: int | None = Field(default=None, ge=0)
    sales_unit: Literal["件", "笔"] | None = None
    sales_exact: bool = False
    sales_is_lower_bound: bool = False
    free_shipping: bool | None = None
    shipping_province: str = ""
    shipping_city: str = ""
    shipping_condition: str = ""
    observed_at: str
    source: str = "1688网页"
    native_search_url: str = ""
    native_position: int | None = Field(default=None, ge=1)
    platform_sales_text: str = ""
    evidence: list[Evidence] = Field(default_factory=list)


class SelectionPatch(BaseModel):
    status: Literal["待评估", "已选中", "已放弃"] = "待评估"
    notes: str = Field(default="", max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=20)


class SaveSelections(BaseModel):
    product_ids: list[str] = Field(min_length=1, max_length=500)


class PresetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    filters: SearchFilters


class SearchLinkRequest(BaseModel):
    url: str = Field(min_length=1, max_length=4000)


class RetryRequest(BaseModel):
    selection_mode: Literal["strict", "platform"] | None = None


class Settings(BaseModel):
    province: str = Field(default="", max_length=30)
    city: str = Field(default="", max_length=30)
    default_limit: int = Field(default=100, ge=1, le=500)


class Backup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["commerce-workbench"]
    version: Literal[1]
    exported_at: str
    tables: dict[str, list[dict]]
