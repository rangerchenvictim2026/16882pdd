"""作品演示提供器：仅生成虚构资料，不联网、不启动浏览器。"""
import re
import tempfile
from datetime import timedelta
from pathlib import Path

from .competition_models import CompetitionItem, CompetitionJob, SearchBatch
from .competition_ranking import canonical_goods_url, parse_competitor, rank_competitors
from .models import Evidence, Product, SKU, SearchFilters, SelectionPatch, now, timestamp

COSTS = [7.8, 9.6, 13.5, 5.2, 11, 4.8, 8.2, 6.4, 3.6, 12.8, 6, 9.9, 14.5, 5.8, 8.6, 10.5, 7.2, 11.8, 4.5, 13]
NAMES = ['陶瓷饮水碗', '循环饮水器', '自动喂食器', '防滑餐垫', '互动玩具']
IMAGES = ['bowl', 'fountain', 'feeder', 'mat', 'toy']


def demo_products(keyword='宠物用品'):
    at = now()
    products = []
    for index, cost in enumerate(COSTS):
        product_id = str(90000001 + index)
        # 示例货源用保留的无效域名，界面不会把它当成真实商品链接。
        url = f'https://example.invalid/demo/{product_id}'
        values = {'published_at': '演示：三天前上新', 'dropship': '演示：支持一件代发',
                  'sales_30d': f'演示：近30天销量{2000-index*70}件', 'free_shipping': '演示：浙江杭州包邮',
                  'price': f'演示规格报价{cost-1:.2f}～{cost:.2f}元', 'platform_new': '演示：平台新品标识'}
        products.append(Product(id=product_id, title=f'【演示】{keyword} · {NAMES[index%5]} 示例{index+1:02}',
            url=url, image=f'/demo-assets/{IMAGES[index%5]}.svg', supplier_name=f'虚构供应商{index%4+1}',
            price_min=round(cost-1, 2), price_max=cost,
            skus=[SKU(name='演示标准款', price=round(cost-1, 2)), SKU(name='演示加大款', price=cost)],
            published_at=(at-timedelta(days=3)).isoformat(), dropship=True, sales_30d=2000-index*70,
            sales_unit='件', sales_exact=True, free_shipping=True, shipping_province='浙江', shipping_city='杭州',
            shipping_condition='演示：送至浙江杭州包邮', observed_at=at.isoformat(), source='虚构演示数据',
            native_position=index+1, evidence=[Evidence(field=field, text=text, url=url, observed_at=at.isoformat())
                                                         for field, text in values.items()]))
    return products


def demo_competitors(keyword, index=0):
    cost = COSTS[index % len(COSTS)]
    candidates = []
    for position, (sales, difference) in enumerate(zip([5800, 4200, 2800, 1600, 900, 500000], [5.5, 2.8, 8, -1, 0, 50])):
        price = round(cost+difference, 2)
        row = parse_competitor({'title': f'【演示】{NAMES[index%5]} 同类商品{position+1}',
            'url': canonical_goods_url(str(99000001+index*10+position)),
            'image': f'/demo-assets/{IMAGES[index%5]}.svg', 'price_text': f'¥{price:.2f}',
            'sales_text': f'{"本店已拼" if position==5 else "已拼"}{sales}件',
            'text': f'虚构演示资料，关键词：{keyword}；报价及销量均为示例'}, position)
        candidates.append(row)
    return SearchBatch(candidates=candidates, search_url='', scanned_count=6)


class DemoSource:
    def __init__(self):
        self.products = demo_products()
        self.progress = None

    def capabilities(self):
        return {'demo': True, 'state': 'demo', 'verification': 'unverified',
                'message': '演示模式：全部商品、价格、销量和供应商均为虚构资料',
                'fields': [], 'note': '不连接平台，不使用真实登录资料；时间为示例生成时间'}

    async def search(self, filters, seen):
        self.products = demo_products(filters.keyword)
        for product in self.products[:filters.limit]:
            if product.id not in seen:
                seen.add(product.id)
                yield product

    async def detail(self, product_id):
        return next(product for product in self.products if product.id == product_id)

    async def open(self):
        return self.capabilities()

    async def close(self):
        pass


class DemoCompetition:
    def __init__(self):
        self.progress = self.checkpoint = self.save_checkpoint = None

    def status(self):
        return {'browser': '演示', 'mode': 'demo', 'verified': True, 'retry_after': 0}

    async def open_login(self):
        return self.status()

    async def confirm_login(self, keyword=None):
        return self.status()

    async def search_by_title(self, keyword):
        number = re.search(r'示例(\d{2})', keyword)
        return demo_competitors(keyword, int(number[1])-1 if number else 0)

    async def close(self):
        pass


def seed_demo(store, products):
    stamp = timestamp()
    filters = SearchFilters(keyword='宠物用品', selection_mode='platform', shipping='free', max_price=15,
                            province='浙江', city='杭州', limit=20)
    task = {'id': 'demo-source', 'status': 'completed', 'filters': filters.model_dump(),
            'created_at': stamp, 'updated_at': stamp, 'scanned': 20, 'qualified': 20, 'excluded': 0,
            'reasons': {}, 'message': '演示资料已就绪；未访问真实平台', 'verification': 'unverified',
            'sort_scope': '固定演示顺序，不代表真实平台排名'}
    store.put_task(task)
    for product in products:
        store.put_product(product, task['id'], [])
    items = []
    for index, product in enumerate(products[:5]):
        batch = demo_competitors(product.title, index)
        matches, excluded = rank_competitors(batch.candidates)
        items.append(CompetitionItem(source=product, keyword=product.title, status='completed', method='title',
            image_status='unsupported', message='已生成虚构演示结果', candidates_count=6,
            matches=matches, excluded=excluded))
    job = CompetitionJob(id='demo-competition', source_task_id=task['id'], status='completed',
        message='演示已完成：5件虚构货源，各5条虚构竞品', created_at=stamp, updated_at=stamp,
        current_index=5, items=items, events=[{'time': stamp, 'message': '固定示例资料，不访问平台'}])
    store.put_competition(job.model_dump())
    store.save_selections([products[0].id])
    store.patch_selection(products[0].id, SelectionPatch(notes='演示备注：待核对同款规格，货价差尚未扣除费用', tags=['示例', '待打样']))
    store.put_kv('settings', {'province': '浙江', 'city': '杭州', 'default_limit': 20})


def create_demo(factory):
    # 始终新建临时目录，不接受或继承WORKBENCH_DATA_DIR。
    directory = Path(tempfile.mkdtemp(prefix='commerce-workbench-demo-'))
    source = DemoSource()
    app = factory(directory, source, DemoCompetition(), item_interval=.01)
    app.state.demo = True
    seed_demo(app.state.store, source.products)
    return app
