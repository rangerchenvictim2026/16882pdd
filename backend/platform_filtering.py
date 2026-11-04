"""原生筛选依据绑定到实际搜索页、商品卡片与本次采集，不补造天数或销量。"""
import json
from datetime import datetime, timedelta

from .models import CHINA, Product, SearchFilters, now
from .native_search import has_reference_bundle, search_params


def native_context(product: Product, filters: SearchFilters) -> dict:
    if not product.native_search_url or product.native_position is None:
        return {}
    try:
        params = search_params(product.native_search_url)
        if params.get("keywords") != filters.keyword:
            return {}
        for evidence in product.evidence:
            if (evidence.field != "native_search" or evidence.url != product.native_search_url
                    or evidence.observed_at != product.observed_at):
                continue
            proof = json.loads(evidence.text)
            if not isinstance(proof, dict):
                continue
            if (proof.get("product_id") == product.id and proof.get("card_url") == product.url
                    and proof.get("position") == product.native_position):
                return params
    except (ValueError, TypeError, UnicodeError):
        pass
    return {}


def evaluate_platform(product: Product, filters: SearchFilters, at: datetime | None = None) -> list[str]:
    at = at or now()
    params = native_context(product, filters)
    bundle = has_reference_bundle(params)
    sources = {product.url, product.native_search_url} if params else {product.url}
    fields = {e.field for e in product.evidence if e.url in sources and e.observed_at == product.observed_at and e.text.strip()}
    reasons = []
    if not product.title.strip() or product.title == f"1688商品 {product.id}":
        reasons.append("商品卡片标题未读取，需重新采集")
    date_ok = False
    if product.published_at and "published_at" in fields:
        try:
            published = datetime.fromisoformat(product.published_at)
            if published.tzinfo is None:
                published = published.replace(tzinfo=CHINA)
            date_ok = timedelta(0) <= at - published <= timedelta(days=15)
        except (ValueError, TypeError):
            pass
    if not bundle and not date_ok and "platform_new" not in fields:
        reasons.append("未确认平台新品筛选")
    if product.dropship is False and "dropship" in fields:
        reasons.append("页面明确不支持一件代发")
    elif not bundle and not (product.dropship is True and "dropship" in fields):
        reasons.append("未确认平台一件代发筛选")
    if filters.shipping != "all":
        native_free = bundle and filters.shipping == "free"
        quoted = (product.free_shipping is not None and "free_shipping" in fields
                  and product.shipping_province == filters.province and product.shipping_city == filters.city
                  and bool(filters.province) and bool(filters.city))
        if not native_free and not quoted:
            reasons.append("未确认对应运费筛选")
        elif not native_free and product.free_shipping != (filters.shipping == "free"):
            reasons.append("不符合包邮筛选")
    if filters.min_price is not None or filters.max_price is not None:
        # 搜索参数仅用于请求筛选，不能替代实际商品报价的逐条校验。
        if product.price_min is None or product.price_max is None or "price" not in fields:
            reasons.append("商品实际价格未读取")
        elif ((filters.min_price is not None and product.price_min < filters.min_price)
              or (filters.max_price is not None and product.price_max > filters.max_price)):
            reasons.append("商品实际价格超出筛选区间")
    # 原生模式保留未知周期，绝不把va_sales360解释为近30天销量。
    return reasons
