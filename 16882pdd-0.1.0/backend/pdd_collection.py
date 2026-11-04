"""小批量竞品采集：先排列表，再补前列链接，保存候选级检查点。"""
import asyncio
import time
from datetime import datetime
from collections import Counter
from urllib.parse import parse_qs, urlsplit

from .competition_models import CollectionCheckpoint, SearchBatch
from .competition_ranking import parse_competitor, rank_competitors
from .models import now, timestamp
from .pdd_provider import keyword_url
from .providers import HumanActionRequired, SourceUnavailable


class AccessPace:
    def __init__(self, interval=6.0, keyword_interval=20.0, clock=None, sleep=None):
        self.interval, self.keyword_interval = interval, keyword_interval
        self.clock, self.sleep = clock or time.monotonic, sleep or asyncio.sleep
        self.last_access = self.last_keyword = None

    async def wait(self, keyword=False):
        current=self.clock()
        target=current if self.last_access is None else self.last_access+self.interval
        if keyword and self.last_keyword is not None:
            target=max(target,self.last_keyword+self.keyword_interval)
        if target>current:
            await self.sleep(target-current)
        self.last_access=self.clock()
        if keyword:
            self.last_keyword=self.last_access


class LimitedCollection:
    """由浏览器提供器复用；仅持久化商品资料，不保存登录凭证。"""
    candidate_limit=20
    detail_budget=10
    checkpoint=None
    save_checkpoint=None

    def _save(self, checkpoint):
        self.checkpoint=checkpoint
        if self.save_checkpoint:
            self.save_checkpoint(checkpoint.model_dump())

    def _progress(self, message):
        if self.progress:
            self.progress(message)

    async def _listing(self, keyword):
        current=urlsplit(self.page.url)
        if current.path!='/search_result.html' or parse_qs(current.query).get('search_key')!=[keyword]:
            await self.pace.wait(keyword=True)
            await self.page.goto(keyword_url(keyword),wait_until='domcontentloaded',timeout=30000)
        await self.page.wait_for_timeout(1000)
        await self.check_page()
        if parse_qs(urlsplit(self.page.url).query).get('sort_type')!=['_sales']:
            import re
            for sort in await self.page.get_by_text(re.compile(r'^销量(?:优先)?$')).all():
                if await sort.is_visible():
                    await self.before_navigation('销量排序')
                    await sort.click()
                    await self.page.wait_for_timeout(1000)
                    break

    async def search_by_title(self, keyword):
        saved=self.checkpoint
        checkpoint=CollectionCheckpoint.model_validate(saved) if isinstance(saved,dict) else saved
        if checkpoint and checkpoint.keyword!=keyword:
            checkpoint=None
        if checkpoint and (now()-datetime.fromisoformat(checkpoint.collected_at)).total_seconds()>86400:
            raise SourceUnavailable('此件断点已超过24小时，请新建检索，避免混用过期报价')
        self.validation_keyword=keyword
        await self.open()
        await self._listing(keyword)
        if checkpoint is None:
            captured={}
            native=False
            for page_index in range(3):
                await self.check_page()
                listing=await self.read_cards()
                current=urlsplit(listing['url'])
                if current.path!='/search_result.html' or parse_qs(current.query).get('search_key')!=[keyword]:
                    raise SourceUnavailable('拼多多页面不属于本次关键词，已停止读取')
                native=parse_qs(current.query).get('sort_type')==['_sales']
                for raw in listing['cards']:
                    captured[(raw['title'],raw.get('image',''))]=raw
                    if len(captured)>=self.candidate_limit:
                        break
                self._progress(f'已读取 {len(captured)} 个候选；优先补齐销量前列商品')
                if len(captured)>=self.candidate_limit:
                    break
                if page_index<2:
                    await self.before_navigation('滚动列表')
                    await self.page.mouse.wheel(0,650)
                    await self.page.wait_for_timeout(1000)
            if not captured:
                import re
                body=await self.check_page()
                if re.search(r'暂无相关商品|没有找到相关商品|未找到相关商品|无搜索结果|没有相关商品',body):
                    return SearchBatch(search_url=keyword_url(keyword),empty_verified=True,scanned_count=0)
                raise SourceUnavailable('未识别到拼多多商品卡片，请查看登录窗口、网络或页面结构；未将加载失败当作无结果')
            checkpoint=CollectionCheckpoint(keyword=keyword,collected_at=timestamp(),
                search_url=keyword_url(keyword)+('&sort_type=_sales' if native else ''),native_sales_sort=native,
                rows=[parse_competitor(raw,index) for index,raw in enumerate(list(captured.values())[:self.candidate_limit])])
            self._save(checkpoint)
        else:
            self._progress(f'复用已保存的 {len(checkpoint.rows)} 个候选，不重新采集列表')

        done=set(checkpoint.resolved_positions)
        used=len(done)
        unknown_used=0
        # 已有明确单品销量和价格的商品先排名；未知口径只在有限预算内补充。
        for row in checkpoint.rows:
            if row.sales_scope=='product' and row.sales_value is not None and row.sales_unit and row.price_min is not None:
                continue
            if row.page_position in done:
                unknown_used+=1
                continue
            if unknown_used>=3 or used>=self.detail_budget:
                continue
            await self.resolve_card(row,keyword)
            done.add(row.page_position);used+=1;unknown_used+=1
            checkpoint.resolved_positions=sorted(done);self._save(checkpoint)

        ordered,excluded=rank_competitors(checkpoint.rows,require_links=False,limit=self.candidate_limit)
        checked=[]
        for row in ordered:
            if row.page_position not in done and not row.id:
                if used>=self.detail_budget:
                    excluded['详情访问达到本次上限']=excluded.get('详情访问达到本次上限',0)+1
                    break
                self._progress(f'正在补充前列商品链接，已核验 {len(checked)} 条；单页访问至少间隔6秒')
                await self.resolve_card(row,keyword)
                done.add(row.page_position);used+=1
                checkpoint.resolved_positions=sorted(done);self._save(checkpoint)
            checked.append(row)
            valid,_=rank_competitors(checked)
            if len(valid)>=5:
                break
        reasons=Counter(excluded)
        return SearchBatch(candidates=checked,scanned_count=len(checkpoint.rows),excluded=dict(reasons),
                           search_url=checkpoint.search_url,native_sales_sort=checkpoint.native_sales_sort)
