"""Cost-protected pricing and evidence-based supply decisions."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import time
from market_intel import Intel, read_json, WINDOW
from stock_pricing import public_price


def cost_floor(cost, policy):
    # Net after benefits retains 8% of cost, plus 1% storage allowance.
    reserve=math.ceil(cost*.09)
    founder=policy.get('founder_percent',5)/100
    acquisition=policy.get('acquisition_margin_percent',25)/100
    return max(math.ceil((cost+reserve)/(1-founder)), cost+math.ceil(reserve/(1-acquisition)))


def sale_quote(unit, catalog, program, intel=None, token='', ref=''):
    base=public_price(unit,catalog)  # Also validates expiry and grade.
    hours=(unit['expires_at']-time.time())/3600 if unit.get('expires_at') else None
    comp=intel.comparable(unit['sku'],unit['grade'],hours,0) if intel else {'count':0,'median':None}
    if comp['median']:
        base=math.ceil(comp['median']*.99)
    floor=cost_floor(unit['cost'],program.p)
    q=program.quote(max(base,floor),unit['cost'],token=token,ref=ref)
    if q['price']-q['reward'] < unit['cost']+math.ceil(unit['cost']*.09):
        raise ValueError('Net margin guard rejected quote')
    if intel:
        intel.decision('seller_quote',{'sku':unit['sku'],'cost':unit['cost'],'price':q['price'],
                       'reward':q['reward'],'floor':floor,'comparable':comp,
                       'source':'settlement_median' if comp['median'] else 'grade_formula_cost_protected'})
    return q


def restock_plan(inv,catalog,intel,now=None):
    now=time.time() if now is None else now
    if inv.get('round')!=3 or not inv.get('restock',{}).get('open'):
        return {'buy':[], 'reason':'supply_closed'}
    stock=inv.get('units',[]);slots=max(0,20-len(stock))
    reserve=max(500000,inv.get('start',5000000)*.5)
    budget=max(0,min(inv.get('balance',0)-reserve,inv.get('balance',0)*.05))
    if not slots or not budget:return {'buy':[],'reason':'inventory_or_cash_limit'}
    if now-intel.get('ledger_through',0)>900:
        return {'buy':[],'reason':'market_evidence_stale'}
    have=Counter(u['sku'] for u in stock)
    volume={r['sku']:r['n'] for r in intel.db.execute('SELECT sku,COUNT(*) n FROM trades WHERE ts>? GROUP BY sku',(now-WINDOW,))}
    registry=(intel.source('shops',{}) or {}).get('shops',[])
    supply=Counter()
    for shop in registry:
        for sku,qty in shop.get('stock',{}).items():
            if type(qty) is int and qty>0:supply[sku]+=qty
    demand=intel.source('demand',[]) or []
    categories={d.get('category') for d in demand if isinstance(d,dict)} if isinstance(demand,list) else set()
    # Forum refs are only evidence when they point to observed settled trades.
    posts=intel.source('forum',[]) or []
    if isinstance(posts,dict):posts=posts.get('posts',[])
    endorsed=Counter()
    for post in posts:
        if post.get('likes',0)<=post.get('dislikes',0):continue
        for ref in post.get('refs',[]):
            seq=ref.get('seq') if isinstance(ref,dict) else ref
            if type(seq) is not int:continue
            row=intel.db.execute('SELECT sku FROM trades WHERE seq=? AND ts>?',(seq,now-WINDOW)).fetchone()
            if row:endorsed[row[0]]+=1
    options=[]
    for sku,n in volume.items():
        product=catalog.get(sku)
        if not product or have[sku]>=2:continue
        if categories and product.get('category') not in categories:continue
        prices=[r[0] for r in intel.db.execute('SELECT price FROM trades WHERE sku=? AND ts>?',(sku,now-WINDOW))]
        if len(prices)<3:continue
        # Supply grade is unknown; require observed prices above worst-case cost + reserve.
        worst=math.ceil(product['list_price']*.75)
        safe=cost_floor(worst,{'founder_percent':5,'acquisition_margin_percent':25})
        viable=sum(v>=safe for v in prices)/len(prices)
        if viable<.5:continue
        score=n/(1+supply[sku]) * viable * (1+min(2,endorsed[sku])*.05)
        options.append((score,sku,worst))
    options.sort(reverse=True)
    buys=[]
    for score,sku,worst in options[:3]:
        qty=min(2-have[sku],slots,int(budget//worst))
        if qty>0:
            buys.append({'sku':sku,'qty':qty});slots-=qty;budget-=qty*worst
    return {'buy':buys,'reason':'observed_demand_and_conservative_cost' if buys else 'no_profitable_evidence',
            'candidates':len(options),'cash_reserve':reserve}


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--state',default='.market');args=ap.parse_args()
    state=Path(args.state);intel=Intel(state)
    try:
        plan=restock_plan(read_json(state/'inventory.json',{}),read_json(state/'catalog.json',{}),intel)
        intel.decision('restock',plan)
        print(json.dumps(plan))
    finally:intel.close()

if __name__=='__main__':main()
