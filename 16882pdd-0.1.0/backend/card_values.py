"""解析页面可见的分段金额与销量原文，不从搜索参数补造商品数值。"""
import re


def prices_from_text(text: str) -> list[float]:
    # ¥、整数、小数可能分成多个元素并换行；不得把36\n.8截成36。
    number = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\s*\.\s*\d{1,2})?"
    pattern = rf"[¥￥]\s*({number})(?![\d.,])(?:\s*[-~～至—]\s*[¥￥]?\s*({number})(?![\d.,]))?"
    values = []
    for match in re.finditer(pattern, text):
        for value in match.groups():
            if value is not None:
                values.append(float(re.sub(r"[\s,]", "", value)))
    return values


def sales_from_text(text: str, explicit: str = "") -> str:
    number = r"\d[\d,]*(?:\.\d+)?\s*(?:万|千|亿)?\s*\+?"
    label = r"(?:(?:近?\s*30\s*(?:天|日)|月|年|累计)\s*)?(?:销量|销售量|销售|已售|售出|成交量|成交|售卖)"
    # 保留原文周期与单位：月销、年销、成交人数都不冒充近30天件数。
    for source in (explicit, text):
        if not source:
            continue
        for pattern in (rf"(?:{label}|月销|年销)\s*[:：]?\s*{number}\s*(?:件|笔|人)?", rf"(?m)^\s*{number}\s*(?:件|笔|人)\s*(?:已售|成交|销量)?\s*$"):
            match = re.search(pattern, source)
            if match:
                value = re.sub(r"\s+", "", match[0])
                if not re.search(r"成交额|金额|回头率|起批|起订", value):
                    return value
    return ""


def clean_title(title: str) -> str:
    return next((line.strip() for line in title.splitlines() if line.strip()), "")
