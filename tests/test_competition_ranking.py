"""核验竞品的单品销量口径、报价、去重及前五名截取。"""
import pytest

from backend.competition_ranking import parse_competitor, parse_pdd_sales, rank_competitors


@pytest.mark.parametrize("text,expected", [
    ("已拼1.2万+件", ("已拼1.2万+件", 12000, "件", "product", True)),
    ("本店\n已拼500万+件", ("本店已拼500万+件", 5000000, "件", "store", True)),
    ("全店\n累计\n已售3,500件", ("全店累计已售3,500件", 3500, "件", "store", False)),
    ("已拼128人", ("已拼128人", 128, "人", "product", False)),
    ("已抢9.2万+件", ("已抢9.2万+件", 92000, "件", "product", True)),
    ("总售4.1万+件", ("总售4.1万+件", 41000, "件", "product", True)),
    ("全店总售500万+件", ("全店总售500万+件", 5000000, "件", "store", True)),
    ("热销1.4万+件", ("热销1.4万+件", 14000, "件", "product", True)),
    ("已拼2630瓶", ("已拼2630瓶", 2630, "瓶", "product", False)),
    ("已有3万条评价", ("", None, "", "unknown", False)),
])
def test_visible_sales_scope_is_preserved(text, expected):
    assert parse_pdd_sales(text) == expected


def competitor(index, sales, price="¥\n12\n.80", url=None):
    return parse_competitor({
        "title": f"测试竞品{index}", "price_text": price, "sales_text": sales,
        "url": url or f"https://mobile.yangkeduo.com/goods.html?goods_id={100000 + index}",
    }, index)


def test_top_five_are_sorted_deduplicated_and_keep_the_visible_prices():
    rows = [competitor(i, f"已拼{i * 100}件") for i in range(1, 8)]
    matches, reasons = rank_competitors(rows + [rows[0]])
    assert [row.sales_value for row in matches] == [700, 600, 500, 400, 300]
    assert all(row.price_min == row.price_max == 12.8 for row in matches)
    assert reasons == {"重复商品": 1}


def test_store_sales_missing_prices_and_unknown_links_cannot_enter_ranking():
    matches, reasons = rank_competitors([
        competitor(1, "本店\n已拼500万+件"),
        competitor(2, "已拼100件", price=""),
        competitor(3, "已拼200件", url="https://example.com/?goods_id=100003"),
        competitor(4, "已拼300件"),
    ])
    assert [row.id for row in matches] == ["100004"]
    assert reasons == {"页面仅显示店铺销量": 1, "售价未读取": 1, "商品链接未取得": 1}


def test_units_are_not_mixed_and_a_missing_period_is_not_invented():
    matches, reasons = rank_competitors([
        competitor(1, "已拼100件"), competitor(2, "已拼200万人"),
    ])
    assert [row.id for row in matches] == ["100001"]
    assert matches[0].sales_text == "已拼100件"
    assert reasons == {"销量单位不一致": 1}


def test_a_store_only_observation_does_not_hide_a_valid_product_observation():
    matches, _ = rank_competitors([
        competitor(1, "本店已拼500万件"), competitor(1, "已拼300件"),
    ])
    assert len(matches) == 1 and matches[0].sales_value == 300


def test_public_goods_links_keep_ids_and_discard_account_or_tracking_parameters():
    url = "https://mobile.yangkeduo.com/goods.html?goods_id=100001&source=search&uin=account-example"
    row = competitor(1, "已拼100件", url=url)
    assert row.url == "https://mobile.yangkeduo.com/goods.html?goods_id=100001"
    row.id = "999999"
    matches, reasons = rank_competitors([row])
    assert not matches and reasons == {"商品链接未取得": 1}
