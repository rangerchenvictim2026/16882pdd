"""货价差采用报价上下限，不凭当前销量推断增长或净利润。"""
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.opportunities import opportunities
from .test_workbench import FakeProvider,sample
from .test_competition_ranking import competitor


def job():
    source=sample('10000001',price_min=8,price_max=12).model_dump()
    return {'id':'测试任务','status':'completed','items':[{'source':source,'keyword':'测试','matches':[
        competitor(1,'已拼100件',price='¥12.80').model_dump(),
        competitor(2,'已拼200件',price='¥10.00').model_dump(),
        competitor(3,'本店已拼100万件',price='¥99').model_dump()]}]}


def test_gap_uses_source_max_and_competitor_min_and_never_claims_profit():
    result=opportunities(job())
    assert len(result['rows'])==1
    row=result['rows'][0]
    assert row['gap_min']==.8 and row['reference_cost']==12 and row['reference_price']==12.8
    assert row['profit'] is None and '待核对' in row['verification']
    assert result['excluded']=={'保守货价差未超过门槛':1,'当前单品销量不足':1}
    assert not opportunities(job(),minimum_gap=.8)['rows']


def test_range_prices_do_not_treat_minimum_source_cost_as_all_skus():
    j=job();j['items'][0]['matches']=[competitor(1,'已拼100件',price='¥15～20').model_dump()]
    row=opportunities(j)['rows'][0]
    assert row['gap_min']==3 and row['gap_max']==12


def test_result_export_and_invalid_threshold_are_checked(tmp_path):
    app=create_app(tmp_path,FakeProvider())
    app.state.store.put_competition(job())
    with TestClient(app) as client:
        result=client.get('/api/competitions/测试任务/opportunities').json()
        assert len(result['rows'])==1
        exported=client.get('/api/competitions/测试任务/opportunities/export').text
        assert '保守货价差' in exported and '0.8' in exported
        assert client.get('/api/competitions/测试任务/opportunities?minimum_gap=nan').status_code==400
