"""回归实际卡片中的小数换行、裸销量及超价放行问题。"""
from pathlib import Path

import pytest

from backend.card_values import prices_from_text, sales_from_text
from backend.filtering import evaluate
from backend.models import SearchFilters
from backend.native_search import REFERENCE_URL, import_search_url
from backend.providers import parse_search_card
from backend.repairs import repair_existing_records
from backend.store import Store
from .test_native_search import NativeProvider, IsolatedPage, card, native_filters
import asyncio


@pytest.mark.parametrize("text,expected",[
    ("¥\n36\n.8",[36.8]),("¥\n18\n.68\n新人价",[18.68]),
    ("¥\n14\n.99",[14.99]),("￥1,299.00",[1299]),
    ("￥12～36.8",[12,36.8]),("￥\n12\n-\n36\n.8",[12,36.8]),
])
def test_split_price_decimals_are_not_truncated(text,expected):
    assert prices_from_text(text)==expected


@pytest.mark.parametrize("text,expected",[
    ("¥\n18\n.68\n新人价\n400+件\n包邮","400+件"),
    ("¥23.7\n10+件\n包邮","10+件"),
    ("近30天成交\n1.2万+\n件","近30天成交1.2万+件"),
    ("年销量 500+ 件","年销量500+件"),
    ("回头率 47%\n24H发\n2件起批",""),
    ("成交额500元\n满300元包邮",""),
])
def test_sales_accepts_visible_counts_without_inventing_a_period(text,expected):
    assert sales_from_text(text)==expected


def native_product(price_text,title="测试商品"):
    return parse_search_card({"id":"10000001","title":title,"text":f"{title}\n{price_text}\n400+件\n包邮", "price_text":price_text},REFERENCE_URL,1)


@pytest.mark.parametrize("price_text",["¥\n36\n.8","¥15.01","¥12～36.8",""])
def test_native_request_does_not_bypass_actual_price_validation(price_text):
    filters=SearchFilters.model_validate(import_search_url(REFERENCE_URL))
    assert evaluate(native_product(price_text),filters)


def test_price_boundary_and_title_are_taken_from_the_card():
    filters=SearchFilters.model_validate(import_search_url(REFERENCE_URL))
    p=native_product("¥\n15\n.00","商品名称\n¥\n15\n.00\n400+件")
    assert p.title=="商品名称" and p.price_max==15 and not evaluate(p,filters)
    assert p.platform_sales_text=="400+件" and p.sales_30d is None


def test_repair_keeps_original_observation_and_excludes_existing_overpriced_result(tmp_path):
    store=Store(tmp_path)
    filters=SearchFilters.model_validate(import_search_url(REFERENCE_URL))
    task={"id":"test-task", "filters":filters.model_dump(),"status":"completed","created_at":"2026-11-02T10:00:00+08:00", "updated_at":"2026-11-02T10:00:00+08:00", "scanned":1,"qualified":1,"excluded":0,"reasons":{}}
    store.put_task(task)
    original=native_product("¥\n36\n.8")
    original.price_min=original.price_max=36
    original.platform_sales_text=""
    store.put_product(original,task["id"],[])
    repair_existing_records(store)
    fixed=store.task_items(task["id"],False)[0]
    assert fixed["product"]["price_min"]==36.8
    assert fixed["product"]["platform_sales_text"]=="400+件"
    assert fixed["product"]["observed_at"]==original.observed_at
    assert not fixed["qualified"] and store.task(task["id"])["qualified"]==0
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM observations").fetchone()[0]==2
    assert list((tmp_path/'backups').glob('*.json'))
    repair_existing_records(store)
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM observations").fetchone()[0]==2


def test_delayed_sales_are_read_after_scrolling_without_opening_details(tmp_path):
    class DelayedPage(IsolatedPage):
        def __init__(self,filters,pages):
            super().__init__(filters,pages)
            self.scrolled=[]
        async def evaluate(self,script):
            response=await super().evaluate(script)
            response["cards"]=[{**c,"sales_text":"400+件" if any(c["id"] in selector for selector in self.scrolled) else ""} for c in response["cards"]]
            return response
    async def scenario():
        filters=native_filters(limit=1)
        source=NativeProvider(tmp_path,filters,[])
        source.page=DelayedPage(filters,[[{**card(),"text":"宠物用品\n¥12"}]])
        results=[p async for p in source.search(filters,set())]
        assert results[0].platform_sales_text=="400+件"
        assert source.page.scrolled and results[0].sales_30d is None
    asyncio.run(scenario())
