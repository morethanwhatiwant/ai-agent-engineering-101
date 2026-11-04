"""Explicit one-off goods purchase authorization. Durable spend; never retry unknown POSTs."""
import argparse,fcntl,importlib.util,json,math,os,sqlite3,time
from pathlib import Path
from network_reads import retry_read
from strategic_market import snapshot
from market_intel import read_json

ROOT=Path(__file__).resolve().parent

def settings():return json.loads((ROOT/'procurement_policy.json').read_text())

def reserve_cash(inv,exposure=0):
    return max(settings()['cash_floor'],inv.get('bankrupt_line',500000))+exposure

def choose(policy,catalog,spent,reserved,balance,exposure,turn,blocked=()):
    remaining=min(policy['budget']-spent-reserved,balance-policy['cash_floor']-exposure)
    priorities=policy['priority'];ordered=priorities[turn%len(priorities):]+priorities[:turn%len(priorities)]
    for sku in ordered:
        product=catalog.get(sku,{})
        if sku in blocked or product.get('kind')!='goods':continue
        upper=math.ceil(product['list_price']*.75)
        if upper<=remaining:return sku,upper
    return None

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--state',default='.market');args=ap.parse_args()
    state=Path(args.state).resolve();policy=settings();state.mkdir(exist_ok=True)
    lock=(state/'procurement.lock').open('a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('Another campaign worker owns the lock')
    db=sqlite3.connect(state/'procurement.sqlite3');db.row_factory=sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS purchases(id INTEGER PRIMARY KEY,campaign TEXT,sku TEXT,upper_cost INTEGER,cost INTEGER,status TEXT,created REAL,detail TEXT)');db.commit()
    spec=importlib.util.spec_from_file_location('kit',ROOT.parents[2]/'scripts/market_kit.py');kit=importlib.util.module_from_spec(spec);spec.loader.exec_module(kit)
    assert (ROOT/'shop_key.pem').exists()
    key=kit.load_key(ROOT/'shop_key.pem');market=kit.Market('https://market.ourolabs.ai',os.environ.get('MARKET_OWNER'))
    def log(**data):print(json.dumps(dict(ts=time.time(),**data),ensure_ascii=False),flush=True)
    def get_board():
        response=market.c.get(market.base+'/api/shops');response.raise_for_status();return response
    board=retry_read(get_board, attempts=None, report=log)
    me=next(s for s in board.json()['shops'] if s['name']=='25512088')
    market.join('shop','25512088',key,me['url'])
    catalog=json.loads((state/'catalog.json').read_text());blocked=set();window=None
    def log(**data):print(json.dumps(dict(ts=time.time(),**data),ensure_ascii=False),flush=True)
    log(event='started',campaign=policy['campaign'],budget=policy['budget'])
    while True:
        rows=db.execute('SELECT * FROM purchases WHERE campaign=?',(policy['campaign'],)).fetchall()
        spent=sum(r['cost'] or 0 for r in rows if r['status']=='paid');pending=sum(r['upper_cost'] for r in rows if r['status'] in ('sent','uncertain'))
        if pending:log(event='uncertain_stop',spent=spent,reserved=pending);break
        def get_inventory():
            response=market.call('GET','/api/inventory','shop');response.raise_for_status();return response.json()
        inv=retry_read(get_inventory, attempts=None, report=log)
        if inv.get('round')!=3:log(event='wrong_round');break
        exposure=0
        with sqlite3.connect(state/'referrals.sqlite3') as referrals:
            if referrals.execute("SELECT 1 FROM sqlite_master WHERE name='warranties'").fetchone():
                exposure=referrals.execute('SELECT COALESCE(SUM(w.amount),0) FROM warranties w JOIN sales s ON s.task=w.task WHERE s.refunded=0 AND (s.settled IS NULL OR s.settled>?)',(time.time()-21600,)).fetchone()[0]
        restock=inv.get('restock',{})
        if window!=restock.get('opens_at'):window=restock.get('opens_at');blocked.clear()
        opening=restock.get('opens_at')
        strategy=read_json(state/'strategy.json',{})
        if not isinstance(opening,(int,float)):
            log(event='missing_supply_window');time.sleep(20);continue
        if strategy.get('opens_at')!=opening and time.time()>=opening-180:
            try:strategy=snapshot(state,catalog,opening,market.base) or {}
            except Exception as exc:
                log(event='selection_retry',error=type(exc).__name__);time.sleep(20);continue
        if strategy.get('opens_at')!=opening:
            log(event='waiting_selection',select_at=opening-180,spent=spent)
            time.sleep(min(20,max(1,opening-180-time.time())));continue
        ranked=strategy.get('top10',[])
        if not ranked:
            log(event='no_qualified_top10');time.sleep(20);continue
        # Diminishing allocation: spread budget as own holdings grow, no unit cap.
        counts={sku:sum(u['sku']==sku for u in inv['units']) for sku in catalog}
        ranked=sorted(ranked,key=lambda r:-r['score']/(1+counts.get(r['sku'],0)))
        current=dict(policy,priority=[r['sku'] for r in ranked])
        picked=choose(current,catalog,spent,pending,inv['balance'],exposure,0,blocked)
        if not picked:
            if blocked:time.sleep(10);continue
            log(event='budget_wait',spent=spent,remaining=policy['budget']-spent)
            time.sleep(60);continue
        if not restock.get('open'):
            log(event='waiting_supply',opens_at=restock.get('opens_at'),spent=spent)
            time.sleep(min(20,max(2,restock.get('opens_at',time.time()+20)-time.time())));continue
        sku,upper=picked
        with db:
            cursor=db.execute('INSERT INTO purchases(campaign,sku,upper_cost,status,created) VALUES(?,?,?,?,?)',(policy['campaign'],sku,upper,'sent',time.time()));rid=cursor.lastrowid
        try:
            result=market.call('POST','/api/supply','shop',json={'sku':sku,'qty':1})
        except Exception as exc:
            with db:db.execute("UPDATE purchases SET status='uncertain',detail=? WHERE id=?",(type(exc).__name__,rid))
            log(event='uncertain_stop',sku=sku);break
        if result.status_code==200:
            body=result.json();cost=body.get('total')
            if type(cost) is not int or not 0<cost<=upper:
                log(event='unexpected_cost_stop',sku=sku);break
            with db:db.execute("UPDATE purchases SET status='paid',cost=?,detail=? WHERE id=?",(cost,json.dumps({'balance':body.get('balance')}),rid))
            log(event='purchased',sku=sku,qty=1,cost=cost,spent=spent+cost,balance=body.get('balance'))
        elif result.status_code>=500:
            with db:db.execute("UPDATE purchases SET status='uncertain',detail=? WHERE id=?",(str(result.status_code),rid))
            log(event='uncertain_stop',sku=sku,status=result.status_code);break
        else:
            with db:db.execute("UPDATE purchases SET status='refused',detail=? WHERE id=?",(str(result.status_code),rid))
            log(event='refused',sku=sku,status=result.status_code,reason=result.text[:180])
            if result.status_code==402:break
            if result.status_code!=429:blocked.add(sku)
            wait=3
            if result.status_code==429:
                try:wait=max(1,min(60,float(result.json().get('wait',3))+.25))
                except (TypeError,ValueError):pass
            time.sleep(wait)
        time.sleep(1)

if __name__=='__main__':main()
