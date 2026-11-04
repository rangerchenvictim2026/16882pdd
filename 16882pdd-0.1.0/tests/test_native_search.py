"""原生筛选回归测试，所有卡片均为隔离构造，不访问1688。"""
import asyncio
import json
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from backend.filtering import evaluate, sorted_products
from backend.main import create_app
from backend.models import SearchFilters
from backend.native_search import REFERENCE_URL, build_search_url, has_reference_bundle, import_search_url, search_params
from backend.providers import BrowserProvider, SEARCH_SCRIPT, parse_search_card
from backend.store import Store
from backend.tasks import TaskManager
from .test_workbench import sample, wait_for


def native_filters(**changes):
    return SearchFilters.model_validate({**import_search_url(REFERENCE_URL), **changes})


def card(product_id="10000001", title="隔离构造的宠物用品卡片"):
    return {"id":product_id, "title":title, "text":"宠物用品\n已售999件", "price_text":"¥12", "image":"", "supplier_name":"隔离供应商"}


def test_reference_query_roundtrip_and_gbk_keyword():
    filters=native_filters()
    params=search_params(build_search_url(filters))
    assert params==search_params(REFERENCE_URL)
    assert filters.keyword=="宠物用品"
    assert has_reference_bundle(params)
    assert params["sortType"]=="va_sales360" and params["priceEnd"]=="15"
    assert "%B3%E8%CE%EF%D3%C3%C6%B7" in build_search_url(filters)


def test_template_updates_keyword_price_and_page_without_losing_tags():
    f=native_filters(keyword="收纳盒",max_price=12.5)
    params=search_params(build_search_url(f,3))
    assert params["keywords"]=="收纳盒" and params["priceEnd"]=="12.5" and params["beginPage"]=="3"
    assert has_reference_bundle(params)
    without_url=native_filters(native_search_url="")
    assert search_params(build_search_url(without_url))==search_params(REFERENCE_URL)


def test_current_form_overrides_legacy_url_instead_of_applying_web_conditions():
    filters=native_filters(shipping="all",keyword="新关键词",min_price=2,max_price=7)
    params=search_params(build_search_url(filters))
    assert "freeShipping" not in params
    assert params["keywords"]=="新关键词"
    assert params["priceStart"]=="2" and params["priceEnd"]=="7"


@pytest.mark.parametrize("url",["https://example.com/selloffer/offer_search.htm?keywords=x", "http://s.1688.com/selloffer/offer_search.htm?keywords=x", "https://s.1688.com:444/selloffer/offer_search.htm?keywords=x", "https://user@s.1688.com/selloffer/offer_search.htm?keywords=x", "https://s.1688.com/selloffer/offer_search.htm?keywords=x&keywords=y"])
def test_import_refuses_unsafe_or_ambiguous_urls(url):
    with pytest.raises(ValueError):
        import_search_url(url)


def test_utf8_keyword_import_is_supported():
    url="https://s.1688.com/selloffer/offer_search.htm?"+urlencode({"keywords":"宠物用品", "priceEnd":15})
    assert import_search_url(url)["keyword"]=="宠物用品"


def test_missing_details_do_not_discard_native_results_or_invent_periods():
    p=parse_search_card(card(),REFERENCE_URL,1)
    assert not evaluate(p,native_filters())
    assert p.published_at is None and p.native_new_days is None
    assert p.sales_30d is None and p.sales_unit is None and p.dropship is None and p.free_shipping is None
    assert p.platform_sales_text=="已售999件"
    assert evaluate(p,native_filters(selection_mode="strict",province="广东",city="广州"))


def test_query_url_alone_is_not_product_membership_evidence():
    p=parse_search_card(card(),REFERENCE_URL,1)
    p.evidence=[]
    assert evaluate(p,native_filters())
    p=parse_search_card(card(),REFERENCE_URL,1)
    proof=json.loads(p.evidence[-1].text)
    proof["product_id"]="99999999"
    p.evidence[-1].text=json.dumps(proof)
    assert evaluate(p,native_filters())


def test_another_query_or_incomplete_bundle_cannot_prove_filters():
    p=parse_search_card(card(),REFERENCE_URL,1)
    assert evaluate(p,native_filters(keyword="收纳盒"))
    url=REFERENCE_URL.replace("98306%2C", "")
    p=parse_search_card(card(),url,1)
    assert evaluate(p,native_filters())


def test_actual_price_range_is_rechecked_even_when_query_has_fifteen_yuan_limit():
    p=parse_search_card({**card(), "skus":[{"name":"小号", "price":12},{"name":"大号", "price":35}]},REFERENCE_URL,1)
    assert p.price_min==12 and p.price_max==35
    assert "商品实际价格超出筛选区间" in evaluate(p,native_filters())
    assert "规格价格范围未核实" in evaluate(p,native_filters(selection_mode="strict",province="广东",city="广州"))


def test_native_order_does_not_mix_in_local_sales_ranking():
    first=parse_search_card(card("10000001"),REFERENCE_URL,1).model_dump()
    second=parse_search_card(card("10000002"),REFERENCE_URL,2).model_dump()
    second.update(sales_30d=100000,sales_unit="件")
    assert [p["id"] for p in sorted_products([second,first],"platform")]==["10000001","10000002"]


class Locator:
    def __init__(self,page,selector): self.page,self.selector=page,selector
    @property
    def first(self): return self
    async def wait_for(self,**kwargs): pass
    async def inner_text(self): return "宠物用品搜索结果"
    async def count(self): return 1
    async def is_visible(self): return True
    async def get_attribute(self,name):
        return "true" if name=="aria-disabled" and self.page.index==len(self.page.pages)-1 else None
    async def evaluate_all(self,script): return "|".join(p["id"] for p in self.page.pages[self.page.index])
    async def click(self): self.page.index+=1
    async def scroll_into_view_if_needed(self,**kwargs):
        if hasattr(self.page,"scrolled"):
            self.page.scrolled.append(self.selector)


class IsolatedPage:
    def __init__(self,filters,pages):
        self.filters,self.pages=filters,pages
        self.index=0
        self.destinations=[]
    @property
    def url(self): return build_search_url(self.filters,self.index+1)
    def is_closed(self): return False
    async def goto(self,url,**kwargs): self.destinations.append(url);self.index=0
    def locator(self,selector): return Locator(self,selector)
    def get_by_text(self,text,**kwargs): return Locator(self,text)
    async def wait_for_function(self,*args,**kwargs): pass
    async def wait_for_timeout(self,*args): pass
    async def evaluate(self,script):
        assert script==SEARCH_SCRIPT
        return {"url":self.url,"cards":self.pages[self.index]}


class NativeProvider(BrowserProvider):
    def __init__(self,directory,filters,pages):
        super().__init__(directory)
        self.page=IsolatedPage(filters,pages)
    async def open(self): pass
    async def detail(self,product_id): raise AssertionError("原生模式不应逐件访问详情")


def test_search_reads_cards_keeps_pagination_order_and_does_not_open_details(tmp_path):
    async def scenario():
        filters=native_filters(limit=2)
        source=NativeProvider(tmp_path,filters,[[card("10000001")],[card("10000002")]])
        products=[p async for p in source.search(filters,set())]
        assert [p.native_position for p in products]==[1,2]
        assert [p.id for p in products]==["10000001","10000002"]
        assert all(not evaluate(p,filters) for p in products)
        assert source.page.destinations==[build_search_url(filters)]
    asyncio.run(scenario())


def test_native_task_save_export_and_import_endpoint(tmp_path):
    filters=native_filters(limit=2)
    source=NativeProvider(tmp_path,filters,[[card("10000001"),card("10000002")]])
    app=create_app(tmp_path,source)
    with TestClient(app,headers={"X-Workbench":"1"}) as client:
        imported=client.post("/api/search-link/import",json={"url":REFERENCE_URL})
        assert imported.status_code==200
        assert imported.json()["keyword"]=="宠物用品" and imported.json()["selection_mode"]=="platform"
        task=client.post("/api/tasks",json=filters.model_dump()).json()
        result=wait_for(client,task["id"])
        assert result["qualified"]==2 and result["excluded"]==0
        results=client.get(f"/api/tasks/{task['id']}/results").json()
        assert results[0]["sales_30d"] is None
        saved=client.post("/api/selections",json={"product_ids":[p["id"] for p in results]})
        assert saved.status_code==200 and len(client.get("/api/selections").json())==2
        exported=client.get("/api/selections/export").text
        assert "平台筛选链接" in exported and "已售999件" in exported and "va_sales360" in exported


def test_switching_legacy_task_mode_does_not_inherit_old_exclusions(tmp_path):
    store=Store(tmp_path)
    manager=TaskManager(store,None)
    original=manager.create(SearchFilters(keyword="宠物用品"))
    store.put_product(sample(dropship=None),original["id"],["无法确认支持一件代发"])
    manager.recount(original)
    retry=manager.create(native_filters(),retry_of=original["id"])
    assert retry["scanned"]==0 and retry["reasons"]=={}
    assert len(store.task_items(original["id"],False))==1
