"""隔离测试数据仅用于验证规则，不写入正式选品库或模拟真实接入。"""
import asyncio
import copy
import json
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from backend.filtering import evaluate, sorted_products
from backend.main import create_app
from backend.models import Backup, Evidence, Product, SearchFilters, SelectionPatch, now
from backend.providers import HumanActionRequired, parse_detail
from backend.store import Store
from backend.tasks import TaskManager


def sample(product_id="10000001", at=None, **changes):
    at = at or now()
    url = f"https://detail.1688.com/offer/{product_id}.html"
    stamp = at.isoformat()
    product = Product(id=product_id, title="隔离测试商品", url=url, observed_at=stamp, price_min=5, price_max=9,
                      published_at=(at-timedelta(days=3)).isoformat(), dropship=True, sales_30d=120, sales_unit="件", sales_exact=True,
                      free_shipping=True, shipping_province="浙江", shipping_city="杭州",
                      evidence=[Evidence(field=f, text=t, url=url, observed_at=stamp) for f,t in [("published_at", "上新时间已明确"), ("dropship", "一件代发"), ("sales_30d", "近30天销量120件"), ("free_shipping", "送至浙江杭州，包邮"), ("price", "5到9元")]])
    return product.model_copy(update=changes)


def test_fifteen_day_boundary_and_future_dates():
    at = now()
    filters = SearchFilters(keyword="收纳盒")
    assert evaluate(sample(at=at, published_at=(at-timedelta(days=15)).isoformat()), filters, at) == []
    assert evaluate(sample(at=at, published_at=(at-timedelta(days=15, microseconds=1)).isoformat()), filters, at)
    assert evaluate(sample(at=at, published_at=(at+timedelta(seconds=1)).isoformat()), filters, at)


def test_unknown_fields_are_excluded_and_shipping_is_optional():
    filters = SearchFilters(keyword="收纳盒")
    assert evaluate(sample(free_shipping=None, shipping_province="", shipping_city=""), filters) == []
    assert evaluate(sample(dropship=None), filters) == ["无法确认支持一件代发"]
    assert evaluate(sample(sales_30d=None), filters) == ["缺少明确的近30天销量"]
    assert evaluate(sample(published_at=None), filters) == ["无法确认近15天上新"]
    assert evaluate(sample(evidence=[]), filters)


def test_evidence_cannot_come_from_another_product_or_snapshot():
    p = sample()
    for e in p.evidence:
        e.url = "https://detail.1688.com/offer/99999999.html"
    assert len(evaluate(p, SearchFilters(keyword="测试"))) == 3


def test_native_new_filter_expires_instead_of_becoming_publish_date():
    at = now()
    p = sample(at=at, published_at=None, native_new_days=15)
    p.evidence.append(Evidence(field="native_new_days", text="近15天上新", url=p.url, observed_at=p.observed_at))
    assert not evaluate(p, SearchFilters(keyword="测试"), at)
    assert evaluate(p, SearchFilters(keyword="测试"), at+timedelta(hours=2))


def test_shipping_matches_destination_and_full_price_range():
    filters = SearchFilters(keyword="收纳盒", shipping="free", province="浙江", city="杭州", max_price=7)
    assert evaluate(sample(), filters) == ["不符合价格区间"]
    assert "包邮适用地区未核实" in evaluate(sample(shipping_city="宁波"), filters)
    assert "无法确认包邮条件" in evaluate(sample(free_shipping=None), filters)
    assert "不符合包邮筛选" in evaluate(sample(free_shipping=False), filters)


def test_sort_keeps_sales_units_separate():
    data = [sample("10000001", sales_30d=12, sales_unit="件").model_dump(), sample("10000002", sales_30d=10000, sales_unit="笔").model_dump(), sample("10000003", sales_30d=55, sales_unit="件").model_dump()]
    assert [p["id"] for p in sorted_products(data)] == ["10000003", "10000001", "10000002"]


def test_parser_rejects_update_time_moq_cumulative_and_buyer_counts():
    p = parse_detail({"title":"测试", "text":"更新时间：2026-11-01\n1件起批\n累计销量900件\n近30天成交买家100人\n近30天成交额900元"}, "10000001")
    assert p.published_at is None and p.dropship is None and p.sales_30d is None
    p = parse_detail({"title":"测试", "text":"不支持一件代发\n近30天销量：120件\n上新时间：2026-11-01"}, "10000001")
    assert p.dropship is False and p.sales_30d == 120 and p.published_at is not None


def test_parser_preserves_sku_prices_and_conditional_shipping():
    p = parse_detail({"title":"测试", "text":"一件代发\n近30天销量 1.2万+件", "price_text":"¥1.00", "skus":[{"name":"大号", "price":12},{"name":"小号", "price":6}],"shipping_text":"配送至：浙江杭州\n满99元包邮"}, "10000001")
    assert p.price_min == 6 and p.price_max == 12
    assert p.sales_30d == 12000 and not p.sales_exact
    assert p.free_shipping is None


def test_sales_abbreviation_is_not_mislabeled_as_a_lower_bound():
    approximate = parse_detail({"title":"测试", "text":"近30天销量1.2万件"}, "10000001")
    lower_bound = parse_detail({"title":"测试", "text":"近30天销量1.2万+件"}, "10000001")
    assert not approximate.sales_exact and not approximate.sales_is_lower_bound
    assert not lower_bound.sales_exact and lower_bound.sales_is_lower_bound


class FakeProvider:
    """测试专用，不可从正式接口或界面选择。"""
    def __init__(self, products=None, pause=False, delay=0):
        self.products=products or [sample()]
        self.pause=pause
        self.delay=delay
        self.calls=0

    def capabilities(self):
        return {"verification":"unverified", "state":"closed", "message":"隔离测试", "fields":[], "note":"隔离测试"}

    async def search(self, filters, seen):
        self.calls += 1
        if self.pause and self.calls == 1:
            raise HumanActionRequired("请手动完成验证")
        for product in self.products:
            if product.id not in seen:
                await asyncio.sleep(self.delay)
                seen.add(product.id)
                yield product

    async def detail(self, product_id):
        return self.products[0]

    async def close(self):
        pass

    async def open(self):
        pass


def wait_for(client, task_id, statuses=("completed", "failed"), timeout=6):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["status"] in statuses:
            return task
        time.sleep(.02)
    raise AssertionError(f"任务没有进入预期状态：{task}")


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, FakeProvider())
    with TestClient(app, headers={"X-Workbench":"1"}) as client:
        yield client


def create_completed(client):
    response = client.post("/api/tasks", json={"keyword":"收纳盒"})
    assert response.status_code == 201, response.text
    task = response.json()
    assert wait_for(client, task["id"])["status"] == "completed"
    return task["id"]


def test_full_api_lifecycle_and_refresh_preserves_notes(client):
    task_id=create_completed(client)
    results=client.get(f"/api/tasks/{task_id}/results").json()
    assert len(results)==1
    for _ in range(2):
        assert client.post("/api/selections",json={"product_ids":[results[0]["id"]]}).status_code==200
    assert len(client.get("/api/selections").json())==1
    patch={"status":"已选中", "notes":"=测试公式", "tags":["打样"]}
    assert client.patch(f"/api/selections/{results[0]['id']}",json=patch).status_code==200
    refresh=client.post(f"/api/products/{results[0]['id']}/refresh",json={}).json()
    wait_for(client,refresh["id"])
    record=client.get("/api/selections").json()[0]
    assert record["notes"]==patch["notes"] and record["status"]=="已选中" and record["tags"]==["打样"]
    csv=client.get("/api/selections/export").text
    assert "商品名称" in csv and "'=测试公式" in csv
    assert client.get("/api/capabilities").json()["verification"]=="unverified"


def test_pause_resume_cancel_and_partial_result_retention(tmp_path):
    provider=FakeProvider([sample("10000001"),sample("10000002")],pause=True)
    app=create_app(tmp_path,provider)
    with TestClient(app,headers={"X-Workbench":"1"}) as client:
        t=client.post("/api/tasks",json={"keyword":"测试"}).json()
        assert wait_for(client,t["id"],("paused",))["status"]=="paused"
        assert client.post(f"/api/tasks/{t['id']}/resume",json={}).status_code==200
        deadline=time.monotonic()+4
        while time.monotonic()<deadline and client.get(f"/api/tasks/{t['id']}").json()["scanned"]<1:
            time.sleep(.02)
        client.post(f"/api/tasks/{t['id']}/cancel",json={})
        assert client.get(f"/api/tasks/{t['id']}").json()["status"]=="cancelled"
        assert len(client.get(f"/api/tasks/{t['id']}/results").json())>=1
        retry=client.post(f"/api/tasks/{t['id']}/retry",json={}).json()
        assert wait_for(client,retry["id"])["qualified"]==2
        assert len(client.get(f"/api/tasks/{retry['id']}/results").json())==2


def test_validation_local_write_guard_and_presets(client):
    assert client.post("/api/tasks",json={"keyword":""}).status_code==422
    assert client.post("/api/tasks",json={"keyword":"测试", "min_price":100,"max_price":1}).status_code==422
    assert client.post("/api/tasks",json={"keyword":"测试", "shipping":"free"}).status_code==422
    assert client.post("/api/tasks",json={"keyword":"测试"},headers={"X-Workbench":""}).status_code==403
    assert client.post("/api/tasks",json={"keyword":"测试"},headers={"Origin":"https://example.com"}).status_code==403
    assert client.get("/api/missing").json()["detail"]=="接口不存在"
    preset=client.post("/api/presets",json={"name":"收纳", "filters":{"keyword":"收纳盒"}}).json()
    assert client.get("/api/presets").json()[0]["name"]=="收纳"
    client.delete(f"/api/presets/{preset['id']}")
    assert client.get("/api/presets").json()==[]


def test_backup_restore_roundtrip_and_rollback(client):
    create_completed(client)
    client.post("/api/selections",json={"product_ids":["10000001"]})
    backup=client.get("/api/backup").json()
    assert "chrome-profile" not in json.dumps(backup)
    damaged=copy.deepcopy(backup)
    damaged["tables"]["selections"][0]["product_id"]="99999999"
    assert client.post("/api/restore",json=damaged).status_code==400
    assert len(client.get("/api/selections").json())==1
    assert client.post("/api/restore",json=backup).status_code==200
    assert len(client.get("/api/selections").json())==1
    assert client.get("/api/tasks").json()[0]["status"]=="cancelled"
    assert client.post("/api/restore",json={"bad":True}).status_code==400


def test_newer_snapshot_cannot_be_overwritten_by_retry(tmp_path):
    store=Store(tmp_path)
    old=sample()
    new=sample(at=now()+timedelta(seconds=1), title="较新的资料")
    store.put_product(old)
    store.put_product(new)
    store.put_product(old)
    assert store.product(old.id)["title"]=="较新的资料"


def test_refresh_retains_original_shipping_and_price_filters(client):
    response=client.post("/api/tasks",json={"keyword":"收纳", "shipping":"free", "province":"浙江", "city":"杭州", "max_price":10})
    task=response.json()
    wait_for(client,task["id"])
    client.post("/api/settings",json={})
    refresh=client.post("/api/products/10000001/refresh",json={}).json()
    assert refresh["filters"]["shipping"]=="free"
    assert refresh["filters"]["city"]=="杭州"
    assert refresh["filters"]["max_price"]==10
    wait_for(client,refresh["id"])


def test_latest_rejected_snapshot_cannot_use_historical_qualification(client):
    create_completed(client)
    store=client.app.state.store
    store.put_product(sample(at=now(),dropship=None))
    response=client.post("/api/selections",json={"product_ids":["10000001"]})
    assert response.status_code==400
    assert client.get("/api/selections").json()==[]


def test_rejected_products_cannot_be_saved(tmp_path):
    app=create_app(tmp_path,FakeProvider([sample(dropship=None)]))
    with TestClient(app,headers={"X-Workbench":"1"}) as client:
        task_id=create_completed(client)
        assert client.get(f"/api/tasks/{task_id}/results").json()==[]
        assert client.post("/api/selections",json={"product_ids":["10000001"]}).status_code==400


def test_retry_rechecks_previously_rejected_products_without_duplicates(tmp_path):
    provider=FakeProvider([sample(dropship=None)])
    app=create_app(tmp_path,provider)
    with TestClient(app,headers={"X-Workbench":"1"}) as client:
        task_id=create_completed(client)
        assert client.get(f"/api/tasks/{task_id}/results").json()==[]
        provider.products=[sample()]
        retried=client.post(f"/api/tasks/{task_id}/retry",json={}).json()
        result=wait_for(client,retried["id"])
        assert result["qualified"]==1 and result["scanned"]==1
        assert len(client.get(f"/api/tasks/{retried['id']}/results").json())==1


def test_restart_retains_completed_results(tmp_path):
    async def scenario():
        store=Store(tmp_path)
        manager=TaskManager(store,FakeProvider())
        task=manager.create(SearchFilters(keyword="测试"))
        store.put_product(sample(),task["id"],[])
        restarted=TaskManager(store,FakeProvider())
        await restarted.start()
        assert store.task(task["id"])["status"]=="interrupted"
        assert len(restarted.results(task["id"]))==1
        await restarted.close()
    asyncio.run(scenario())
