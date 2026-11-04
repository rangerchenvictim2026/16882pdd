"""仅供界面联调的隔离服务，不访问1688、不接触正式数据库。"""
import tempfile
from pathlib import Path

from backend.main import create_app
from .test_native_search import NativeProvider, native_filters, card
from .test_competition_tasks import KeywordProvider
from starlette.responses import HTMLResponse
from starlette.routing import Route

directory=Path(tempfile.mkdtemp(prefix="workbench-ui-test-"))
app = create_app(directory, NativeProvider(directory,native_filters(),[[
    card("10000001","隔离测试 · 宠物用品卡片一"),
    card("10000002","隔离测试 · 宠物用品卡片二"),
]]), KeywordProvider(pause=True))


async def fixture(request):
    return HTMLResponse('''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>隔离商品卡片结构测试</title><body>
    <header><a href="https://detail.1688.com/offer/88888888.html">导航中的商品链接</a></header>
    <main><div class="search-list">
    <article class="offer-card"><a title="隔离结构测试商品一" href="https://detail.1688.com/offer/10000001.html"><img width="80" height="80" alt="隔离结构测试商品一" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='80' height='80'%3E%3C/svg%3E">隔离结构测试商品一</a><div class="price">¥12.50</div><p>已售999件</p><a href="https://test-supplier.1688.com/">隔离供应商</a></article>
    <article class="offer-card"><a title="隔离结构测试商品二" href="https://detail.1688.com/offer/10000002.html">隔离结构测试商品二</a><div class="price">¥9.00</div><p>成交99笔</p></article>
    </div><div class="recommend"><a href="https://detail.1688.com/offer/77777777.html">不应采集的推荐商品</a></div></main></body></html>''')


app.router.routes.insert(0,Route("/fixture/search",fixture))


async def pdd_fixture(request):
    return HTMLResponse('''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>隔离拼多多卡片结构测试</title><body>
    <main><div class="_1cbPZFE7"><img alt="隔离测试竞品手机壳" width="40" height="40" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E"><div class="_1_mHsNT0"><span class="J9WPy2Wu">隔离测试竞品手机壳</span><div class="m9lfFP_K">12.80</div><span>已拼1.2万+件</span></div></div>
    <div class="_1cbPZFE7"><a href="https://mobile.yangkeduo.com/goods.html?goods_id=100002"><span class="J9WPy2Wu">隔离测试店铺销量</span></a><div class="m9lfFP_K">9.90</div><span>本店已拼500万件</span></div></main>
    </body></html>''')


app.router.routes.insert(0,Route("/fixture/pdd",pdd_fixture))
