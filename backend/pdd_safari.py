"""复用Safari现有登录会话；窗口采集限速执行，不伪称无头。"""
import asyncio
from urllib.parse import parse_qs, urlsplit

from .access_cooldown import AccessCooldown
from .models import timestamp
from .pdd_collection import AccessPace, LimitedCollection
from .pdd_provider import PDDKeywordProvider, keyword_url
from .providers import HumanActionRequired, SourceUnavailable
from .safari_browser import SafariBridge, SafariPage


class SafariPDDProvider(LimitedCollection, PDDKeywordProvider):
    def __init__(self, directory, bridge=None, pace=None):
        super().__init__(directory)
        self.profile = None
        self.bridge = bridge or SafariBridge()
        self.pace = pace or AccessPace()
        self.cooldown = AccessCooldown(directory / 'pdd-safari-access-state.json')
        self.verified = False
        self.verified_at = None
        self.validation_keyword = None
        self.checkpoint = self.save_checkpoint = None
        self.validation_lock = asyncio.Lock()

    def status(self):
        return {'browser': 'Safari', 'mode': 'window' if self.page and not self.page.is_closed() else 'closed',
                'headless_supported': False, 'verified': self.verified, 'verified_at': self.verified_at,
                'candidate_limit': self.candidate_limit, 'navigation_interval': self.pace.interval,
                'keyword_interval': self.pace.keyword_interval, 'retry_after': self.cooldown.remaining}

    async def _ensure_window(self, foreground=False):
        async with self.launch_lock:
            if self.page and not self.page.is_closed():
                if foreground:
                    await self.page.bring_to_front()
                return
            self.verified = False
            await self.bridge.create(foreground=foreground)
            self.page = SafariPage(self.bridge)
            for _ in range(10):
                try:
                    await self.page.wait_for_timeout(500)
                    return
                except SourceUnavailable:
                    continue
            raise SourceUnavailable('Safari首页尚未就绪，请检查页面后再手动验证')

    async def open_login(self):
        await self._ensure_window(foreground=True)
        return self.status()

    async def open(self):
        self.cooldown.ensure()
        await self._ensure_window()
        await self.check_page()

    async def before_navigation(self, action):
        self.cooldown.ensure()
        await self.pace.wait()

    async def check_page(self):
        try:
            body = await super().check_page()
            if '系统繁忙' in body or '请稍后再试' in body:
                self.cooldown.block()
                raise HumanActionRequired('Safari中的拼多多提示系统繁忙，已停止请求并保存断点；至少等待5分钟后手动验证，不连续刷新')
            return body
        except HumanActionRequired as error:
            self.verified = False
            if any(word in str(error) for word in ('限制访问', '访问受限', '频繁')):
                self.cooldown.block()
            message = str(error).replace('拼多多专用Chrome', 'Safari采集窗口').replace('专用浏览器', 'Safari采集窗口')
            raise HumanActionRequired(message) from error

    async def confirm_login(self, keyword=None):
        async with self.validation_lock:
            self.cooldown.ensure()
            target = keyword_url(keyword or self.validation_keyword or '')
            self.validation_keyword = (keyword or self.validation_keyword).strip()
            self.verified = False
            await self._ensure_window()
            # 用户明确验证时只重新请求一次，避免持续读取旧错误页；任务不自动刷新。
            await self.pace.wait(keyword=True)
            await self.page.goto(target, wait_until='domcontentloaded', timeout=30000)
            for _ in range(20):
                await self.page.wait_for_timeout(500)
                await self.check_page()
                listing = await self.read_cards()
                current = urlsplit(listing['url'])
                if current.path == '/search_result.html' and parse_qs(current.query).get('search_key') == [self.validation_keyword] and listing['cards']:
                    self.verified = True
                    self.verified_at = timestamp()
                    return self.status()
            raise SourceUnavailable('Safari尚未取得当前关键词的商品列表，请检查页面；本次不会自动重新搜索')

    async def close(self):
        await self.bridge.close()
        self.page = None
        self.verified = False
