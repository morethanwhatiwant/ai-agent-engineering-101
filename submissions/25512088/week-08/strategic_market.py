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


def rank_products(catalog, trades, shops, own_name='25512088', local_demand=None):
    local_demand=local_demand or {}
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
        scenarios=[];expected=0
        for grade,prob,rate in [('A',.7,.65),('B',.2,.50),('C',.1,.35)]:
            observations=[r['price'] for r in trades if r['sku']==sku and r.get('grade')==grade and r['warranty']==6 and r['honest']]
            typical=median(observations) if observations else None
            cost=product['list_price']*rate
            # Unknown grade-price evidence contributes a conservative unsold cost.
            net=typical*.85*.95-cost*1.09-2000 if typical else -cost
            expected+=prob*net
            scenarios.append(dict(grade=grade,samples=len(observations),median_6h=typical,mean_cost=cost,net_estimate=net))
        requests=local_demand.get(sku,0)
        score=(len(values)+min(requests,len(values))*.25)*price/((1+qty)*(1+count)**.5)
        rows.append(dict(expected_net=expected,grade_scenarios=scenarios,local_requests=requests,sku=sku,name=product['name'],sales=len(values),median_6h=price,
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
        demand={}
        buyer_path=Path(state)/'buyer.sqlite3'
        if buyer_path.exists():
            with sqlite3.connect('file:'+str(buyer_path.resolve())+'?mode=ro',uri=True) as db:
                for (context,) in db.execute('SELECT context FROM orders WHERE updated>?',(time.time()-21600,)):
                    for sku in set(json.loads(context).get('acceptable') or []):demand[sku]=demand.get(sku,0)+1
        top=rank_products(catalog,rows,shops.get('shops',[]),local_demand=demand)
        feedback=[]
        buyer_db=Path(state)/'buyer.sqlite3'
        if buyer_db.exists():
            with sqlite3.connect('file:'+str(buyer_db.resolve())+'?mode=ro',uri=True) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='rival_feedback'").fetchone():
                    feedback=[dict(order=r[0],sku=r[1],ledger=r[2],observed_at=r[3],stock=json.loads(r[4]),error=r[5]) for r in db.execute('SELECT * FROM rival_feedback ORDER BY observed_at DESC LIMIT 20')]
        result=dict(version=2,buyer_feedback=feedback,opens_at=opens_at,selected_at=time.time(),ledger_through=through,
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
