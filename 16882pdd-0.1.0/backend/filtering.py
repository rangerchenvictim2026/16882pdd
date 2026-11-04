"""仅对有明确证据的字段作出判断，未知值不等于零或否。"""
from datetime import datetime, timedelta

from .models import CHINA, Product, SearchFilters, now


def evaluate(product: Product, filters: SearchFilters, at: datetime | None = None) -> list[str]:
    if filters.selection_mode == "platform":
        from .platform_filtering import evaluate_platform
        return evaluate_platform(product, filters, at)
    at = at or now()
    reasons: list[str] = []
    evidence_fields = {e.field for e in product.evidence if e.text.strip() and e.url == product.url and e.observed_at == product.observed_at}
    date_ok = False
    if product.published_at and "published_at" in evidence_fields:
        try:
            published = datetime.fromisoformat(product.published_at)
            published = published.replace(tzinfo=CHINA) if published.tzinfo is None else published
            date_ok = timedelta(0) <= at - published <= timedelta(days=filters.new_days)
        except ValueError:
            pass
    elif product.native_new_days is not None and "native_new_days" in evidence_fields:
        # 原生上新筛选只在本次采集当日内作为依据，不能永久沿用。
        try:
            observed = datetime.fromisoformat(product.observed_at)
            date_ok = 0 < product.native_new_days <= filters.new_days and timedelta(0) <= at - observed <= timedelta(hours=1)
        except (ValueError, TypeError):
            pass
    if not date_ok:
        reasons.append("无法确认近15天上新" if not product.published_at else "不符合近15天上新")
    if product.dropship is not True or "dropship" not in evidence_fields:
        reasons.append("无法确认支持一件代发")
    if product.sales_30d is None or product.sales_unit not in ("件", "笔") or "sales_30d" not in evidence_fields:
        reasons.append("缺少明确的近30天销量")
    if filters.shipping != "all":
        if product.free_shipping is None or "free_shipping" not in evidence_fields:
            reasons.append("无法确认包邮条件")
        elif product.shipping_province != filters.province or product.shipping_city != filters.city:
            reasons.append("包邮适用地区未核实")
        elif product.free_shipping != (filters.shipping == "free"):
            reasons.append("不符合包邮筛选")
    if filters.min_price is not None or filters.max_price is not None:
        # 要求整段价格落在筛选范围，避免最低价规格掩盖其他规格。
        if product.price_min is None or product.price_max is None or "price" not in evidence_fields:
            reasons.append("规格价格范围未核实")
        elif ((filters.min_price is not None and product.price_min < filters.min_price)
              or (filters.max_price is not None and product.price_max > filters.max_price)):
            reasons.append("不符合价格区间")
    return reasons


def sorted_products(products: list[dict], mode="strict") -> list[dict]:
    if mode == "platform":
        return sorted(products, key=lambda p: (p.get("native_position") is None, p.get("native_position") or 0))
    # 不将件与笔混排成一个排行榜，也不把带加号的下限当精确值。
    return sorted(products, key=lambda p: (p.get("sales_unit") or "", -(p.get("sales_30d") or 0), p["id"]))
