"""可替换的数据来源；网页接入仅读取可见信息，不调用未公开接口。"""
import asyncio
import re
import json
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Protocol

from .models import CHINA, Evidence, Product, SearchFilters, SKU, timestamp
from .native_search import build_search_url, search_params
from .card_values import prices_from_text, sales_from_text, clean_title


class HumanActionRequired(Exception):
    """登录、验证或访问限制需要人工处理。"""


class SourceUnavailable(Exception):
    """页面结构或数据能力不足，不能假装采集成功。"""


class ProductProvider(Protocol):
    async def search(self, filters: SearchFilters, seen: set[str]) -> AsyncIterator[Product]: ...
    async def detail(self, product_id: str) -> Product: ...
    async def close(self): ...


def parse_detail(raw: dict, product_id: str, observed_at: str | None = None) -> Product:
    """保守解析商品区域文本；没有明确标签就保留未知。"""
    observed_at = observed_at or timestamp()
    url = f"https://detail.1688.com/offer/{product_id}.html"
    text = raw.get("text", "")
    product = Product(id=product_id, title=raw.get("title") or f"1688商品 {product_id}", url=url,
                      image=raw.get("image", ""), supplier_name=raw.get("supplier_name", ""),
                      supplier_url=raw.get("supplier_url", ""), supplier_id=raw.get("supplier_id", ""), observed_at=observed_at)

    def proof(field, value):
        product.evidence.append(Evidence(field=field, text=value[:4000], url=url, observed_at=observed_at))

    published = re.search(r"(?:上架时间|上新时间|发布时间)\s*[:：]?\s*(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})日?(?:\s+(\d{1,2}):(\d{2}))?", text)
    if published:
        try:
            year, month, day, hour, minute = published.groups()
            product.published_at = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), tzinfo=CHINA).isoformat()
            proof("published_at", published.group())
        except ValueError:
            pass
    negative = re.search(r"(?:不支持|不提供|暂不支持)\s*一件代发", text)
    dropship = re.search(r"(?:^|\n)\s*(?:支持\s*)?一件代发\s*(?:[|｜·]|$|\n)", text)
    if negative:
        product.dropship = False
        proof("dropship", negative.group())
    elif dropship:
        product.dropship = True
        proof("dropship", dropship.group().strip())

    # 必须同时出现30天、计数和单位，成交额和买家人数均不匹配。
    sales = re.search(r"(?:近\s*30\s*(?:天|日)|30\s*天内)\s*(?:销量|销售|售出|成交)\s*[:：]?\s*(\d+(?:\.\d+)?)(万|千)?(\+)?\s*(件|笔)", text)
    if sales:
        number, scale, plus, unit = sales.groups()
        product.sales_30d = int(float(number) * {None: 1, "万": 10000, "千": 1000}[scale])
        product.sales_unit = unit
        product.sales_exact = not plus and not scale
        product.sales_is_lower_bound = bool(plus)
        proof("sales_30d", sales.group())

    # 价格来自单独提取的价格区，而非整页所有金额。
    price_text = raw.get("price_text", "")
    values = prices_from_text(price_text)
    if values:
        product.price_min, product.price_max = min(values), max(values)
        proof("price", price_text)
    for sku in raw.get("skus", []):
        if isinstance(sku.get("price"), (int, float)) and sku["price"] >= 0:
            product.skus.append(SKU(name=sku["name"], price=sku["price"]))
    if product.skus:
        # 规格报价优先于展示最低价。
        product.price_min = min(s.price for s in product.skus)
        product.price_max = max(s.price for s in product.skus)
        proof("price", "；".join(f"{s.name}：{s.price}元" for s in product.skus))

    # 运费必须是一个有明确目的地的报价区域，页面其他位置的包邮广告不采信。
    shipping = raw.get("shipping_text", "")
    region = re.search(r"(?:配送至|运送至|送至)\s*[:：]?\s*([^\n]{2,60})", shipping)
    if region:
        product.shipping_condition = shipping[:1000]
        # 地区由任务随后逐字核对，不从账号默认地址推断。
        if re.search(r"(?:运费|快递)\s*[:：]?\s*(?:[¥￥]\s*)?0(?:\.00)?\s*元?(?:$|\n)|(?:^|\n)\s*(?:运费[:：]?\s*)?包邮\s*(?:$|\n)", shipping):
            if not re.search(r"满\s*\d|偏远|首单|会员|新客|限量", shipping):
                product.free_shipping = True
                proof("free_shipping", shipping)
        else:
            fee = re.search(r"(?:运费|快递)\s*[:：]?\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*元", shipping)
            if fee and float(fee[1]) > 0:
                product.free_shipping = False
                proof("free_shipping", shipping)
    return product


def parse_search_card(raw: dict, search_url: str, position: int, observed_at: str | None = None) -> Product:
    """搜索卡片是原生筛选结果，详情字段缺失不会抹去这一来源。"""
    product_id = str(raw["id"])
    observed_at = observed_at or timestamp()
    raw = {**raw, "title": clean_title(raw.get("title", ""))}
    product = parse_detail(raw, product_id, observed_at)
    product.native_search_url = search_url
    product.native_position = position
    for evidence in product.evidence:
        evidence.url = search_url
    text = raw.get("text", "")
    product.platform_sales_text = sales_from_text(text, raw.get("sales_text", ""))
    if product.platform_sales_text:
        product.evidence.append(Evidence(field="platform_sales", text=product.platform_sales_text, url=search_url, observed_at=observed_at))
    if re.search(r"(?:^|\n)\s*新品\s*(?:$|\n)", text):
        product.evidence.append(Evidence(field="platform_new", text="商品卡片标记：新品；平台未提供具体上新天数", url=search_url, observed_at=observed_at))
    product.evidence.append(Evidence(field="search_card", text=text[:4000], url=search_url, observed_at=observed_at))
    proof = {"product_id": product.id, "card_url": product.url, "position": position}
    product.evidence.append(Evidence(field="native_search", text=json.dumps(proof, ensure_ascii=False), url=search_url, observed_at=observed_at))
    return product


SEARCH_SCRIPT = r"""() => {
  const visible = e => !!(e && e.getClientRects().length);
  const excluded = 'nav,header,footer,aside,[class*="recommend"],[class*="related"],[class*="advert"],[data-ad="true"]';
  const offerId = href => href?.match(/^https?:\/\/detail\.1688\.com\/offer\/(\d+)\.html(?:[?#]|$)/)?.[1];
  const links = Array.from(document.querySelectorAll('a[href*="/offer/"]')).filter(e=>visible(e) && !e.closest(excluded) && offerId(e.href));
  const unique = new Map();
  for(const link of links) {
    const id = offerId(link.href);
    if(unique.has(id)) continue;
    let card = link;
    for(let parent=link.parentElement, depth=0; parent && depth<8 && parent!==document.body; parent=parent.parentElement, depth++) {
      if(parent.matches('main') || parent.closest(excluded)) break;
      const ids = new Set(Array.from(parent.querySelectorAll('a[href*="/offer/"]')).map(a=>offerId(a.href)).filter(Boolean));
      if(ids.size>1 || parent.innerText.length>5000) break;
      card = parent;
    }
    const imgs=Array.from(card.querySelectorAll('img')).filter(e=>visible(e) && !/logo|icon|avatar/i.test(e.src) && e.naturalWidth>=40 && e.naturalHeight>=40 && e.naturalWidth<e.naturalHeight*5);
    if(link.tagName==='IMG' && !imgs.includes(link)) imgs.unshift(link);
    const sameLinks = [link,...card.querySelectorAll('a[href]')].filter(a=>offerId(a.href)===id);
    const titles = [...sameLinks.map(a=>a.getAttribute('title')),...sameLinks.map(a=>a.querySelector('img')?.alt),...sameLinks.map(a=>a.innerText?.trim())];
    const title = titles.map(t=>t?.split('\n').find(line=>line.trim())?.trim()).find(t=>t && t.length>=4 && !/^详情|^查看|^找相似/.test(t)) || '';
    if(!title || !card.innerText.trim()) continue;
    const supplier = Array.from(card.querySelectorAll('a[href]')).find(a=>!offerId(a.href) && /^https?:\/\/[^/.]+\.1688\.com(?:\/|$)/.test(a.href) && a.innerText.trim());
    const text=card.innerText;
    const priceNodes=Array.from(card.querySelectorAll('[class*="price"],[class*="Price"]')).filter(e=>visible(e) && !e.closest('s,del,[class*="original"],[class*="origin-price"],[class*="old-price"]'));
    const price_text=priceNodes.map(e=>e.innerText).filter(t=>t && t.length<150).join('\n');
    const salesNodes=Array.from(card.querySelectorAll('[class*="sale" i],[class*="trade" i],[class*="sold" i],[class*="volume" i],[class*="deal" i]')).filter(visible);
    const salesAttributes=Array.from(card.querySelectorAll('[title],[aria-label]')).filter(visible).flatMap(e=>[e.getAttribute('title'),e.getAttribute('aria-label')]).filter(t=>t && t.length<120 && /销量|已售|成交|售出|^\s*[\d,.]+[万千]?\+?\s*(件|笔|人)\s*$/.test(t));
    const sales_text=[...salesNodes.map(e=>e.innerText).filter(t=>t && t.length<150),...salesAttributes].join('\n');
    const image=imgs.sort((a,b)=>b.naturalWidth*b.naturalHeight-a.naturalWidth*a.naturalHeight).find(e=>!e.src.includes('/tfs/') && !/tps-\d+-\d+\.png/.test(e.src));
    unique.set(id,{id,title:title.trim(),text,sales_text,image:image?.currentSrc || image?.src || '',price_text:/[¥￥]/.test(price_text)?price_text:(text.match(/[¥￥]\s*\d+(?:\s*\.\s*\d{1,2})?/)?.[0] || ''),supplier_name:supplier?.innerText.trim()||'',supplier_url:supplier?.href||''});
  }
  return {url:location.href,cards:Array.from(unique.values())};
}"""


DETAIL_SCRIPT = r"""() => {
  const visible = e => !!(e && e.getClientRects().length);
  const root = document.querySelector('main') || document.body;
  const excluded = 'nav,aside,footer,header,script,style,[class*="recommend"],[class*="related"]';
  const text = Array.from(root.querySelectorAll('*')).filter(e=>visible(e) && !e.closest(excluded) && e.children.length===0).map(e=>e.innerText?.trim() || '').filter(Boolean).join('\n');
  const title = document.querySelector('h1');
  const prices = Array.from(root.querySelectorAll('[class*="price"]')).filter(e=>visible(e) && !e.closest(excluded) && /[¥￥]/.test(e.innerText) && e.innerText.length < 800);
  const shipping = Array.from(root.querySelectorAll('[class*="logistics"],[class*="freight"],[class*="shipping"]')).find(e=>visible(e) && !e.closest(excluded) && /配送至|运送至|送至/.test(e.innerText) && e.innerText.length<1200);
  const supplier = Array.from(document.querySelectorAll('a[href]')).find(e=>visible(e) && /https?:\/\/[^/]+\.1688\.com\/?(?:page\/index\.html)?(?:\?.*)?$/.test(e.href) && e.innerText.trim().length>2);
  const img = root.querySelector('img[src*="alicdn.com"]');
  // 逐规格价格只接受同一可见行内的明确金额，不按最低价扩展。
  const skus = Array.from(root.querySelectorAll('tr,[class*="sku-item"]')).filter(e=>visible(e) && !e.closest(excluded)).flatMap(e=>{
    const t=e.innerText.trim(), m=t.match(/[¥￥]\s*(\d+(?:\.\d{1,2})?)/);
    return m && t.length<160 ? [{name:t.replace(m[0],'').trim(),price:Number(m[1])}] : [];
  });
  return {text, title:title?.innerText?.trim() || '', image:img?.src || '',
    supplier_name:supplier?.innerText?.trim() || '',supplier_url:supplier?.href || '',
    price_text:prices.map(e=>e.innerText).join('\n'),shipping_text:shipping?.innerText || '',skus};
}"""


class BrowserProvider:
    def __init__(self, directory: Path):
        self.profile = directory / "chrome-profile"
        self.playwright = None
        self.context = None
        self.page = None
        self.launch_lock = asyncio.Lock()
        self.state = "closed"
        self.message = "请打开专用浏览器，手动登录1688"

    def capabilities(self):
        return {"provider": "1688本机浏览器", "verification": "unverified", "state": self.state,
                "message": self.message, "keyword_search": True, "image_search": False,
                "native_new_filter": True, "global_sales_sort": False, "native_sales_sort": True,
                "fields": [{"name": "平台新品 / 具体上新天数", "state": "使用原生新品筛选；具体天数未提供"},
                           {"name": "一件代发", "state": "待真实页面验证"},
                           {"name": "近30天销量", "state": "待真实页面验证"},
                           {"name": "地区包邮报价", "state": "待真实页面验证"}],
                "note": "已按用户提供的网页链接接入原生包邮、代发、新品及销量排序参数。新品天数、销量周期未提供时保持未知；真实页面卡片适配仍需验证。"}

    async def open(self):
        async with self.launch_lock:
            if self.context and self.page and not self.page.is_closed():
                return
            from playwright.async_api import async_playwright
            if self.context:
                await self.close()
            self.playwright = await async_playwright().start()
            try:
                self.context = await self.playwright.chromium.launch_persistent_context(
                    str(self.profile), channel="chrome", headless=False, locale="zh-CN", timezone_id="Asia/Shanghai",
                    viewport={"width": 1360, "height": 900}, accept_downloads=False)
                self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
                self.page.set_default_timeout(12000)
                self.state, self.message = "open", "专用浏览器已打开；请在其中完成登录或验证"
                await self.page.goto("https://www.1688.com/", wait_until="domcontentloaded", timeout=30000)
            except Exception as error:
                await self.close()
                raise SourceUnavailable("无法打开专用Chrome，请确认已安装Chrome且没有重复运行本工具") from error

    async def check_page(self):
        if not self.page or self.page.is_closed():
            raise HumanActionRequired("专用浏览器已关闭，请重新打开后继续")
        url = self.page.url
        text = (await self.page.locator("body").inner_text())[:30000]
        if any(word in url for word in ("login.1688", "passport.", "punish", "_____tmd_____")) or any(word in text for word in ("滑动验证", "安全验证", "访问受限", "请完成验证", "访问过于频繁")):
            self.state, self.message = "needs_action", "请在专用Chrome中完成登录或验证，然后点击继续"
            raise HumanActionRequired(self.message)

    async def read_search_cards(self, limit: int):
        """逐卡片滚入可见区域，给延迟渲染的销量与图片一次有上限的补全机会。"""
        listing = await self.page.evaluate(SEARCH_SCRIPT)
        captured = {str(card["id"]): card for card in listing["cards"]}
        for product_id in list(captured)[:limit]:
            card = captured[product_id]
            if sales_from_text(card.get("text", ""), card.get("sales_text", "")):
                continue
            try:
                await self.page.locator(f'a[href*="/offer/{product_id}.html"]').first.scroll_into_view_if_needed(timeout=4000)
            except Exception:
                await self.check_page()
                continue
            for _ in range(3):
                await self.page.wait_for_timeout(200)
                refreshed = await self.page.evaluate(SEARCH_SCRIPT)
                listing["url"] = refreshed["url"]
                for fresh in refreshed["cards"]:
                    fresh_id = str(fresh["id"])
                    if fresh_id in captured:
                        captured[fresh_id] = {**captured[fresh_id], **{k:v for k,v in fresh.items() if v not in ("",None)}}
                card = captured[product_id]
                if sales_from_text(card.get("text", ""), card.get("sales_text", "")):
                    break
            await self.check_page()
        return {"url": listing["url"], "cards": list(captured.values())}

    async def search(self, filters: SearchFilters, seen: set[str]) -> AsyncIterator[Product]:
        await self.open()
        search_url = build_search_url(filters)
        await self.page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        ranking = 0
        prior_page_ids = None
        visited = set()
        for page_index in range(25):
            await self.check_page()
            try:
                await self.page.locator('a[href*="/offer/"]').first.wait_for(state="attached", timeout=12000)
                await self.page.wait_for_function("""() => Array.from(document.querySelectorAll('a[href*="/offer/"]')).some(a =>
                  a.getClientRects().length && ((a.getAttribute('title')||'').trim().length>=4 || (a.innerText||'').trim().length>=4 || (a.querySelector('img')?.alt||'').trim().length>=4))""", timeout=12000)
            except Exception:
                await self.check_page()
                body = await self.page.locator("body").inner_text()
                if "请登录" in body or "登录后" in body:
                    raise HumanActionRequired("1688要求登录，请在专用Chrome登录后点击继续")
                raise SourceUnavailable("未识别到商品列表：可能无结果、页面结构变化或需要登录，请查看专用浏览器")
            listing = await self.read_search_cards(filters.limit-ranking)
            if not listing["cards"]:
                raise SourceUnavailable("读到了商品链接，但未取得有效商品卡片；请检查登录状态或页面结构，不能据此认定商品不合格")
            page_ids = tuple(card["id"] for card in listing["cards"])
            if page_ids == prior_page_ids:
                raise SourceUnavailable("分页商品没有变化，已停止重复采集")
            prior_page_ids = page_ids
            try:
                actual_params = search_params(listing["url"])
                expected_params = search_params(search_url)
                if any(actual_params.get(key) != value for key, value in expected_params.items() if key != "beginPage"):
                    raise SourceUnavailable("搜索页跳转后筛选参数发生变化，已暂停采集，请检查原生筛选条件")
            except ValueError as error:
                raise SourceUnavailable("当前页面不是可核实的1688筛选搜索页") from error
            for card in listing["cards"]:
                product_id = str(card["id"])
                if product_id in visited:
                    continue
                visited.add(product_id)
                ranking += 1
                if ranking > filters.limit:
                    return
                if product_id in seen:
                    continue
                product = parse_search_card(card, listing["url"], ranking)
                # 严格模式按需补充详情，原生模式不因详情缺失丢失搜索卡片。
                if filters.selection_mode == "strict":
                    detail = await self.detail(product_id)
                    await self.page.goto(listing["url"], wait_until="domcontentloaded", timeout=30000)
                    detail.native_search_url, detail.native_position = product.native_search_url, product.native_position
                    detail.platform_sales_text = product.platform_sales_text
                    detail.evidence.extend(e.model_copy(update={"observed_at": detail.observed_at}) for e in product.evidence if e.field == "native_search")
                    if detail.title == f"1688商品 {product_id}":
                        detail.title = product.title
                    if not detail.image or "/tfs/" in detail.image:
                        detail.image = product.image
                    product = detail
                # 仅将明确报价地区映射到本次设置，未确认的地区保持空值。
                shipping_text = " ".join(e.text for e in product.evidence if e.field == "free_shipping")
                region = re.search(r"(?:配送至|运送至|送至)\s*[:：]?\s*([^\n]{2,60})", shipping_text)
                if region and filters.province and filters.city and filters.province in region[1] and filters.city in region[1]:
                    product.shipping_province, product.shipping_city = filters.province, filters.city
                seen.add(product_id)
                yield product
            if ranking >= filters.limit:
                return
            await self.check_page()
            next_button = self.page.get_by_text("下一页", exact=True)
            if await next_button.count() != 1 or not await next_button.is_visible():
                return
            if await next_button.get_attribute("disabled") is not None or await next_button.get_attribute("aria-disabled") == "true":
                return
            previous = await self.page.locator('a[href*="/offer/"]').evaluate_all("es=>es.map(e=>e.href).join('|')")
            await next_button.click()
            try:
                await self.page.wait_for_function("previous => Array.from(document.querySelectorAll('a[href*=\"/offer/\"]')).map(e=>e.href).join('|') !== previous", arg=previous, timeout=12000)
            except Exception:
                raise SourceUnavailable("分页未变化，已停止重复采集；请检查页面或缩小采集数量")
        raise SourceUnavailable("已达到单次分页保护上限，请缩小搜索范围")

    async def refresh(self, product_id: str, filters: SearchFilters) -> Product:
        if filters.selection_mode == "strict":
            return await self.detail(product_id)
        # 重新搜索验证当前是否仍属于原生筛选结果，不能借用旧链接证明新详情。
        async for product in self.search(filters.model_copy(update={"limit": 500}), set()):
            if product.id == product_id:
                return product
        raise SourceUnavailable("当前平台筛选结果中未找到该商品，旧资料与个人备注已保留")

    async def detail(self, product_id: str) -> Product:
        await self.open()
        await self.page.goto(f"https://detail.1688.com/offer/{product_id}.html", wait_until="domcontentloaded", timeout=30000)
        await self.check_page()
        try:
            await self.page.locator("h1").first.wait_for(state="visible", timeout=12000)
        except Exception:
            await self.check_page()
            raise SourceUnavailable("商品详情缺少可识别的标题，可能已下架、未登录或页面结构发生变化")
        return parse_detail(await self.page.evaluate(DETAIL_SCRIPT), product_id)

    async def close(self):
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self.playwright:
            await self.playwright.stop()
        self.context, self.page, self.playwright = None, None, None
        self.state = "closed"
