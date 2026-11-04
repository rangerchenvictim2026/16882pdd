"""使用原始快照修复已存结果，不伪造新采集时间，不访问平台。"""
from pathlib import Path

from .filtering import evaluate
from .models import Product, SearchFilters, timestamp
from .providers import parse_search_card
from .store import dump

REPAIR_KEY = "card_price_sales_repair_v2"


def repair_existing_records(store):
    if store.get_kv(REPAIR_KEY):
        return
    tasks = [t for t in store.tasks() if t["filters"].get("selection_mode") == "platform"]
    if tasks:
        folder=store.path.parent / "backups"
        folder.mkdir(exist_ok=True)
        (folder / ("价格销量修复前-"+timestamp().replace(":","-")+".json")).write_text(dump(store.backup()),encoding="utf-8")
    changed = 0
    for task in tasks:
        filters=SearchFilters.model_validate(task["filters"])
        for item in store.task_items(task["id"],False):
            old=Product.model_validate(item["product"])
            card=next((e for e in old.evidence if e.field=="search_card"),None)
            if card and old.native_search_url and old.native_position:
                raw={"id":old.id,"title":old.title,"text":card.text,"image":old.image,
                     "supplier_name":old.supplier_name,"supplier_url":old.supplier_url,"supplier_id":old.supplier_id,
                     "sales_text":"\n".join(e.text for e in old.evidence if e.field=="platform_sales"),
                     "price_text":"\n".join(e.text for e in old.evidence if e.field=="price"),
                     "skus":[s.model_dump() for s in old.skus]}
                fixed=parse_search_card(raw,old.native_search_url,old.native_position,old.observed_at)
                if "/tfs/" in fixed.image or "-tps-" in fixed.image:
                    fixed.image=""
                store.put_product(fixed,task["id"],evaluate(fixed,filters))
                changed+=1
            else:
                store.put_product(old,task["id"],evaluate(old,filters))
        from collections import Counter
        items=store.task_items(task["id"],False)
        task.update(scanned=len(items),qualified=sum(i["qualified"] for i in items),
                    excluded=sum(not i["qualified"] for i in items),
                    reasons=dict(Counter(reason for i in items for reason in i["reasons"])))
        task["message"]="已依据原始卡片重新核对实际价格与销量；超价及价格未读取商品已排除，原始快照保留"
        store.put_task(task)
    store.put_kv(REPAIR_KEY,{"time":timestamp(),"repaired":changed})
