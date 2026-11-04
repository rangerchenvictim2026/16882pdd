"""核验网页登录中断与无结果判断，不连接真实平台。"""
import asyncio

import pytest

from backend.pdd_provider import PDDKeywordProvider, keyword_url
from backend.providers import HumanActionRequired, SourceUnavailable
from .test_competition_ranking import competitor


class TextLocator:
    def __init__(self, text):
        self.text = text

    async def inner_text(self):
        return self.text

    async def all(self):
        return []


class Page:
    def __init__(self, url, text):
        self.url, self.text = url, text
        self.mouse = self

    def is_closed(self):
        return False

    def locator(self, selector):
        return TextLocator(self.text)

    def get_by_text(self, text):
        return TextLocator('')

    async def wait_for_timeout(self, timeout):
        pass

    async def wheel(self, x, y):
        pass


@pytest.mark.parametrize('url,text', [
    ('https://mobile.yangkeduo.com/login.html', '手机登录'),
    (keyword_url('手机壳'), '同意协议并登录'),
    (keyword_url('手机壳'), '请完成验证'),
])
def test_login_and_verification_are_actionable_pauses(tmp_path, url, text):
    provider = PDDKeywordProvider(tmp_path)
    provider.page = Page(url, text)
    with pytest.raises(HumanActionRequired):
        asyncio.run(provider.check_page())


@pytest.mark.parametrize('text,empty', [('没有找到相关商品', True), ('页面加载中', False)])
def test_only_an_explicit_empty_message_counts_as_no_results(tmp_path, text, empty):
    class EmptyProvider(PDDKeywordProvider):
        async def open(self):
            pass

        async def read_cards(self):
            return {'url':self.page.url, 'cards':[]}

    provider = EmptyProvider(tmp_path)
    provider.page = Page(keyword_url('手机壳'), text)
    if empty:
        assert asyncio.run(provider.search_by_title('手机壳')).empty_verified
    else:
        with pytest.raises(SourceUnavailable, match='未识别到'):
            asyncio.run(provider.search_by_title('手机壳'))


def test_verified_product_links_do_not_need_an_extra_click(tmp_path):
    provider = PDDKeywordProvider(tmp_path)
    row = competitor(1, '已拼300件')
    assert asyncio.run(provider.resolve_card(row, '手机壳')) == row


def test_detail_verification_page_is_preserved_for_the_user(tmp_path):
    class Target:
        async def count(self):
            return 1
        @property
        def first(self):
            return self
        async def scroll_into_view_if_needed(self, **options):
            pass
        async def click(self, **options):
            pass

    class VerificationPage(Page):
        def __init__(self):
            super().__init__('https://mobile.yangkeduo.com/verify.html', '请完成验证')
            self.went_back = False
        def get_by_text(self, *args, **kwargs):
            return Target()
        async def go_back(self, **options):
            self.went_back = True

    provider = PDDKeywordProvider(tmp_path)
    provider.page = VerificationPage()
    row = competitor(1, '本店已拼100万件')
    with pytest.raises(HumanActionRequired):
        asyncio.run(provider.resolve_card(row, '手机壳'))
    assert not provider.page.went_back


def test_slow_details_keep_decimal_quote_and_complete_title_without_using_other_buyers_sales(tmp_path):
    class Target:
        async def scroll_into_view_if_needed(self, **options):
            pass
        async def click(self, **options):
            pass

    class SlowPage(Page):
        def __init__(self):
            super().__init__(keyword_url('宠物餐具清洁'), '搜索结果')
            self.reads = 0
            self.went_back = False
        def locator(self, selector):
            return TextLocator(self.text) if selector == 'body' else Target()
        async def evaluate(self, script):
            if 'const expected=' in script:
                return True
            self.reads += 1
            if self.reads < 3:
                return {'title':'','price_text':'','sales_text':''}
            self.url='https://mobile.yangkeduo.com/goods.html?goods_id=773197918282&uin=account-example'
            return {'title':'宠物洗碗慕斯去油去污洗碗液餐具清洗剂', 'price_text':'¥6.9', 'sales_text':'已拼2630瓶'}
        async def go_back(self, **options):
            self.went_back=True
            self.url=keyword_url('宠物餐具清洁')

    provider=PDDKeywordProvider(tmp_path)
    provider.page=SlowPage()
    row=competitor(1,'已拼2630瓶',price='¥\n6\n.9')
    row.id=row.url=''
    row.title='宠物洗碗慕斯'
    row=asyncio.run(provider.resolve_card(row,'宠物餐具清洁'))
    assert provider.page.reads == 3 and provider.page.went_back
    assert row.title == '宠物洗碗慕斯去油去污洗碗液餐具清洗剂'
    assert row.price_min == row.price_max == 6.9
    assert row.sales_text == '已拼2630瓶'
    assert row.url == 'https://mobile.yangkeduo.com/goods.html?goods_id=773197918282'


def test_identical_truncated_titles_are_not_clicked_without_card_identity(tmp_path):
    class Matches:
        async def count(self):
            return 2
    class AmbiguousPage(Page):
        async def evaluate(self, script):
            return False
        def get_by_text(self, *args, **kwargs):
            return Matches()
    provider=PDDKeywordProvider(tmp_path)
    provider.page=AmbiguousPage(keyword_url('饮水机'),'搜索结果')
    row=competitor(1,'已拼300件');row.id=row.url=''
    assert asyncio.run(provider.resolve_card(row,'饮水机')).url == ''


def test_rebuilt_search_cards_are_relocated_by_identity_before_clicking(tmp_path):
    class RebuiltPage(Page):
        def __init__(self):
            super().__init__(keyword_url('饮水机'),'搜索结果')
            self.clicked=[]
        async def evaluate(self, script):
            if 'const expected=' in script:
                return False
            if 'const nativeCards=' in script:
                return {'url':self.url,'cards':[{'title':'测试竞品1','price_text':'¥12.8','text':'已拼300件','locator':'#新卡片'}]}
            return {'title':'测试竞品1完整标题','price_text':'¥12.8','sales_text':'已拼300件'}
        def locator(self, selector):
            if selector=='body':
                return TextLocator(self.text)
            page=self
            class Target:
                async def scroll_into_view_if_needed(self,**options):
                    pass
                async def click(self,**options):
                    page.clicked.append(selector)
                    page.url='https://mobile.yangkeduo.com/goods.html?goods_id=100001'
            return Target()
        async def go_back(self,**options):
            self.url=keyword_url('饮水机')
    provider=PDDKeywordProvider(tmp_path);provider.page=RebuiltPage()
    row=competitor(1,'已拼300件');row.id=row.url='';row.locator='#旧卡片'
    row=asyncio.run(provider.resolve_card(row,'饮水机'))
    assert provider.page.clicked==['#新卡片']
    assert row.url=='https://mobile.yangkeduo.com/goods.html?goods_id=100001'
    assert row.title=='测试竞品1完整标题'


def test_cancelling_detail_does_not_navigate_back_or_issue_more_requests(tmp_path):
    class CancelledPage(Page):
        def __init__(self):
            super().__init__('https://mobile.yangkeduo.com/goods.html?goods_id=100001','商品')
            self.went_back=False
        async def evaluate(self,script):
            raise asyncio.CancelledError()
        async def go_back(self,**options):self.went_back=True
    provider=PDDKeywordProvider(tmp_path);provider.page=CancelledPage()
    row=competitor(1,'本店已拼100万件');row.id=row.url=''
    with pytest.raises(asyncio.CancelledError):asyncio.run(provider.resolve_card(row,'测试'))
    assert not provider.page.went_back
