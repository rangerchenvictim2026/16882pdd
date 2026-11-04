"""1688网页搜索参数与用户提供的原生筛选组合，不推断标签的独立含义。"""
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

REFERENCE_URL = "https://s.1688.com/selloffer/offer_search.htm?keywords=%B3%E8%CE%EF%D3%C3%C6%B7&beginPage=1&sortType=va_sales360&descendOrder=true&freeShipping=2&filtOfferTags=1988226%2C98306%2C235906&priceEnd=15&tags=289794"
REFERENCE_TAGS = {"1988226", "98306", "235906"}
ALLOWED_QUERY = {"keywords", "beginPage", "sortType", "descendOrder", "freeShipping", "filtOfferTags", "priceStart", "priceEnd", "tags"}


def search_params(url: str) -> dict[str, str]:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "s.1688.com" or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.path != "/selloffer/offer_search.htm"):
        raise ValueError("请粘贴1688的HTTPS商品搜索链接")
    # 1688网页中的中文关键词使用GBK编码；同时兼容UTF-8链接。
    query = parse_qs(parsed.query, encoding="utf-8", errors="replace", keep_blank_values=True)
    if any("\ufffd" in value for values in query.values() for value in values):
        query = parse_qs(parsed.query, encoding="gbk", errors="strict", keep_blank_values=True)
    if any(len(values) != 1 for values in query.values()):
        raise ValueError("搜索链接包含重复参数，请复制当前筛选后的完整链接")
    return {key: values[0] for key, values in query.items() if key in ALLOWED_QUERY}


def has_reference_bundle(params: dict[str, str]) -> bool:
    # 只认用户提供的完整组合，不猜每个数字标签分别表示哪项服务。
    return (params.get("freeShipping") == "2"
            and set(params.get("filtOfferTags", "").split(",")) == REFERENCE_TAGS
            and params.get("tags") == "289794")


def build_search_url(filters, page: int = 1) -> str:
    # 只按当前表单构造请求，旧方案或历史链接不得覆盖本次筛选项。
    if filters.shipping == "free":
        params = search_params(REFERENCE_URL)
    else:
        params = {}
    params.update(keywords=filters.keyword, beginPage=str(page), sortType="va_sales360", descendOrder="true")
    if filters.shipping == "free":
        params["freeShipping"] = "2"
    else:
        params.pop("freeShipping", None)
    for name, value in (("priceEnd", filters.max_price), ("priceStart", filters.min_price)):
        if value is None:
            params.pop(name, None)
        else:
            params[name] = format(value, ".12g")
    return urlunsplit(("https", "s.1688.com", "/selloffer/offer_search.htm", urlencode(params, encoding="gbk", errors="strict"), ""))


def import_search_url(url: str) -> dict:
    params = search_params(url)
    keyword = params.get("keywords", "").strip()
    if not keyword:
        raise ValueError("搜索链接缺少商品关键词")
    def amount(name):
        value = params.get(name)
        try:
            return float(value) if value else None
        except ValueError:
            raise ValueError("搜索链接中的价格格式不正确")
    return {"keyword": keyword, "selection_mode": "platform", "native_search_url": url,
            "shipping": "free" if params.get("freeShipping") == "2" else "all",
            "min_price": amount("priceStart"), "max_price": amount("priceEnd")}
