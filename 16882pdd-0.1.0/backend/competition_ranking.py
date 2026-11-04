"""竞品销量与报价解析，店铺总销量不冒充单品销量。"""
import re
from collections import Counter
from urllib.parse import parse_qs, urlsplit
from .card_values import prices_from_text, clean_title
from .competition_models import Competitor
from .models import timestamp


def pdd_goods_id(url: str) -> str:
    try:
        parts=urlsplit(url)
        if parts.scheme not in {"http","https"} or parts.username or parts.password:
            return ""
        if parts.hostname not in {"mobile.yangkeduo.com","yangkeduo.com","www.yangkeduo.com","www.pinduoduo.com"}:
            return ""
        query=parse_qs(parts.query)
        value=(query.get("goods_id") or query.get("goodsId") or [""])[0]
        return value if re.fullmatch(r"\d{5,30}",value) else ""
    except ValueError:
        return ""


def canonical_goods_url(goods_id: str) -> str:
    return f"https://mobile.yangkeduo.com/goods.html?goods_id={goods_id}" if re.fullmatch(r"\d{5,30}",goods_id) else ""


def parse_pdd_sales(text: str):
    # 页面可能把“本店”和“已拼”放在两个节点，换行不能改变销量口径。
    pattern=r"(?P<label>(?:(?:本店|全店|店铺)\s*)?(?:累计\s*)?(?:已拼|已抢|已售|售出|总售|热销|销量|拼单))\s*[:：]?\s*(?P<number>\d[\d,]*(?:\.\d+)?)\s*(?P<scale>万|千|亿)?\s*(?P<plus>\+)?\s*(?P<unit>件|笔|人|瓶|包|盒|套|台|个|支|袋|副|只|双|卷|罐|本|张|片|颗|条|组|桶)?"
    matches=list(re.finditer(pattern,text))
    product=[m for m in matches if not any(w in m['label'] for w in ("本店","全店","店铺"))]
    candidates=product or matches
    if not candidates:
        return "",None,"","unknown",False
    m=candidates[0]
    scope="store" if any(w in m['label'] for w in ("本店","全店","店铺")) else "product"
    value=float(m['number'].replace(",",""))*{None:1,"千":1000,"万":10000,"亿":100000000}[m['scale']]
    return re.sub(r"\s+","",m.group()),value,m['unit'] or "",scope,bool(m['plus'] or m['scale'])


def parse_competitor(raw: dict, position=0) -> Competitor:
    text=raw.get("text","")
    sales_text,value,unit,scope,approx=parse_pdd_sales(raw.get("sales_text") or text)
    values=prices_from_text(raw.get("price_text", ""))
    url=raw.get("url","")
    goods_id=pdd_goods_id(url)
    return Competitor(id=goods_id,title=clean_title(raw.get("title","") or text)[:1000] or "商品名称未读取",
                      image=raw.get("image", ""),url=canonical_goods_url(goods_id),
                      price_min=min(values) if values else None,price_max=max(values) if values else None,
                      price_text=raw.get("price_text", ""),sales_text=sales_text,sales_value=value,
                      sales_unit=unit,sales_scope=scope,sales_approximate=approx,observed_at=timestamp(),
                      page_position=position,locator=raw.get("locator", ""),raw_text=text[:5000])


def rank_competitors(candidates: list[Competitor], require_links=True, limit=5):
    reasons=Counter()
    valid=[]
    seen=set()
    for row in candidates:
        key=row.id or (row.title,row.price_min,row.sales_text)
        if key in seen:
            reasons["重复商品"]+=1;continue
        if row.sales_scope=="store":
            reasons["页面仅显示店铺销量"]+=1;continue
        if row.sales_value is None or row.sales_scope!="product" or not row.sales_unit:
            reasons["单品销量未读取"]+=1;continue
        if row.price_min is None or row.price_max is None:
            reasons["售价未读取"]+=1;continue
        if require_links and (not row.id or pdd_goods_id(row.url)!=row.id):
            reasons["商品链接未取得"]+=1;continue
        seen.add(key)
        valid.append(row)
    # 只比较相同销量单位；有件数优先件数，否则使用候选最多的单位。
    counts=Counter(row.sales_unit for row in valid)
    unit="件" if counts.get("件") else (counts.most_common(1)[0][0] if counts else "")
    comparable=[row for row in valid if row.sales_unit==unit]
    reasons["销量单位不一致"]+=len(valid)-len(comparable)
    comparable.sort(key=lambda row:(-row.sales_value,row.page_position))
    return comparable[:limit],{k:v for k,v in reasons.items() if v}
