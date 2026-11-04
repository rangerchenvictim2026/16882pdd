"""验证登录资料复用、模式互斥、访问节奏与候选级恢复，不访问平台。"""
import asyncio
from pathlib import Path

import pytest

from backend.pdd_chrome import ChromePDDProvider, HOME
from backend.pdd_collection import AccessPace, LimitedCollection
from backend.pdd_provider import PDDKeywordProvider, keyword_url
from backend.competition_models import CollectionCheckpoint
from backend.competition_ranking import canonical_goods_url, rank_competitors
from backend.models import timestamp
from backend.providers import HumanActionRequired

PROBE=keyword_url('测试')


class Text:
    def __init__(self,page):self.page=page
    async def inner_text(self):return self.page.body


class Page:
    def __init__(self,context):
        self.context=context;self.url=HOME;self.body='首页';self.visits=[]
    def is_closed(self):return self.context.closed
    def set_default_timeout(self,value):pass
    async def bring_to_front(self):pass
    def locator(self,selector):return Text(self)
    async def wait_for_timeout(self,value):pass
    async def goto(self,url,**options):
        self.visits.append(url)
        self.url=url
        if self.context.headless and not self.context.state['logged_in']:
            self.url=HOME+'login.html';self.body='同意协议并登录'
        else:self.body='商品资料'
    async def reload(self,**options):
        self.visits.append('手动刷新:'+self.url)
        self.body='商品资料'
    async def evaluate(self,script):
        if 'const nativeCards=' in script:
            return {'url':self.url,'cards':[{'title':'可访问的商品','price_text':'¥39.80'}]}
        return {'title':'已可访问的商品','price_text':'¥39.80','sales_text':'已拼100件'}


class Context:
    def __init__(self,state,headless):
        self.state=state;self.headless=headless;self.closed=False;self.pages=[Page(self)]
    async def close(self):self.closed=True


class Launcher:
    def __init__(self):self.state={'logged_in':False};self.calls=[];self.contexts=[]
    async def __call__(self,profile,**options):
        assert all(c.closed for c in self.contexts)
        self.calls.append((profile,options));context=Context(self.state,options['headless']);self.contexts.append(context);return context


def fast_pace():return AccessPace(interval=0,keyword_interval=0)


def test_manual_login_then_headless_share_one_profile_and_normal_close_keeps_files(tmp_path):
    async def run():
        launcher=Launcher();provider=ChromePDDProvider(tmp_path,launcher=launcher,pace=fast_pace());provider.validation_keyword='测试'
        provider.profile.mkdir();marker=provider.profile/'仅测试的本地资料';marker.write_text('保留')
        await provider.open_login()
        assert provider.mode=='headed' and not provider.verified
        launcher.state['logged_in']=True
        await provider.confirm_login('测试')
        assert provider.mode=='headless' and provider.verified
        assert [options['headless'] for _,options in launcher.calls]==[False,True]
        assert len({profile for profile,_ in launcher.calls})==1
        assert launcher.calls[0][0]==str(tmp_path/'pdd-chrome-profile')
        before=len(provider.page.visits)
        await provider.open();await provider.open()
        assert len(provider.page.visits)==before
        await provider.close()
        assert marker.read_text()=='保留'
    asyncio.run(run())


def test_login_challenge_stops_requests_until_user_changes_state(tmp_path):
    async def run():
        launcher=Launcher();provider=ChromePDDProvider(tmp_path,launcher=launcher,pace=fast_pace());provider.validation_keyword='测试'
        with pytest.raises(HumanActionRequired,match='登录'):
            await provider.open()
        assert provider.page.visits==[PROBE]
        with pytest.raises(HumanActionRequired):await provider.open()
        assert provider.page.visits==[PROBE]
        await provider.open_login()
        assert provider.mode=='headed'
        assert not any('storage_state' in options or 'user_agent' in options or 'args' in options for _,options in launcher.calls)
        await provider.close()
    asyncio.run(run())


def test_network_actions_obey_minimum_intervals_and_keyword_cooldown():
    async def run():
        values={'time':0};sleeps=[]
        async def sleep(seconds):sleeps.append(seconds);values['time']+=seconds
        pace=AccessPace(6,20,clock=lambda:values['time'],sleep=sleep)
        await pace.wait(keyword=True);await pace.wait();await pace.wait(keyword=True)
        assert sleeps==[6,14] and values['time']==20
    asyncio.run(run())


class Collector(LimitedCollection,PDDKeywordProvider):
    def __init__(self,directory,pause_at=None):
        super().__init__(directory);self.checkpoint=None;self.saved=[];self.save_checkpoint=self.saved.append
        self.pace=fast_pace();self.calls=[];self.reads=0;self.pause_at=pause_at
    async def open(self):pass
    async def _listing(self,keyword):pass
    async def check_page(self):return '商品列表'
    async def read_cards(self):
        self.reads+=1
        return {'url':keyword_url('测试')+'&sort_type=_sales','cards':[
            {'title':f'商品{i}','price_text':'¥12.80','text':f'已拼{i*100}件','locator':f'#商品{i}'} for i in range(1,21)]}
    async def resolve_card(self,row,keyword):
        if self.pause_at is not None and len(self.calls)==self.pause_at:
            raise HumanActionRequired('需要登录')
        self.calls.append(row.sales_value)
        row.id=str(100000+int(row.sales_value));row.url=canonical_goods_url(row.id)
        return row


def test_twenty_candidates_only_open_five_highest_eligible_details(tmp_path):
    async def run():
        collector=Collector(tmp_path)
        batch=await collector.search_by_title('测试')
        assert batch.scanned_count==20 and collector.reads==1
        assert collector.calls==[2000,1900,1800,1700,1600]
        assert len(rank_competitors(batch.candidates)[0])==5
        assert len(collector.saved[-1]['resolved_positions'])==5
    asyncio.run(run())


def test_new_provider_resumes_saved_candidates_without_reopening_completed_details(tmp_path):
    async def run():
        first=Collector(tmp_path,pause_at=2)
        with pytest.raises(HumanActionRequired):await first.search_by_title('测试')
        saved=CollectionCheckpoint.model_validate(first.saved[-1]).model_dump()
        resumed=Collector(tmp_path);resumed.checkpoint=saved
        batch=await resumed.search_by_title('测试')
        assert resumed.reads==0 and resumed.calls==[1800,1700,1600]
        assert len(rank_competitors(batch.candidates)[0])==5
    asyncio.run(run())


def test_busy_page_sets_persistent_cooldown_without_relaunching_or_clearing_profile(tmp_path):
    async def run():
        launcher=Launcher();provider=ChromePDDProvider(tmp_path,launcher=launcher,pace=fast_pace())
        await provider.open_login()
        provider.page.body='系统繁忙，请稍后再试'
        with pytest.raises(HumanActionRequired,match='系统繁忙'):
            await provider.check_page()
        assert provider.status()['retry_after']>0
        before=len(launcher.calls)
        with pytest.raises(HumanActionRequired,match='不会自动重试'):
            await provider.confirm_login('测试')
        assert len(launcher.calls)==before
        restored=ChromePDDProvider(tmp_path,launcher=Launcher(),pace=fast_pace())
        assert restored.status()['retry_after']>0
        await provider.close()
    asyncio.run(run())


def test_unknown_store_sales_have_a_small_detail_budget_instead_of_visiting_every_card(tmp_path):
    class StoreOnly(Collector):
        async def read_cards(self):
            result=await super().read_cards()
            for row in result['cards']:row['text']='本店已拼100万件'
            return result
    async def run():
        provider=StoreOnly(tmp_path)
        result=await provider.search_by_title('测试')
        assert len(provider.calls)==3 and result.scanned_count==20
        assert not result.candidates and not result.empty_verified
        assert result.excluded['页面仅显示店铺销量']==20
    asyncio.run(run())


def test_explicit_revalidation_refreshes_once_after_network_recovery(tmp_path):
    async def run():
        launcher=Launcher();launcher.state['logged_in']=True
        provider=ChromePDDProvider(tmp_path,launcher=launcher,pace=fast_pace());provider.validation_keyword='测试'
        await provider.open()
        provider.verified=False
        await provider.confirm_login('测试')
        assert provider.page.visits==[PROBE,'手动刷新:'+PROBE]
        await provider.open()
        assert len(provider.page.visits)==2
        await provider.close()
    asyncio.run(run())
