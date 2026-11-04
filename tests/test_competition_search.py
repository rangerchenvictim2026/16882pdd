"""区分图搜无结果、登录中断与入口不可用，避免静默替换搜索方式。"""
import asyncio

import pytest

from backend.competition_models import SearchBatch
from backend.competition_search import search_competition
from backend.models import Product, timestamp
from backend.providers import HumanActionRequired, SourceUnavailable
from .test_competition_ranking import competitor


class RecordedProvider:
    def __init__(self, image, title=None):
        self.image, self.title = image, title
        self.calls = []

    async def search_by_image(self, source):
        self.calls.append(("图片", source.image))
        if isinstance(self.image, Exception):
            raise self.image
        return self.image

    async def search_by_title(self, title):
        self.calls.append(("标题", title))
        return self.title


def source():
    return Product(id="1000001", title="测试货源标题", image="https://example.com/product.jpg",
                   url="https://detail.1688.com/offer/1000001.html", observed_at=timestamp())


@pytest.mark.parametrize("failure", [
    HumanActionRequired("请先登录拼多多"), SourceUnavailable("此网页要求前往 APP 图搜"),
])
def test_unavailable_image_search_does_not_become_title_search(failure):
    provider = RecordedProvider(failure)
    with pytest.raises(type(failure)):
        asyncio.run(search_competition(source(), provider))
    assert [call[0] for call in provider.calls] == ["图片"]


def test_unconfirmed_empty_page_does_not_trigger_fallback():
    provider = RecordedProvider(SearchBatch())
    with pytest.raises(SourceUnavailable, match="尚未确认图搜结果"):
        asyncio.run(search_competition(source(), provider))
    assert len(provider.calls) == 1


def test_confirmed_empty_image_search_falls_back_to_the_source_title():
    provider = RecordedProvider(SearchBatch(empty_verified=True),
                                SearchBatch(candidates=[competitor(1, "已拼100件")]))
    item = asyncio.run(search_competition(source(), provider))
    assert provider.calls == [("图片", source().image), ("标题", source().title)]
    assert item.method == "title" and item.image_status == "empty"
    assert item.matches[0].sales_value == 100


def test_image_results_are_ranked_before_limiting_to_five():
    provider = RecordedProvider(SearchBatch(candidates=[competitor(i, f"已拼{i}件") for i in range(1, 9)]))
    item = asyncio.run(search_competition(source(), provider))
    assert item.method == "image" and len(provider.calls) == 1
    assert [row.sales_value for row in item.matches] == [8, 7, 6, 5, 4]


def test_incomplete_image_candidates_do_not_trigger_title_search():
    provider = RecordedProvider(SearchBatch(candidates=[competitor(1, "本店已拼100万件")]))
    item = asyncio.run(search_competition(source(), provider))
    assert item.status == "insufficient_data" and len(provider.calls) == 1
    assert item.excluded == {"页面仅显示店铺销量": 1}


def test_missing_source_image_does_not_trigger_any_search():
    product = source()
    product.image = ""
    provider = RecordedProvider(SearchBatch())
    with pytest.raises(SourceUnavailable, match="货源主图未取得"):
        asyncio.run(search_competition(product, provider))
    assert not provider.calls
