"""拼多多消费者网页关键词接入；只读取页面，不调用未公开接口。"""
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlencode, urlsplit, parse_qs

from .competition_models import SearchBatch
from .card_values import clean_title, prices_from_text
from .competition_ranking import canonical_goods_url, parse_competitor, parse_pdd_sales, pdd_goods_id
from .providers import HumanActionRequired, SourceUnavailable


def keyword_url(keyword: str) -> str:
    keyword = keyword.strip()
    if not keyword or len(keyword) > 80:
        raise ValueError("搜索关键词不能为空，且不能超过80个字")
    return "https://mobile.yangkeduo.com/search_result.html?" + urlencode({"search_key": keyword})


PDD_CARDS_SCRIPT = r"""() => {
  const visible = el => el && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
  const path = el => {const parts=[];while(el && el.tagName!=='BODY'){
    const siblings=el.parentElement ? [...el.parentElement.children].filter(n=>n.tagName===el.tagName):[];
    parts.unshift(el.tagName.toLowerCase()+':nth-of-type('+(siblings.indexOf(el)+1)+')');el=el.parentElement;
  }return 'body > '+parts.join(' > ')};
  const cardSelector='._1cbPZFE7, .rjNMXsUm, [class*="goods-item"], [class*="GoodsItem"], [class*="goods-card"], a[href*="goods_id="]';
  const nativeCards=[...document.querySelectorAll('.rjNMXsUm')];
  const nodes = new Set(nativeCards.length?nativeCards:document.querySelectorAll(cardSelector));
  for(const img of nativeCards.length?[]:document.querySelectorAll('img')){
    if(img.closest(cardSelector))continue;
    let p=img.parentElement;
    for(let i=0;p&&i<6;i++,p=p.parentElement){
      const t=p.innerText||'';
      if(t.length>15&&t.length<900&&p.querySelectorAll('img').length<=3&&
        (p.querySelector('.m9lfFP_K, [class*="price"], [class*="Price"]')||/[¥￥]\s*\d/.test(t))){nodes.add(p);break;}
    }
  }
  const cards=[];const seen=new Set();
  for(const card of nodes){
    if(!visible(card))continue;
    const text=(card.innerText||'').trim();if(text.length>1200)continue;
    const img=card.querySelector('img');
    const titleEl=card.querySelector('._3ANzdjkc, .J9WPy2Wu, [class*="goodsName"], [class*="goods-name"], [class*="title"], [class*="Title"]');
    const title=(titleEl?.innerText||img?.alt||text.split('\n').find(t=>t.trim().length>=4&&!/^[¥￥\d]|^(本店|全店|已拼|已抢|已售|热销|总售|退货|极速|立减)/.test(t.trim()))||'').trim();
    const priceEl=card.querySelector('._3gmVc4Lg, .m9lfFP_K, [class*="goods-price"], [class*="price"], [class*="Price"]');
    const priceSource=priceEl?.innerText||text;
    let price=(priceSource.match(/[¥￥]\s*\d[\d,]*(?:\s*\.\s*\d{1,2})?(?:\s*[～~-]\s*\d[\d,]*(?:\s*\.\s*\d{1,2})?)?/)?.[0]||priceEl?.innerText||'').trim();
    if(/^\d[\d,.]*(?:\s*[～~-]\s*\d[\d,.]*)?$/.test(price))price='¥'+price;
    const link=card.matches('a[href]')?card:card.querySelector('a[href*="goods_id="], a[href*="goodsId="]');
    const image=img?.currentSrc||img?.src||'';
    const key=title+'|'+image;if(!title||!price||seen.has(key))continue;seen.add(key);
    cards.push({title,price_text:price,text,image,url:link?.href||'',locator:path(card)});
  }
  return {url:location.href,cards};
}"""


PDD_DETAIL_SCRIPT = r"""() => {
  const visible = el => el && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
  const first = selector => [...document.querySelectorAll(selector)].find(visible);
  const title=first('.tLYIg_Ju, h1, [class*="goods-title"], [class*="goodsTitle"]');
  const price=first('.uPP2olgd');
  const sales=first('.SkePSPI2');
  return {title:(title?.innerText||'').trim(),price_text:(price?.innerText||'').trim(),sales_text:(sales?.innerText||'').trim()};
}"""


class PDDKeywordProvider:
    def __init__(self, directory: Path):
        self.profile = directory / "pdd-chrome-profile"
        self.playwright = self.context = self.page = None
        self.launch_lock = asyncio.Lock()
        self.progress = None

    async def before_navigation(self, action):
        """具体提供器可在页面访问前限速；纯解析测试无需等待。"""
        pass

    async def open(self):
        async with self.launch_lock:
            if self.context and self.page and not self.page.is_closed():
                await self.page.bring_to_front()
                return
            from playwright.async_api import async_playwright
            await self.close()
            self.playwright = await async_playwright().start()
            try:
                self.context = await self.playwright.chromium.launch_persistent_context(
                    str(self.profile), channel="chrome", headless=False, locale="zh-CN",
                    timezone_id="Asia/Shanghai", viewport={"width": 1280, "height": 850},
                    accept_downloads=False)
                self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
                self.page.set_default_timeout(8000)
                await self.page.goto("https://mobile.yangkeduo.com/", wait_until="domcontentloaded", timeout=30000)
            except Exception as error:
                await self.close()
                raise SourceUnavailable("无法打开拼多多专用Chrome，请确认已安装Chrome且没有重复启动") from error

    async def check_page(self):
        if not self.page or self.page.is_closed():
            raise HumanActionRequired("拼多多专用浏览器已关闭，请打开浏览器后继续")
        body = (await self.page.locator("body").inner_text())[:30000]
        if "/login" in urlsplit(self.page.url).path or "同意协议并登录" in body or "请先登录" in body:
            raise HumanActionRequired("请在拼多多专用Chrome中手动登录，然后点击“已处理，继续”")
        if any(word in body for word in ("滑动验证", "安全验证", "请完成验证")):
            raise HumanActionRequired("拼多多要求验证，请在专用浏览器中处理后继续")
        if any(word in body for word in ("访问受限", "访问过于频繁", "操作过于频繁")):
            raise HumanActionRequired("拼多多限制访问，请稍后在专用浏览器中检查，已停止请求")
        return body

    async def read_cards(self):
        return await self.page.evaluate(PDD_CARDS_SCRIPT)

    async def resolve_card(self, row, keyword):
        """没有商品链接时正常点击卡片取得地址，并补读商品区域销量。"""
        if row.id and row.sales_scope == "product" and row.sales_unit and row.price_min is not None and row.price_max is not None:
            return row
        needs_action = False
        try:
            # 相同的截断标题可能对应不同商品，先核对本卡片位置和主图。
            identity = json.dumps({"locator":row.locator,"title":row.title,"image":row.image}, ensure_ascii=False)
            verified = await self.page.evaluate('''() => {const expected=EXPECTED;const card=expected.locator?document.querySelector(expected.locator):null;
              const img=card?.querySelector('img');return !!card&&(card.innerText||'').includes(expected.title)&&
              (!expected.image||(img?.currentSrc||img?.src||'')===expected.image)}'''.replace('EXPECTED',identity))
            if verified:
                target = self.page.locator(row.locator)
            else:
                # Safari返回列表后会重建节点，旧位置失效时按主图、标题及报价重新定位。
                target = None
                for attempt in range(5):
                    listing = await self.read_cards()
                    matches=[]
                    for raw in listing["cards"]:
                        current=parse_competitor(raw)
                        if current.title == row.title and current.price_min == row.price_min and current.price_max == row.price_max and (
                            (row.image and current.image == row.image) or (not row.image and current.sales_text == row.sales_text)):
                            matches.append(raw)
                    if len(matches) == 1:
                        target=self.page.locator(matches[0]["locator"])
                        break
                    if attempt < 4:
                        await self.before_navigation("滚动定位")
                        await self.page.mouse.wheel(0,650)
                        await self.page.wait_for_timeout(450)
                if target is None:
                    return row
            await target.scroll_into_view_if_needed(timeout=3000)
            await self.before_navigation("打开详情")
            await target.click(timeout=4000)
            for _ in range(30):
                await self.page.wait_for_timeout(500)
                detail = await self.page.evaluate(PDD_DETAIL_SCRIPT)
                goods_id = pdd_goods_id(self.page.url)
                if goods_id and detail["title"] and prices_from_text(detail["price_text"]):
                    row.id, row.url = goods_id, canonical_goods_url(goods_id)
                    row.title = clean_title(detail["title"])[:1000]
                    # 详情中的价格与销量仅用于补缺，保留搜索列表报价和排名口径。
                    if row.price_min is None or row.price_max is None:
                        values=prices_from_text(detail["price_text"])
                        row.price_text=detail["price_text"]
                        row.price_min,row.price_max=min(values),max(values)
                    if row.sales_scope != "product" or row.sales_value is None or not row.sales_unit:
                        sales=parse_pdd_sales(detail["sales_text"])
                        if sales[3] == "product":
                            row.sales_text, row.sales_value, row.sales_unit, row.sales_scope, row.sales_approximate = sales
                    break
                await self.check_page()
            return row
        except asyncio.CancelledError:
            needs_action = True
            raise
        except HumanActionRequired:
            needs_action = True
            raise
        except Exception:
            try:
                await self.check_page()
            except HumanActionRequired:
                needs_action = True
                raise
            return row
        finally:
            if not needs_action and self.page and not self.page.is_closed() and urlsplit(self.page.url).path != "/search_result.html":
                # 登录页保留给用户处理，不立刻跳走。
                if "/login" not in urlsplit(self.page.url).path:
                    await self.before_navigation("返回列表")
                    await self.page.go_back(wait_until="domcontentloaded", timeout=15000)

    async def search_by_title(self, keyword: str):
        await self.open()
        url = keyword_url(keyword)
        # 用户刚登录完成时，可以直接读取已经返回的同一搜索页。
        current = urlsplit(self.page.url)
        if current.path != "/search_result.html" or parse_qs(current.query).get("search_key") != [keyword]:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await self.page.wait_for_timeout(700)
        await self.check_page()
        native_sort = False
        sort = self.page.get_by_text(re.compile(r"^销量(?:优先)?$"))
        for option in await sort.all():
            if await option.is_visible():
                await option.click()
                native_sort = True
                await self.page.wait_for_timeout(500)
                break
        captured = {}
        actual_url = url
        for _ in range(5):
            body = await self.check_page()
            current = urlsplit(self.page.url)
            if current.path != "/search_result.html" or parse_qs(current.query).get("search_key") != [keyword]:
                raise SourceUnavailable("拼多多没有停留在本次关键词结果页，请查看专用浏览器")
            listing = await self.read_cards()
            # 保存关键词与原生排序依据，不保存登录账户或推广跟踪参数。
            sorted_by_sales = parse_qs(urlsplit(listing["url"]).query).get("sort_type") == ["_sales"]
            native_sort = sorted_by_sales
            actual_url = url + ("&sort_type=_sales" if sorted_by_sales else "")
            for raw in listing["cards"]:
                key = (raw["title"], raw.get("image", ""))
                captured[key] = raw
            if self.progress:
                self.progress(f"已读取 {min(40, len(captured))} 个候选，正在补充商品信息")
            if len(captured) >= 40:
                break
            await self.page.mouse.wheel(0, 650)
            await self.page.wait_for_timeout(450)
        if not captured:
            body = await self.check_page()
            if re.search(r"暂无相关商品|没有找到相关商品|未找到相关商品|无搜索结果|没有相关商品", body):
                return SearchBatch(search_url=actual_url, empty_verified=True, native_sales_sort=native_sort)
            raise SourceUnavailable("未识别到拼多多商品卡片，请检查登录、页面加载或页面结构；本次没有伪造竞品结果")
        candidates = []
        for index, raw in enumerate(list(captured.values())[:40]):
            if self.progress:
                self.progress(f"正在核验候选 {index + 1} / {min(40, len(captured))} 的销量与链接")
            row = parse_competitor(raw, index)
            row = await self.resolve_card(row, keyword)
            candidates.append(row)
        return SearchBatch(candidates=candidates, search_url=actual_url, native_sales_sort=native_sort)

    async def close(self):
        try:
            if self.context:
                await self.context.close()
        finally:
            self.context = self.page = None
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None
