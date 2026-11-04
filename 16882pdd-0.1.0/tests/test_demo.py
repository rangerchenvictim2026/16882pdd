"""演示复用业务流程，但必须与正式数据和平台浏览器隔离。"""
import time
import pytest

from fastapi.testclient import TestClient

from backend import main


def demo_app(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKBENCH_DEMO', '1')
    monkeypatch.setenv('WORKBENCH_DATA_DIR', str(tmp_path / '不能读取的正式资料'))
    return main.configured_app()


def wait_completed(client, path):
    for _ in range(600):
        value = client.get(path).json()
        if value['status'] in {'completed', 'failed'}:
            return value
        time.sleep(.01)
    raise AssertionError('演示流程未在限定时间内完成')


def test_demo_ignores_live_directory_and_has_twenty_sources_and_twenty_five_matches(tmp_path, monkeypatch):
    app = demo_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.get('/api/health').json()['demo'] is True
        assert client.get('/api/capabilities').json()['demo'] is True
        task = client.get('/api/tasks').json()[0]
        products = client.get(f"/api/tasks/{task['id']}/results").json()
        assert len(products) == 20
        assert all('演示' in p['title'] and p['image'].startswith('/demo-assets/') for p in products)
        job = client.get('/api/competitions').json()[0]
        assert job['status'] == 'completed' and len(job['items']) == 5
        assert all(len(item['matches']) == 5 for item in job['items'])
        assert all('演示' in row['title'] for item in job['items'] for row in item['matches'])
        assert len(client.get('/api/selections').json()) == 1
        assert client.get('/demo-assets/bowl.svg').status_code == 200
    assert not (tmp_path / '不能读取的正式资料').exists()


def test_demo_screening_runs_real_filter_ranking_gap_and_selection_workflow(tmp_path, monkeypatch):
    with TestClient(demo_app(tmp_path, monkeypatch), headers={'X-Workbench': '1'}) as client:
        task = client.post('/api/screenings', json={
            'keyword': '宠物用品', 'selection_mode': 'platform', 'max_price': 6,
            'shipping': 'free', 'province': '浙江', 'city': '杭州', 'limit': 100,
        }).json()
        finished = wait_completed(client, f"/api/tasks/{task['id']}")
        assert finished['status'] == 'completed' and finished['scanned'] == 20
        products = client.get(f"/api/tasks/{task['id']}/results").json()
        assert products and all(p['price_max'] <= 6 for p in products)
        for _ in range(200):
            jobs = client.get('/api/competitions', params={'source_task_id': task['id']}).json()
            if jobs:
                break
            time.sleep(.01)
        assert jobs
        job = wait_completed(client, f"/api/competitions/{jobs[0]['id']}")
        assert job['status'] == 'completed'
        assert len(job['items']) == min(5, len(products))
        assert all(len(item['matches']) == 5 for item in job['items'])
        assert all(item['matches'][0]['sales_value'] >= item['matches'][-1]['sales_value'] for item in job['items'])
        opportunities = client.get(f"/api/competitions/{job['id']}/opportunities").json()['rows']
        assert opportunities and all(row['gap_min'] > 0 and row['profit'] is None for row in opportunities)
        product_id = products[0]['id']
        assert client.post('/api/selections', json={'product_ids': [product_id]}).status_code == 200
        assert client.patch(f'/api/selections/{product_id}', json={'notes': '演示备注', 'status': '已选中'}).status_code == 200
        saved = next(row for row in client.get('/api/selections').json() if row['product_id'] == product_id)
        assert saved['notes'] == '演示备注'
        assert '演示' in client.get(f"/api/competitions/{job['id']}/opportunities/export").text


def test_demo_browser_controls_never_need_a_real_browser(tmp_path, monkeypatch):
    with TestClient(demo_app(tmp_path, monkeypatch), headers={'X-Workbench': '1'}) as client:
        for endpoint in ['/api/browser/open', '/api/pdd/browser/open', '/api/pdd/session/confirm']:
            assert client.post(endpoint, json={}).status_code == 200
        status = client.get('/api/pdd/session').json()
        assert status['browser'] == '演示' and status['mode'] == 'demo' and status['verified'] is True


def test_explicit_normal_mode_keeps_the_real_provider(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKBENCH_DEMO', '0')
    monkeypatch.setenv('WORKBENCH_DATA_DIR', str(tmp_path / '正式模式隔离测试'))
    app = main.configured_app()
    from backend.pdd_safari import SafariPDDProvider
    assert isinstance(app.state.competition.provider, SafariPDDProvider)
    assert not getattr(app.state, 'demo', False)


def test_demo_launcher_uses_a_separate_port_and_rejects_unknown_arguments():
    from scripts.start import launch_options
    assert launch_options(['--demo', '--no-open']) == (True, False, 8766)
    assert launch_options([]) == (False, True, 8765)
    with pytest.raises(RuntimeError, match='不支持的启动参数'):
        launch_options(['--随意参数'])
