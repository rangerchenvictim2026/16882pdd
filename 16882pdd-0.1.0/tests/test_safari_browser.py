"""只用隔离替身验证Safari脚本与窗口约束，不发送真实Apple事件。"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.pdd_safari import SafariPDDProvider
from backend.pdd_chrome import ChromePDDProvider
from backend.pdd_collection import AccessPace, LimitedCollection
from backend.pdd_provider import keyword_url
from backend.providers import HumanActionRequired, SourceUnavailable
from backend.safari_browser import HOME, SafariBridge, SafariLocator, SafariPage, allowed_url, apple_string
from .test_workbench import FakeProvider


class RecordedBridge(SafariBridge):
    def __init__(self, responses=None):
        super().__init__()
        self.responses = list(responses or [])
        self.scripts = []

    async def _run(self, script):
        self.scripts.append(script)
        return self.responses.pop(0) if self.responses else ''


def test_default_application_selects_safari_without_opening_a_browser(tmp_path, monkeypatch):
    monkeypatch.delenv('WORKBENCH_PDD_BROWSER', raising=False)
    app = create_app(tmp_path, FakeProvider())
    assert isinstance(app.state.competition.provider, SafariPDDProvider)
    assert isinstance(app.state.competition.provider, LimitedCollection)
    assert app.state.competition.provider.context is None
    assert app.state.competition.provider.page is None


def test_chrome_can_be_explicitly_selected_without_automatic_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKBENCH_PDD_BROWSER', 'chrome')
    app = create_app(tmp_path, FakeProvider())
    assert isinstance(app.state.competition.provider, ChromePDDProvider)


def test_opening_without_safari_permission_returns_actionable_chinese_message(tmp_path):
    class PermissionProvider:
        async def open(self):
            raise HumanActionRequired("请在系统设置中允许启动程序控制Safari")
        async def close(self):
            pass
    app = create_app(tmp_path, FakeProvider(), PermissionProvider())
    with TestClient(app, headers={"X-Workbench":"1"}) as client:
        response = client.post('/api/pdd/browser/open',json={})
        assert response.status_code == 409
        assert response.json()['detail'] == "请在系统设置中允许启动程序控制Safari"


@pytest.mark.parametrize('url,expected', [
    (HOME,True), ('https://mobile.yangkeduo.com/goods.html?goods_id=123456',True),
    ('https://mobile.yangkeduo.com.evil.example/',False), ('http://mobile.yangkeduo.com/',False),
    ('https://user:pass@mobile.yangkeduo.com/',False), ('file:///etc/passwd',False),
])
def test_only_pinduoduo_consumer_pages_are_allowed(url, expected):
    assert allowed_url(url) is expected


def test_script_literals_escape_quotes_and_commands_are_not_shell_strings():
    escaped = apple_string('标题"\\\nend tell\ndo shell script "例子"')
    assert '\\"' in escaped and '\\\\' in escaped and '\\nend tell' in escaped
    assert '\n' not in escaped


def test_created_window_is_recorded_and_every_read_targets_only_that_window():
    async def scenario():
        bridge = RecordedBridge(['42',json.dumps({'url':HOME,'value':{'cards':[]}})])
        await bridge.create()
        assert bridge.window_id == 42
        assert await bridge.execute('({cards:[]})') == {'cards':[]}
        assert 'make new document' in bridge.scripts[0]
        assert 'window id 42' in bridge.scripts[1]
        assert 'count of tabs of targetWindow' in bridge.scripts[1]
        assert 'current tab of front window' not in bridge.scripts[1]
        assert 'document.cookie' not in bridge.scripts[1]
    asyncio.run(scenario())


def test_close_preserves_user_windows_or_extra_tabs():
    async def scenario():
        bridge = RecordedBridge()
        bridge.window_id = 42
        await bridge.close()
        assert bridge.window_id is None
        assert '(count of tabs of targetWindow) is 1' in bridge.scripts[0]
        assert 'close targetWindow' in bridge.scripts[0]
        assert 'quit' not in bridge.scripts[0]
        assert 'close every' not in bridge.scripts[0]
    asyncio.run(scenario())


def test_background_window_creation_does_not_activate_safari():
    async def scenario():
        bridge = RecordedBridge(['42'])
        await bridge.create(foreground=False)
        assert 'activate' not in bridge.scripts[0]
        assert 'make new document' in bridge.scripts[0]
    asyncio.run(scenario())


class SessionBridge:
    """仅模拟公开页面，永不连接真实Safari。"""
    def __init__(self):
        self.window_id = None
        self.url = HOME
        self.body = '商品列表'
        self.creations = []
        self.fronts = 0
        self.visits = []

    async def create(self, foreground=True):
        self.window_id = 42
        self.creations.append(foreground)

    async def execute(self, expression):
        if 'const nativeCards=' in expression:
            return {'url': self.url, 'cards': [{'title': '测试商品', 'price_text': '¥12.80'}]}
        if 'return el?.innerText' in expression:
            return self.body
        return 'complete'

    async def navigate(self, url):
        self.visits.append(url)
        self.url = url

    async def foreground(self):
        self.fronts += 1

    async def close(self):
        self.window_id = None


def test_safari_verifies_current_keyword_and_reuses_window_without_foregrounding(tmp_path, monkeypatch):
    async def scenario():
        async def no_wait(seconds):
            pass
        monkeypatch.setattr('backend.safari_browser.asyncio.sleep', no_wait)
        bridge = SessionBridge()
        provider = SafariPDDProvider(tmp_path, bridge=bridge, pace=AccessPace(0, 0))
        await provider.open_login()
        status = await provider.confirm_login('宠物饮水机')
        assert status['browser'] == 'Safari' and status['mode'] == 'window'
        assert status['verified'] and status['headless_supported'] is False
        assert bridge.visits == [keyword_url('宠物饮水机')]
        await provider.open()
        await provider.open()
        assert bridge.creations == [True] and bridge.fronts == 0
        await provider.open_login()
        assert bridge.fronts == 1 and len(bridge.visits) == 1
        await provider.close()
        assert not provider.status()['verified'] and provider.status()['mode'] == 'closed'
    asyncio.run(scenario())


@pytest.mark.parametrize('body, message', [('同意协议并登录', '登录'), ('安全验证', '验证'), ('系统繁忙，请稍后再试', '系统繁忙')])
def test_safari_challenges_stop_without_repeated_navigation(tmp_path, monkeypatch, body, message):
    async def scenario():
        async def no_wait(seconds):
            pass
        monkeypatch.setattr('backend.safari_browser.asyncio.sleep', no_wait)
        bridge = SessionBridge()
        bridge.body = body
        provider = SafariPDDProvider(tmp_path, bridge=bridge, pace=AccessPace(0, 0))
        with pytest.raises(HumanActionRequired, match=message):
            await provider.confirm_login('测试')
        assert bridge.visits == [keyword_url('测试')] and not provider.status()['verified']
        with pytest.raises(HumanActionRequired):
            await provider.open()
        assert len(bridge.visits) == 1
        if '系统繁忙' in body:
            restored = SafariPDDProvider(tmp_path, bridge=SessionBridge())
            assert restored.status()['retry_after'] > 0
            assert not (tmp_path / 'pdd-access-state.json').exists()
    asyncio.run(scenario())


def test_navigation_does_not_read_a_previous_keyword_as_the_new_result(monkeypatch):
    async def scenario():
        first = HOME+'search_result.html?search_key=old'
        target = HOME+'search_result.html?search_key=new'
        bridge = RecordedBridge(['',json.dumps({'url':first,'value':'complete'}),json.dumps({'url':target,'value':'complete'})])
        bridge.window_id = 42
        async def no_wait(seconds):
            pass
        monkeypatch.setattr('backend.safari_browser.asyncio.sleep',no_wait)
        await bridge.navigate(target)
        assert bridge.url == target and len(bridge.scripts) == 3
        assert 'set URL of targetTab' in bridge.scripts[0]
        with pytest.raises(ValueError):
            await bridge.navigate('https://example.com/')
    asyncio.run(scenario())


@pytest.mark.parametrize('error,message', [
    ('Not authorized to send Apple events to Safari. (-1743)','隐私与安全性'),
    ('JavaScript from Apple Events is not allowed.','允许来自Apple事件'),
    ('WB_WINDOW_CHANGED','标签页发生变化'),
    ('WB_SITE_CHANGED','离开拼多多'),
])
def test_permission_and_window_problems_pause_instead_of_changing_settings(monkeypatch,error,message):
    class Process:
        returncode = 1
        async def communicate(self, data):
            self.data = data
            return b'',error.encode()
    calls=[]
    async def create(*args,**kwargs):
        calls.append(args)
        return Process()
    monkeypatch.setattr('backend.safari_browser.sys.platform','darwin')
    monkeypatch.setattr('backend.safari_browser.asyncio.create_subprocess_exec',create)
    bridge = SafariBridge()
    bridge.window_id = 42
    with pytest.raises(HumanActionRequired,match=message):
        asyncio.run(bridge._run('检查页面'))
    assert calls == [('/usr/bin/osascript','-l','AppleScript','-')]
    if 'WB_WINDOW' in error:
        assert bridge.window_id is None


def test_permission_setup_is_not_written_into_the_browser_scripts():
    text = __import__('pathlib').Path('backend/safari_browser.py').read_text()
    assert 'defaults write' not in text and 'safaridriver --enable' not in text
    assert 'System Events' not in text


def test_safari_locator_passes_product_text_as_data_not_as_code():
    async def scenario():
        bridge = RecordedBridge([json.dumps({'url':HOME,'value':True})])
        bridge.window_id=42
        page = SafariPage(bridge)
        product='商品";alert(1);//'
        await page.get_by_text(product,exact=True).click()
        script=bridge.scripts[0]
        assert 'JSON.stringify' in script and 'spec.text' in script
        assert 'eval(' not in script
        assert '\\\"' in script
    asyncio.run(scenario())


def test_malformed_browser_result_and_cross_site_redirect_are_not_products():
    async def scenario():
        bridge = RecordedBridge(['not-json',json.dumps({'url':'https://example.com/','value':{}})])
        bridge.window_id=42
        with pytest.raises(SourceUnavailable):
            await bridge.execute('({})')
        with pytest.raises(HumanActionRequired):
            await bridge.execute('({})')
    asyncio.run(scenario())
