"""专用Chrome资料原地复用：可见登录、无头采集，禁止并发打开同一资料。"""
import asyncio
import json
import re
from urllib.parse import urlsplit,parse_qs

from .models import timestamp
from .access_cooldown import AccessCooldown
from .pdd_collection import AccessPace, LimitedCollection
from .pdd_provider import PDDKeywordProvider, keyword_url
from .providers import HumanActionRequired, SourceUnavailable

HOME='https://mobile.yangkeduo.com/'


class ChromePDDProvider(LimitedCollection,PDDKeywordProvider):
    def __init__(self,directory,launcher=None,pace=None):
        super().__init__(directory)
        self.mode=None
        self.verified=False
        self.verified_at=None
        self.launcher=launcher
        self.pace=pace or AccessPace()
        self.checkpoint=None
        self.save_checkpoint=None
        self.directory=directory
        self.network_failures=[]
        self.validation_keyword=None
        self.cooldown=AccessCooldown(directory/'pdd-access-state.json')

    def status(self):
        return {'browser':'Chrome','mode':self.mode or 'closed','verified':self.verified,
                'verified_at':self.verified_at,'candidate_limit':self.candidate_limit,
                'navigation_interval':self.pace.interval,'keyword_interval':self.pace.keyword_interval,
                'retry_after':self.cooldown.remaining}

    def _ensure_available(self):
        self.cooldown.ensure()

    def _backoff(self):
        self.verified=False
        self.cooldown.block()

    async def before_navigation(self,action):
        self._ensure_available()
        await self.pace.wait()

    async def _close_context(self):
        try:
            if self.context:
                await self.context.close()
        finally:
            self.context=self.page=None
            self.mode=None

    async def _launch(self,headless):
        if self.context and self.page and not self.page.is_closed() and self.mode==('headless' if headless else 'headed'):
            return
        await self._close_context()
        if self.launcher is None:
            if not self.playwright:
                from playwright.async_api import async_playwright
                self.playwright=await async_playwright().start()
            launch=self.playwright.chromium.launch_persistent_context
        else:
            launch=self.launcher
        try:
            self.context=await launch(str(self.profile),channel='chrome',headless=headless,locale='zh-CN',
                timezone_id='Asia/Shanghai',viewport={'width':1280,'height':850},accept_downloads=False)
            self.page=self.context.pages[0] if self.context.pages else await self.context.new_page()
            self.page.set_default_timeout(8000)
            def failed(request):
                code=re.search(r'net::ERR_[A-Z_]+',request.failure or '')
                self.network_failures.append({'host':urlsplit(request.url).hostname,'type':request.resource_type,
                                              'error':code.group() if code else '请求未完成'})
                self.network_failures=self.network_failures[-30:]
            if hasattr(self.page,'on'):
                self.page.on('requestfailed',failed)
            self.mode='headless' if headless else 'headed'
        except Exception as error:
            await self._close_context()
            self.verified=False
            raise SourceUnavailable('无法打开拼多多专用Chrome，请检查安装情况，或关闭仍占用该专用资料的旧窗口；登录资料未删除') from error

    async def open_login(self):
        async with self.launch_lock:
            if self.mode=='headed' and self.page and not self.page.is_closed():
                await self.page.bring_to_front()
                return self.status()
            target=HOME
            if self.page and not self.page.is_closed():
                path=urlsplit(self.page.url).path
                if '/login' in path:
                    target=HOME+'login.html'
                elif 'verify' in path:
                    target=self.page.url
            self.verified=False
            await self._launch(False)
            try:
                await self.page.goto(target,wait_until='domcontentloaded',timeout=30000)
                await self.page.bring_to_front()
            except Exception as error:
                raise SourceUnavailable('登录窗口已打开，但拼多多页面未加载完成；请直接在该窗口检查网络，不会循环重试') from error
            return self.status()

    async def check_page(self):
        try:
            body=await super().check_page()
            if '系统繁忙' in body or '请稍后再试' in body:
                self._backoff()
                raise HumanActionRequired('拼多多提示系统繁忙，已保存断点并停止请求；至少等待5分钟后手动验证，不连续刷新')
            return body
        except HumanActionRequired as error:
            self.verified=False
            if any(word in str(error) for word in ('系统繁忙','稍后再试')):
                raise
            if any(word in str(error) for word in ('限制访问','访问受限','频繁')):
                self._backoff()
            if '登录' in str(error):
                raise HumanActionRequired('拼多多要求重新登录，已停止请求并保存断点；请点击“登录拼多多”处理，再点击“验证登录并转后台”') from error
            raise HumanActionRequired('拼多多要求验证或限制访问，已停止请求并保存断点；请点击“登录拼多多”查看提示，处理后再继续') from error

    async def _verify_headless(self,force_reload=False):
        self._ensure_available()
        if not self.validation_keyword:
            raise HumanActionRequired('请先输入要检索的关键词，再验证后台会话')
        await self.pace.wait()
        try:
            current=urlsplit(self.page.url)
            if current.path!='/search_result.html' or parse_qs(current.query).get('search_key')!=[self.validation_keyword]:
                await self.page.goto(keyword_url(self.validation_keyword),wait_until='domcontentloaded',timeout=30000)
            elif force_reload:
                # 网络恢复后的手动验证只刷新一次，不能反复检查旧错误页而不给新请求机会。
                await self.page.reload(wait_until='domcontentloaded',timeout=30000)
            for _ in range(30):
                await self.page.wait_for_timeout(500)
                await self.check_page()
                listing=await self.read_cards()
                current=urlsplit(self.page.url)
                if current.path=='/search_result.html' and parse_qs(current.query).get('search_key')==[self.validation_keyword] and listing['cards']:
                    self.verified=True;self.verified_at=timestamp()
                    return self.status()
        except HumanActionRequired:
            try:
                await self.diagnose()
            except Exception:
                pass
            raise
        except Exception as error:
            self.verified=False
            raise SourceUnavailable('无头页面读取未通过，请打开登录窗口检查网络和登录；本次未清空浏览器资料，也不自动重复验证') from error
        self.verified=False
        try:
            await self.diagnose()
        except Exception:
            pass
        raise HumanActionRequired('无头页面尚未取得商品列表，请打开登录窗口检查，再手动验证；不会自动循环尝试')

    async def diagnose(self):
        """只留存公共商品字段和错误类别，不保存整页、账号、Cookie或地址查询参数。"""
        if not self.page or self.page.is_closed():
            return
        data=await self.page.evaluate(r'''() => {
          const visible=e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
          const text=document.body?.innerText||'';
          const messages=['网络异常','网络错误','网络开小差','请求失败','暂时无法','访问受限','请先登录','同意协议并登录','验证码','安全验证','请打开拼多多','App内打开','APP内打开','页面不存在','商品已下架','商品不存在'].filter(s=>text.includes(s));
          const nodes=[...document.body.querySelectorAll('*')].filter(e=>visible(e)&&e.children.length===0);
          return {path:location.pathname,title:document.title,messages,textLength:text.length,
            publicNotice:location.pathname==='/goods.html'&&text.length<=100&&!document.querySelector('input,textarea')?text:'',
            shortError:text.length<200&&/网络|失败|异常|加载|重试|繁忙|刷新|连接|错误|APP|App|验证|登录|不存在|下架/.test(text)?text:'',
            roots:[...document.body.children].slice(0,6).map(e=>({tag:e.tagName,cls:e.className,textLength:(e.innerText||'').length})),
            images:document.querySelectorAll('img').length,
            listingFields:location.pathname==='/search_result.html'?nodes.filter(e=>{const t=(e.innerText||'').trim();return t.length<100&&(/^\d[\d,.]*$/.test(t)||/已拼|已售|已抢|热销|总售|饮水|喝水/.test(t))}).slice(0,55).map(e=>({tag:e.tagName,cls:e.className,text:e.innerText,parent:e.parentElement.className})):[],
            prices:nodes.filter(e=>/^[¥￥]\s*\d[\d\s,.]*$/.test((e.innerText||'').trim())&&e.innerText.length<80).slice(0,12).map(e=>({tag:e.tagName,cls:e.className,text:e.innerText,parent:e.parentElement.className})),
            titles:nodes.filter(e=>(e.innerText||'').startsWith('猫咪饮水机流动水碗')&&e.innerText.length<180).slice(0,3).map(e=>({tag:e.tagName,cls:e.className,text:e.innerText,parent:e.parentElement.className})),
            knownTitle:!!document.querySelector('.tLYIg_Ju'),knownPrice:!!document.querySelector('.uPP2olgd')};
        }''')
        data['network_failures']=self.network_failures
        folder=self.directory/'diagnostics';folder.mkdir(parents=True,exist_ok=True)
        (folder/'pdd-headless-latest.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        if urlsplit(self.page.url).hostname=='mobile.yangkeduo.com' and urlsplit(self.page.url).path=='/search_result.html' and hasattr(self.page,'screenshot'):
            await self.page.screenshot(path=str(folder/'pdd-headless-latest.png'),full_page=False)
        return {'message':'已记录当前页面的公开商品字段；没有重新搜索或读取登录资料','path':data.get('path'),'messages':data.get('messages',[])}

    async def confirm_login(self,keyword=None):
        async with self.launch_lock:
            self._ensure_available()
            self.validation_keyword=(keyword or self.validation_keyword or '').strip()
            if not self.validation_keyword:
                raise ValueError('请先填写搜索关键词，再验证后台会话')
            keyword_url(self.validation_keyword)
            if self.mode=='headed' and self.page and not self.page.is_closed():
                await self.check_page()
            self.verified=False
            # 先正常关闭以落盘，再用同一专用目录无头启动，不复制Cookie。
            await self._launch(True)
            return await self._verify_headless(force_reload=True)

    async def open(self):
        async with self.launch_lock:
            self._ensure_available()
            if self.mode=='headed':
                raise HumanActionRequired('请完成专用Chrome登录后，点击“验证登录并转后台”；采集不会在登录窗口中抢占操作')
            if self.mode=='headless' and self.page and not self.page.is_closed():
                await self.check_page()
                if self.verified:
                    return
                raise HumanActionRequired('无头登录尚未验证，请先打开登录窗口并手动验证')
            await self._launch(True)
            await self._verify_headless()

    async def close(self):
        await self._close_context()
        self.verified=False
        if self.playwright:
            await self.playwright.stop()
            self.playwright=None
