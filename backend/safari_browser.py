"""Safari普通会话的独立采集窗口；不更改系统权限，不导出登录资料。"""
import asyncio
import json
import sys
from urllib.parse import parse_qs, urlsplit

from .providers import HumanActionRequired, SourceUnavailable

ORIGIN = "https://mobile.yangkeduo.com"
HOME = ORIGIN + "/"


def apple_string(value: str) -> str:
    """通过脚本标准输入传递文本；不拼接终端命令。"""
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\r', '\\r').replace('\n', '\\n') + '"'


def allowed_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return parsed.scheme == "https" and parsed.hostname == "mobile.yangkeduo.com" and not parsed.username and not parsed.password
    except ValueError:
        return False


class SafariBridge:
    def __init__(self):
        self.window_id = None
        self.url = HOME
        self.lock = asyncio.Lock()

    async def _run(self, script):
        if sys.platform != "darwin":
            raise SourceUnavailable("Safari采集仅支持macOS")
        async with self.lock:
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/osascript", "-l", "AppleScript", "-",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                output, errors = await asyncio.wait_for(process.communicate(script.encode("utf-8")), 20)
            except (asyncio.TimeoutError, asyncio.CancelledError) as error:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise HumanActionRequired("Safari连接超时，请先检查系统是否提示允许启动程序控制Safari；也可在系统设置→隐私与安全性→自动化中检查Safari权限，处理后重新点击“查看Safari”") from error
            if process.returncode:
                message = errors.decode("utf-8", errors="replace")
                if "-1743" in message or "not authorized" in message.lower() or "不允许发送" in message:
                    raise HumanActionRequired("请在macOS的隐私与安全性→自动化中，允许工作台启动程序控制Safari，然后继续")
                if "JavaScript" in message and any(word in message.lower() for word in ("apple", "不允许", "not allowed", "禁用")):
                    raise HumanActionRequired("请在Safari设置→开发者中启用“允许来自Apple事件的JavaScript”，然后继续；程序不会自动更改该权限")
                if "WB_WINDOW" in message:
                    self.window_id = None
                    raise HumanActionRequired("Safari采集窗口已关闭或标签页发生变化，请点击“查看Safari”重新打开后继续")
                if "WB_SITE" in message:
                    raise HumanActionRequired("Safari采集窗口已离开拼多多，请返回拼多多页面后继续")
                raise SourceUnavailable("Safari未能执行本次页面操作，请检查采集窗口与系统提示后重试")
            return output.decode("utf-8").strip()

    def _target(self):
        if self.window_id is None:
            raise HumanActionRequired("Safari采集窗口尚未打开，请先打开后继续")
        return f'''if not (exists window id {int(self.window_id)}) then error "WB_WINDOW_MISSING"
set targetWindow to window id {int(self.window_id)}
if (count of tabs of targetWindow) is not 1 then error "WB_WINDOW_CHANGED"
set targetTab to tab 1 of targetWindow
set currentURL to URL of targetTab
if currentURL is not {apple_string(ORIGIN)} and currentURL does not start with {apple_string(HOME)} then error "WB_SITE_CHANGED"
'''

    async def create(self,foreground=True):
        activation='activate\n' if foreground else ''
        result = await self._run(f'''tell application id "com.apple.Safari"
{activation}
make new document with properties {{URL:{apple_string(HOME)}}}
return id of front window as text
end tell''')
        try:
            self.window_id = int(result)
        except ValueError as error:
            raise SourceUnavailable("Safari没有返回有效的采集窗口标识") from error
        self.url = HOME

    async def execute(self, expression):
        script = "JSON.stringify({url:location.href,value:(" + expression + ")})"
        result = await self._run('tell application id "com.apple.Safari"\n' + self._target() +
                                 "return do JavaScript " + apple_string(script) + " in targetTab\nend tell")
        try:
            envelope = json.loads(result)
            if not allowed_url(envelope["url"]):
                raise HumanActionRequired("Safari页面已离开拼多多，请返回后继续")
            self.url = envelope["url"]
            return envelope.get("value")
        except (ValueError, KeyError, TypeError) as error:
            raise SourceUnavailable("Safari页面尚未就绪，未取得可读取的页面信息，请稍后重试") from error

    async def navigate(self, url):
        if not allowed_url(url):
            raise ValueError("Safari采集仅允许打开拼多多消费者网页")
        await self._run('tell application id "com.apple.Safari"\n' + self._target() +
                        "set URL of targetTab to " + apple_string(url) + "\nend tell")
        # Safari地址栏会先改变，等待文档也完成跳转，避免读取上一批商品。
        for _ in range(40):
            await asyncio.sleep(.25)
            try:
                await self.execute("document.readyState")
            except SourceUnavailable:
                continue
            current, expected = urlsplit(self.url), urlsplit(url)
            same_keyword = parse_qs(current.query).get("search_key") == parse_qs(expected.query).get("search_key")
            if (current.path == expected.path and same_keyword) or "/login" in current.path or "verify" in current.path:
                return
        raise SourceUnavailable("Safari页面跳转超时，请检查网络后重试")

    async def foreground(self):
        await self._run('tell application id "com.apple.Safari"\n' + self._target() +
                        "set index of targetWindow to 1\nactivate\nend tell")

    async def close(self):
        if self.window_id is None:
            return
        # 只关闭本服务创建且仍保持单标签的拼多多窗口，保留用户原有窗口。
        window_id, self.window_id = self.window_id, None
        try:
            await self._run(f'''tell application id "com.apple.Safari"
if exists window id {int(window_id)} then
set targetWindow to window id {int(window_id)}
if (count of tabs of targetWindow) is 1 then
set currentURL to URL of tab 1 of targetWindow
if currentURL is {apple_string(ORIGIN)} or currentURL starts with {apple_string(HOME)} then close targetWindow
end if
end if
end tell''')
        except (HumanActionRequired, SourceUnavailable):
            pass


LOCATE = r"""spec => {
  const visible=el=>el.getClientRects().length&&getComputedStyle(el).visibility!=='hidden';
  if(spec.css)return [...document.querySelectorAll(spec.css)];
  const match=el=>{const text=(el.innerText||'').trim();return spec.pattern?new RegExp(spec.pattern).test(text):spec.exact?text===spec.text:text.includes(spec.text)};
  return [...document.body.querySelectorAll('*')].filter(el=>visible(el)&&!['SCRIPT','STYLE','INPUT','TEXTAREA'].includes(el.tagName)&&match(el)&&![...el.children].some(child=>visible(child)&&match(child)));
}"""


class SafariLocator:
    def __init__(self, page, spec, index=0):
        self.page, self.spec, self.index = page, spec, index

    @property
    def first(self):
        return SafariLocator(self.page, self.spec, 0)

    async def _value(self, operation):
        spec = json.dumps(self.spec, ensure_ascii=False)
        return await self.page.bridge.execute(f"(()=>{{const nodes=({LOCATE})({spec});const el=nodes[{self.index}];{operation}}})()")

    async def count(self):
        return await self._value("return nodes.length;")

    async def all(self):
        return [SafariLocator(self.page, self.spec, index) for index in range(await self.count())]

    async def inner_text(self):
        return await self._value("return el?.innerText||'';")

    async def is_visible(self):
        return bool(await self._value("return !!el&&el.getClientRects().length>0;"))

    async def click(self, **options):
        if not await self._value("if(!el)return false;el.click();return true;"):
            raise SourceUnavailable("Safari没有找到要点击的商品或排序入口")

    async def scroll_into_view_if_needed(self, **options):
        await self._value("if(el)el.scrollIntoView({block:'center',behavior:'instant'});return !!el;")


class SafariMouse:
    def __init__(self, page):
        self.page = page

    async def wheel(self, x, y):
        await self.page.bridge.execute(f"(()=>{{window.scrollBy({int(x)},{int(y)});return true}})()")


class SafariPage:
    def __init__(self, bridge):
        self.bridge = bridge
        self.mouse = SafariMouse(self)

    @property
    def url(self):
        return self.bridge.url

    def is_closed(self):
        return self.bridge.window_id is None

    def locator(self, selector):
        return SafariLocator(self, {"css":selector})

    def get_by_text(self, text, exact=False):
        return SafariLocator(self, {"pattern":text.pattern} if hasattr(text,"pattern") else {"text":text,"exact":exact})

    async def evaluate(self, script):
        return await self.bridge.execute("(" + script + ")()")

    async def goto(self, url, **options):
        await self.bridge.navigate(url)

    async def wait_for_timeout(self, milliseconds):
        await asyncio.sleep(milliseconds / 1000)
        await self.bridge.execute("document.readyState")

    async def go_back(self, **options):
        original_path = urlsplit(self.url).path
        await self.bridge.execute("(()=>{history.back();return true})()")
        for _ in range(30):
            await self.wait_for_timeout(250)
            if urlsplit(self.url).path != original_path:
                return
        raise SourceUnavailable("Safari未能返回搜索列表，请检查页面后重试")

    async def bring_to_front(self):
        await self.bridge.foreground()
