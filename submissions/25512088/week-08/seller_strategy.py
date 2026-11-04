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


def warranty_hours(unit):
    return 6 if unit.get('kind')=='goods' else 0


def protected_floor(unit, policy):
    # Nocha12's cost + storage + profit / (1-breakage) structure, adapted
    # to our existing benefits. 15% is an explicit prior from the reference strategy, not measured risk.
    floor=cost_floor(unit['cost'],policy)
    if not warranty_hours(unit):return floor
    cost=unit['cost'];reserve=math.ceil(cost*.09)
    acquisition=policy.get('acquisition_margin_percent',25)/100
    founder=policy.get('founder_percent',5)/100
    return max(floor,math.ceil((cost+reserve)/(.85*(1-founder))),
               math.ceil((cost*(1-acquisition)+reserve)/(.85-acquisition)))


def negotiate(unit, current, offered, catalog, program, token='', ref=''):
    floor=protected_floor(unit,program.p)
    minimum=program.quote(floor,unit['cost'],token=token,ref=ref)
    if offered>=minimum['price']:
        lo,hi=floor,current['base']
        while lo<hi:
            mid=(lo+hi+1)//2
            if program.quote(mid,unit['cost'],token=token,ref=ref)['price']<=offered:lo=mid
            else:hi=mid-1
        q=program.quote(lo,unit['cost'],token=token,ref=ref)
    else:
        q=program.quote(max(floor,(current['base']+floor)//2),unit['cost'],token=token,ref=ref)
    q['warranty_hours']=warranty_hours(unit)
    if q['price']*(.85 if q['warranty_hours'] else 1)-q['reward'] < unit['cost']+math.ceil(unit['cost']*.09):
        return current
    return q if q['price']<current['price'] else current


def sale_quote(unit, catalog, program, intel=None, token='', ref=''):
    base=public_price(unit,catalog)  # Also validates expiry and grade.
    hours=(unit['expires_at']-time.time())/3600 if unit.get('expires_at') else None
    comp=intel.comparable(unit['sku'],unit['grade'],hours,warranty_hours(unit)) if intel else {'count':0,'median':None}
    if comp['median']:
        base=math.ceil(comp['median']*.99)
    floor=protected_floor(unit,program.p)
    q=program.quote(max(base,floor),unit['cost'],token=token,ref=ref)
    q['warranty_hours']=warranty_hours(unit)
    if q['price']*(.85 if q['warranty_hours'] else 1)-q['reward'] < unit['cost']+math.ceil(unit['cost']*.09):
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
                     (not food and r['hours'] is None and r['warranty']==(6 if product.get('kind')=='goods' else 0)))]
        shelf=product.get('shelf_hours')
        if food and isinstance(shelf,(int,float)) and shelf>0:
            lo,hi={'A':(.8,1),'B':(.45,.8),'C':(0,.45)}[grade]
            comparable=[r for r in comparable if lo*shelf<=r['hours']<=hi*shelf]
        cost=math.ceil(product['list_price']*upper_cost[grade])
        price=int(median([r['price'] for r in comparable])) if comparable else None
        floor=protected_floor(dict(cost=cost,kind=product.get('kind')),{'founder_percent':5,'acquisition_margin_percent':25})
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
    exposure=0
    db=intel.root/'referrals.sqlite3'
    if db.exists():
        import sqlite3
        with sqlite3.connect(str(db)) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='warranties'").fetchone():
                exposure=conn.execute('SELECT COALESCE(SUM(w.amount),0) FROM warranties w JOIN sales s ON s.task=w.task WHERE s.refunded=0 AND (s.settled IS NULL OR s.settled>?)',(now-21600,)).fetchone()[0]
    reserve+=exposure
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
        proven=False;sold_recent=0
        db=intel.root/'referrals.sqlite3'
        if db.exists():
            import sqlite3
            with sqlite3.connect(str(db)) as conn:
                sold_recent=conn.execute('SELECT COUNT(*) FROM sales WHERE sku=? AND settled>? AND refunded=0',(sku,now-3600)).fetchone()[0]
                proven=sold_recent>0
        target=min(2,max(1,math.ceil(sold_recent/3)))  # ~20-minute supply cadence; retain hard cap.
        qty=min(target-have[sku],slots,int(budget//worst))
        if qty>0:
            buys.append({'sku':sku,'qty':qty});slots-=qty;budget-=qty*worst
            selected.append(dict(evidence,qty=qty,own_sales_proven=proven,sales_last_hour=sold_recent,target_units=target))
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
