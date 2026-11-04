"""Shared strategy snapshots. Public observed demand is a proxy, not all market orders."""
import json
import math
import os
import sqlite3
import time
import httpx
from network_reads import retry_read
from pathlib import Path
from statistics import median
from market_intel import Intel, read_json


def rank_products(catalog, trades, shops, own_name='25512088'):
    prices = {}; supply = {}; vendors = {}
    for row in trades:
        if row['warranty'] != 6 or not row['honest']:
            continue
        sku=row['sku']
        if catalog.get(sku,{}).get('kind') != 'goods':continue
        prices.setdefault(sku,[]).append(row['price'])
    for shop in shops:
        if shop.get('name') == own_name:continue
        for sku,qty in shop.get('stock',{}).items():
            if isinstance(qty,(int,float)) and qty>0:
                supply[sku]=supply.get(sku,0)+qty
                vendors.setdefault(sku,set()).add(shop.get('url',shop.get('name')))
    if not prices:return []
    medians=sorted(median(v) for v in prices.values())
    # High-price means the upper quartile of observed non-food 6h trades.
    threshold=medians[int((len(medians)-1)*.75)]
    rows=[]
    for sku,values in prices.items():
        product=catalog[sku];price=median(values)
        if len(values)<3 or price<threshold:continue
        qty=supply.get(sku,0);count=len(vendors.get(sku,set()))
        score=len(values)*price/((1+qty)*(1+count)**.5)
        rows.append(dict(sku=sku,name=product['name'],sales=len(values),median_6h=price,
                         competitor_units=qty,competitor_shops=count,score=score,
                         upper_cost=math.ceil(product['list_price']*.75)))
    return sorted(rows,key=lambda r:(-r['score'],r['sku']))[:10]


def snapshot(state,catalog,opens_at,market):
    if time.time()<opens_at-180:
        return None
    intel=Intel(state)
    try:
        # Fetch a fresh tail independently of historical backfill. Do not move its cursor.
        with httpx.Client(timeout=15) as client:
            def get(path):
                response=client.get(market+path);response.raise_for_status();return response.json()
            shops=retry_read(lambda:get('/api/shops'))
            recent=retry_read(lambda:get('/api/ledger?after=-1000'))
        intel.save_source('shops',shops)
        intel.ingest(recent)
        if recent:
            intel.put('ledger_through',max(intel.get('ledger_through',0),max(r['ts'] for r in recent)))
        through=intel.get('ledger_through',0)
        if not shops or time.time()-through>900:
            return None  # Do not substitute an old ranking for the scheduled snapshot.
        rows=[dict(r) for r in intel.db.execute('SELECT * FROM trades WHERE ts>?',(time.time()-21600,))]
        top=rank_products(catalog,rows,shops.get('shops',[]))
        result=dict(opens_at=opens_at,selected_at=time.time(),ledger_through=through,
                    late=time.time()>opens_at-150,top10=top,
                    demand_basis='observed honest settlements with 6h warranty in last 6h; incomplete sample')
        path=Path(state)/'strategy.json';tmp=path.with_suffix('.tmp')
        tmp.write_text(json.dumps(result,ensure_ascii=False));os.replace(tmp,path)
        intel.decision('strategy_snapshot',result)
        return result
    finally:intel.close()


def owned_targets(state, now=None):
    """Only actually held strategic non-food; stale inventory closes the buyer gate."""
    now=time.time() if now is None else now
    root=Path(state);path=root/'inventory.json'
    if not path.exists() or now-path.stat().st_mtime>120:return set()
    inv=read_json(path,{})
    if inv.get('round')!=3:return set()
    targets=set()
    dbpath=root/'procurement.sqlite3'
    if dbpath.exists():
        with sqlite3.connect('file:'+str(dbpath.resolve())+'?mode=ro',uri=True) as db:
            targets={r[0] for r in db.execute("SELECT DISTINCT sku FROM purchases WHERE status='paid'")}
    # A completed seller receipt already promises that unit to somebody else.
    reserved=set();ref=root/'referrals.sqlite3'
    if ref.exists():
        with sqlite3.connect('file:'+str(ref.resolve())+'?mode=ro',uri=True) as db:
            reserved={r[0] for r in db.execute('SELECT unit FROM sales')}
    return {u['sku'] for u in inv.get('units',[]) if u.get('kind')=='goods'
            and u.get('grade') in ('A','B','C') and u['sku'] in targets and u['unit'] not in reserved}


def restrict_order(order,state):
    result=dict(order)
    result['acceptable']=sorted(set(order.get('acceptable',[])) & owned_targets(state))
    if order.get('kind')=='food':result['acceptable']=[]
    return result
