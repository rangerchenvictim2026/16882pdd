"""验证关键词竞品任务的真实状态流转、快照、导出和备份。"""
import asyncio
import csv
import io
import time

import pytest
from fastapi.testclient import TestClient

from backend.competition_models import CompetitionRequest, SearchBatch
from backend.competition_tasks import CompetitionManager, search_keyword
from backend.main import create_app
from backend.models import Backup, SearchFilters
from backend.pdd_provider import keyword_url
from backend.providers import HumanActionRequired, SourceUnavailable
from backend.store import Store
from backend.tasks import TaskManager
from .test_competition_ranking import competitor
from .test_workbench import FakeProvider, sample


class KeywordProvider:
    def __init__(self, pause=False, fail=False):
        self.pause, self.fail = pause, fail
        self.calls = []
        self.opened = False

    async def open(self):
        self.opened = True

    async def close(self):
        pass

    async def search_by_title(self, keyword):
        self.calls.append(keyword)
        if self.pause and len(self.calls) == 1:
            raise HumanActionRequired("请先在专用浏览器登录拼多多")
        if self.fail:
            raise SourceUnavailable("页面结构未识别")
        rows = [competitor(i, f"已拼{i * 100}件") for i in range(1, 9)]
        return SearchBatch(candidates=rows, search_url=keyword_url(keyword))


def seed(store, count=7):
    task = {"id":"source-task", "status":"completed", "filters":SearchFilters(keyword="手机壳").model_dump(),
            "created_at":"2026-11-02T12:00:00+08:00", "updated_at":"2026-11-02T12:00:00+08:00"}
    store.put_task(task)
    for index in range(1, count + 1):
        store.put_product(sample(str(10000000 + index), title=f"一件代发 手机壳{index}", sales_30d=index), task["id"], [])
    return task


def wait_job(client, job_id, statuses, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = client.get('/api/competitions/' + job_id).json()
        if job["status"] in statuses:
            return job
        time.sleep(.02)
    raise AssertionError(f"竞品任务没有进入预期状态：{job}")


def test_first_five_use_the_same_source_ranking_and_keep_personal_records(tmp_path):
    app = create_app(tmp_path, FakeProvider(), KeywordProvider())
    seed(app.state.store)
    with TestClient(app, headers={"X-Workbench":"1"}) as client:
        response = client.post('/api/competitions', json={"source_task_id":"source-task"})
        assert response.status_code == 201
        job = wait_job(client, response.json()["id"], {"completed"})
        assert [i["source"]["id"] for i in job["items"]] == [str(10000000+i) for i in [7,6,5,4,3]]
        assert all([r["sales_value"] for r in i["matches"]] == [800,700,600,500,400] for i in job["items"])
        assert len(app.state.store.task_items('source-task')) == 7
        assert not app.state.store.selections()
        assert client.get('/api/competitions?source_task_id=other').json() == []
        rows = list(csv.reader(io.StringIO(client.get(f"/api/competitions/{job['id']}/export").text.lstrip('\ufeff'))))
        assert len(rows) == 26 and rows[0][8] == "单品销量原文"
        assert "goods_id=" in rows[1][9]


def test_login_pauses_then_continues_with_the_same_keywords(tmp_path):
    provider = KeywordProvider(pause=True)
    app = create_app(tmp_path, FakeProvider(), provider)
    seed(app.state.store, 1)
    with TestClient(app, headers={"X-Workbench":"1"}) as client:
        job = client.post('/api/competitions', json={"source_task_id":"source-task", "keywords":{"10000001":"自定关键词"}}).json()
        paused = wait_job(client, job['id'], {'paused'})
        assert paused['action'] == 'login' and not paused['items'][0]['matches']
        duplicate = client.post('/api/competitions', json={"source_task_id":"source-task"}).json()
        assert duplicate['id'] == job['id']
        assert client.post('/api/pdd/browser/open', json={}).status_code == 200
        assert provider.opened
        client.post(f"/api/competitions/{job['id']}/resume", json={})
        assert wait_job(client, job['id'], {'completed'})['items'][0]['keyword'] == '自定关键词'
        assert provider.calls == ['自定关键词', '自定关键词']


def test_cancel_keeps_finished_items_and_allows_the_queue_to_continue(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        seed(store, 2)
        source_tasks = TaskManager(store, FakeProvider())
        manager = CompetitionManager(store, KeywordProvider(), source_tasks)
        await manager.start()
        job = manager.create(CompetitionRequest(source_task_id='source-task'))
        for _ in range(200):
            if store.competition(job['id'])['items'][0]['status'] == 'completed':
                break
            await asyncio.sleep(.01)
        manager.cancel(job['id'])
        await asyncio.wait_for(manager.queue.join(), 2)
        saved = store.competition(job['id'])
        assert saved['status'] == 'cancelled'
        assert len(saved['items'][0]['matches']) == 5 and saved['items'][1]['status'] == 'cancelled'
        next_job = manager.create(CompetitionRequest(source_task_id='source-task'))
        await asyncio.wait_for(manager.queue.join(), 4)
        assert store.competition(next_job['id'])['status'] == 'completed'
        await manager.close()
    asyncio.run(scenario())


def test_structural_failure_is_not_a_successful_empty_search(tmp_path):
    app = create_app(tmp_path, FakeProvider(), KeywordProvider(fail=True))
    seed(app.state.store, 1)
    with TestClient(app, headers={"X-Workbench":"1"}) as client:
        job = client.post('/api/competitions', json={"source_task_id":"source-task"}).json()
        failed = wait_job(client, job['id'], {'failed'})
        assert failed['message'] == '页面结构未识别'
        assert failed['items'][0]['status'] == 'failed'


def test_backup_and_restart_keep_competition_snapshots_but_not_login_sessions(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        seed(store, 1)
        manager = CompetitionManager(store, KeywordProvider(), TaskManager(store, FakeProvider()))
        job = manager.create(CompetitionRequest(source_task_id='source-task'))
        store.put_kv('pdd-browser-session', {'cookie':'不应备份的隔离测试占位'})
        backup = Backup.model_validate(store.backup())
        assert all(row['key'] != 'pdd-browser-session' for row in backup.tables['kv'])
        restored = Store(tmp_path/'restored')
        restored.restore(backup)
        assert restored.competition(job['id'])['status'] == 'interrupted'
        await manager.start()
        assert store.competition(job['id'])['status'] == 'interrupted'
        await manager.close()
    asyncio.run(scenario())


def test_api_rejects_running_sources_unknown_products_and_blank_keywords(tmp_path):
    app = create_app(tmp_path, FakeProvider(), KeywordProvider())
    task = seed(app.state.store)
    with TestClient(app, headers={"X-Workbench":"1"}) as client:
        for keywords in [{'10000001':'不在前5件'}, {'10000007':''}]:
            assert client.post('/api/competitions', json={'source_task_id':'source-task','keywords':keywords}).status_code in {400,422}
        task['status'] = 'running'
        app.state.store.put_task(task)
        assert client.post('/api/competitions', json={'source_task_id':'source-task'}).status_code == 400
        assert client.get('/api/competitions/missing').status_code == 404
        assert client.post('/api/competitions', json={'source_task_id':'missing'}).status_code == 400


def test_keywords_keep_the_product_name_and_encode_reserved_characters():
    assert search_keyword('一件代发 手机壳支架 批发') == '手机壳支架'
    assert 'search_key=%E6%89%8B%E6%9C%BA%E5%A3%B3%26%231' in keyword_url('手机壳&#1')
    with pytest.raises(ValueError):
        keyword_url(' ')


def test_small_screening_chains_only_first_five_after_twenty_sources(tmp_path):
    class TwentySources(FakeProvider):
        async def search(self,filters,seen):
            assert filters.limit==20
            for index in range(20):
                product=sample(str(20000000+index),title=f'货源{index}',sales_30d=1000-index,native_position=index+1,
                    native_search_url='https://s.1688.com/selloffer/offer_search.htm',source='1688网页')
                yield product
    app=create_app(tmp_path,TwentySources(),KeywordProvider())
    with TestClient(app,headers={'X-Workbench':'1'}) as client:
        result=client.post('/api/screenings',json={'keyword':'测试','limit':500,'shipping':'all'}).json()
        assert result['filters']['limit']==20 and result['compare_after'] is True
        # 此处的1秒保存间隔不影响逻辑，测试只等候真实队列完成。
        end=time.monotonic()+28
        task={}
        while time.monotonic()<end:
            task=client.get('/api/tasks/'+result['id']).json()
            if task.get('competition_id'):break
            time.sleep(.02)
        assert task['status']=='completed' and task['scanned']==20
        job=wait_job(client,task['competition_id'],{'completed'})
        assert len(job['items'])==5
        assert [i['source']['id'] for i in job['items']]==[str(20000000+i) for i in range(5)]
        assert sum(len(i['matches']) for i in job['items'])==25


def test_interrupted_job_resumes_remaining_sources_without_repeating_finished_ones(tmp_path):
    async def scenario():
        store=Store(tmp_path);seed(store,2)
        provider=KeywordProvider()
        manager=CompetitionManager(store,provider,TaskManager(store,FakeProvider()))
        job=manager.create(CompetitionRequest(source_task_id='source-task'))
        # 模拟一次已保存第一组后服务停止；新实例不自动继续。
        job['items'][0]['status']='completed'
        job['items'][0]['matches']=[competitor(1,'已拼100件').model_dump()]
        store.put_competition(job)
        restored=CompetitionManager(store,provider,TaskManager(store,FakeProvider()))
        await restored.start()
        assert store.competition(job['id'])['status']=='interrupted' and not provider.calls
        restored.resume(job['id'])
        await asyncio.wait_for(restored.queue.join(),3)
        assert len(provider.calls)==1
        assert len(store.competition(job['id'])['items'][0]['matches'])==1
        await restored.close()
    asyncio.run(scenario())


def test_candidates_with_missing_fields_are_insufficient_data_not_empty_or_failed(tmp_path):
    class Insufficient(KeywordProvider):
        async def search_by_title(self,keyword):
            return SearchBatch(scanned_count=20,excluded={'页面仅显示店铺销量':20})
    app=create_app(tmp_path,FakeProvider(),Insufficient());seed(app.state.store,1)
    with TestClient(app,headers={'X-Workbench':'1'}) as client:
        j=client.post('/api/competitions',json={'source_task_id':'source-task'}).json()
        item=wait_job(client,j['id'],{'completed'})['items'][0]
        assert item['status']=='insufficient_data' and item['candidates_count']==20
        assert item['excluded']=={'页面仅显示店铺销量':20}
