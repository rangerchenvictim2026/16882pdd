"""只筛选有销量的正货价差候选，不把关键词相似当同款或净利润。"""
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP


def money(value):
    return Decimal(str(value))


def amount(value):
    return float(value.quantize(Decimal('0.01'),rounding=ROUND_HALF_UP))


def opportunities(job,minimum_gap=0):
    rows=[]
    excluded=Counter()
    for source_rank,item in enumerate(job['items'],1):
        source=item['source']
        for rank,match in enumerate(item['matches'],1):
            if source.get('price_min') is None or source.get('price_max') is None:
                excluded['货源报价未读取']+=1
                continue
            if match.get('price_min') is None or match.get('price_max') is None or match['price_min']<=0:
                excluded['竞品报价无效']+=1
                continue
            if match.get('sales_scope')!='product' or not match.get('sales_unit') or not match.get('sales_value'):
                excluded['当前单品销量不足']+=1
                continue
            lower=money(match['price_min'])-money(source['price_max'])
            upper=money(match['price_max'])-money(source['price_min'])
            if lower<=money(minimum_gap):
                excluded['保守货价差未超过门槛']+=1
                continue
            rows.append({'source':source,'competitor':match,'source_rank':source_rank,'competitor_rank':rank,
                'reference_cost':source['price_max'],'reference_price':match['price_min'],
                'gap_min':amount(lower),'gap_max':amount(upper),'keyword':item['keyword'],
                'verification':'关键词竞品，同款规格待核对','cost_basis':'1688最高展示价',
                'price_basis':'拼多多最低展示价','profit':None})
    rows.sort(key=lambda r:(-r['gap_min'],r['source_rank'],r['competitor_rank']))
    return {'job_id':job['id'],'status':job['status'],'rows':rows,'excluded':dict(excluded),
        'note':'货价差未扣运费、平台、推广和售后等费用；不是净利润。同款与规格需要人工核对，销量仅为当前网页展示值。'}
