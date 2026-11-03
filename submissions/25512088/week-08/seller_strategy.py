"""Cost-protected pricing and evidence-based supply decisions."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
from statistics import median
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


def graded_demand(sku, product, intel, now):
    """Unknown arrival grade: separate scenarios and exclude unsupported warranties."""
    food=product.get('kind')=='food'
    probabilities={'A':.25,'B':.50,'C':.25} if food else {'A':.70,'B':.20,'C':.10}
    upper_cost={'A':.75,'B':.60,'C':.45}
    rows=intel.db.execute('SELECT * FROM trades WHERE sku=? AND ts>?',(sku,now-WINDOW)).fetchall()
    scenarios=[]; supported=0; demand=0
    for grade,probability in probabilities.items():
        comparable=[r for r in rows if r['grade']==grade and
                    ((food and r['hours'] is not None and r['hours']>0) or
                     (not food and r['hours'] is None and r['warranty']==0))]
        shelf=product.get('shelf_hours')
        if food and isinstance(shelf,(int,float)) and shelf>0:
            lo,hi={'A':(.8,1),'B':(.45,.8),'C':(0,.45)}[grade]
            comparable=[r for r in comparable if lo*shelf<=r['hours']<=hi*shelf]
        cost=math.ceil(product['list_price']*upper_cost[grade])
        price=int(median([r['price'] for r in comparable])) if comparable else None
        floor=cost_floor(cost,{'founder_percent':5,'acquisition_margin_percent':25})
        viable=len(comparable)>=3 and price>=floor
        scenarios.append(dict(grade=grade,sales=len(comparable),median=price,
                              upper_cost=cost,required_price=floor,viable=viable))
        if viable:supported+=probability;demand+=len(comparable)
    return dict(sku=sku,grade_scenarios=scenarios,supported_arrival_probability=round(supported,2),
                compatible_sales=demand,uncertainty='Other arrival grades may remain unsold.')


def restock_plan(inv,catalog,intel,now=None):
    now=time.time() if now is None else now
    if inv.get('round')!=3 or not inv.get('restock',{}).get('open'):
        return {'buy':[], 'reason':'supply_closed'}
    stock=inv.get('units',[]);slots=max(0,24-len(stock))  # Four bounded trial slots.
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
        evidence=graded_demand(sku,product,intel,now)
        if evidence['supported_arrival_probability']<.25:continue
        # Cash budget uses worst acquisition cost; demand comparisons use each grade.
        worst=math.ceil(product['list_price']*.75)
        score=evidence['compatible_sales']/(1+supply[sku])*evidence['supported_arrival_probability']
        score*=1+min(2,endorsed[sku])*.05
        evidence.update(score=round(score,4),competing_units=supply[sku])
        options.append((score,sku,worst,evidence))
    options.sort(key=lambda x:(x[0],x[1]),reverse=True)
    buys=[];selected=[]
    for score,sku,worst,evidence in options[:3]:
        proven=False
        db=intel.root/'referrals.sqlite3'
        if db.exists():
            import sqlite3
            with sqlite3.connect(str(db)) as conn:
                proven=conn.execute('SELECT 1 FROM sales WHERE sku=? AND settled IS NOT NULL AND refunded=0 LIMIT 1',(sku,)).fetchone() is not None
        qty=min((2 if proven else 1)-have[sku],slots,int(budget//worst))
        if qty>0:
            buys.append({'sku':sku,'qty':qty});slots-=qty;budget-=qty*worst
            selected.append(dict(evidence,qty=qty,own_sales_proven=proven))
    return {'buy':buys,'reason':'grade_condition_matched_trial' if buys else 'no_profitable_evidence',
            'candidates':len(options),'cash_reserve':reserve,'selected':selected,
            'ranked_candidates':[x[3] for x in options[:8]]}


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--state',default='.market');args=ap.parse_args()
    state=Path(args.state);intel=Intel(state)
    try:
        plan=restock_plan(read_json(state/'inventory.json',{}),read_json(state/'catalog.json',{}),intel)
        intel.decision('restock',plan)
        print(json.dumps(plan))
    finally:intel.close()

if __name__=='__main__':main()
