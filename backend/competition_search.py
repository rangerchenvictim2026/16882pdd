"""竞品检索流程：只有图搜明确无结果时，才允许改用标题。"""
from typing import Protocol

from .competition_models import CompetitionItem, SearchBatch
from .competition_ranking import rank_competitors
from .models import Product
from .providers import SourceUnavailable


class CompetitionProvider(Protocol):
    async def search_by_image(self, source: Product) -> SearchBatch: ...
    async def search_by_title(self, title: str) -> SearchBatch: ...


async def search_competition(source: Product, provider: CompetitionProvider,
                             allow_title_fallback: bool = True) -> CompetitionItem:
    if not source.image:
        raise SourceUnavailable("货源主图未取得，无法进行图搜；请先刷新商品资料")

    # 登录、验证、入口不可用等异常直接交给任务层处理，不能冒充没有同款。
    batch = await provider.search_by_image(source)
    item = CompetitionItem(source=source, method="image")
    if batch.candidates:
        item.image_status = "found"
    elif not batch.empty_verified:
        raise SourceUnavailable("尚未确认图搜结果，请检查页面加载或图搜入口；未自动转标题搜索")
    else:
        item.image_status = "empty"
        if allow_title_fallback:
            item.method = "title"
            item.fallback_reason = "图搜已明确返回无结果"
            batch = await provider.search_by_title(source.title)

    if not batch.candidates and not batch.empty_verified:
        raise SourceUnavailable("尚未确认标题搜索结果，请检查页面加载状态")

    item.search_url = batch.search_url
    item.candidates_count = len(batch.candidates)
    item.matches, item.excluded = rank_competitors(batch.candidates)
    if item.matches:
        item.status = "completed"
        item.message = f"已按本次读取的单品销量展示值排序，保留前 {len(item.matches)} 名"
    elif batch.candidates:
        item.status = "insufficient_data"
        item.message = "已找到候选商品，但售价、单品销量或链接不足，暂不能形成排行榜"
    else:
        item.status = "no_results"
        item.message = "图搜及标题搜索均无结果" if item.method == "title" else "图搜无结果"
    return item
