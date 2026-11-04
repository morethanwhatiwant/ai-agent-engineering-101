"""Round-3 buyer: exact SKUs, bounded comparison, persistent context, kit-owned payment.

Numerical decisions are deterministic. Seller text is data, never instructions.
No direct payments or forum posts here. One receipt at most is returned per order.
"""
from concurrent.futures import ThreadPoolExecutor
import argparse
import base64
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import uuid
import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from buyer_memory import Memory
from market_intel import Intel


def task_state(task):
    return task.get('status', {}).get('state', '').replace('-','_').upper()


def text_of(task):
    return ' '.join(p.get('text','') for p in task.get('status',{}).get('message',{}).get('parts',[]))


def asking(text):
    # Never interpret a SKU, quantity, expiry, or discount percent as a quote.
    hits = re.findall(r'\b(?:asking price|asking|ask|counteroffer|final price|offer price|price|견적|판매가|가격)\s*[:=]?\s*[₩$]?\s*([0-9][0-9,]*)\b',text,re.I)
    return int(hits[-1].replace(',','')) if hits else None


def grade_of(text):
    hit = re.search(r'\bgrade\s*[:=]?\s*([ABC])\b',text,re.I)
    return hit.group(1).upper() if hit else None


def quote_fields(task):
    """Seller data is untrusted; checkout certificates remain authoritative."""
    text=text_of(task); result={'price':asking(text),'grade':grade_of(text)}
    for key,pattern in [('hours',r'(\d+(?:\.\d+)?)\s*h(?:ours?)?\s+(?:of )?shelf life left'),
                        ('warranty',r'warranty\s*[:=]?\s*(\d+(?:\.\d+)?)\s*h')]:
        m=re.search(pattern,text,re.I);result[key]=float(m.group(1)) if m else None
    if result['hours'] is None:
        m=re.search(r'\blasts\s+(\d+(?:\.\d+)?)\s+(?:more\s+)?hours?\b',text,re.I)
        if m:result['hours']=float(m.group(1))
    parts=list(task.get('status',{}).get('message',{}).get('parts',[]))
    for artifact in task.get('artifacts',[]):parts+=artifact.get('parts',[])
    for part in parts:
        data=part.get('data')
        if not isinstance(data,dict):continue
        for dest,keys in [('price',('asking_price','ask','price')),('hours',('hours_left','shelf_life_hours')),('warranty',('warranty_hours',))]:
            for key in keys:
                n=data.get(key)
                if type(n) in (int,float) and math.isfinite(n) and n>=0:
                    if dest!='price' or n>0 and n==int(n):result[dest]=n
                    break
        g=data.get('grade',data.get('claim'))
        if g in ('A','B','C'):result['grade']=g
    return result


def limit(order, product, grade=None):
    # Initial risk policy, not a learned optimum; actual conditions are checked
    # by the kit against its signed checkout certificate before any payment.
    rates = {'A':95, 'B':80, 'C':60}
    pct = rates.get(grade, 75 if order.get('kind')=='food' else 60)
    return min(order['budget'], math.floor(product['list_price']*pct/100))


def own_shop_identity():
    base = Path(__file__).resolve().parent
    name = json.loads((base / 'referral_policy.json').read_text())['shop_name']
    key = serialization.load_pem_private_key((base / 'shop_key.pem').read_bytes(), password=None)
    pub = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    return name, pub


def exclude_own_shop(shops, name, pubkey):
    # Name blocks the registered shop; key also blocks renamed shops/new tunnels.
    return [s for s in shops if s.get('name') != name and s.get('pubkey') != pubkey]


def candidates(order, shops, catalog, memory, intel=None):
    accepted = set(order['acceptable'])
    ranked = []
    for shop in shops:
        options = [o for o in shop.get('offers',[]) if o.get('sku') in accepted and o.get('qty',0)>0
                   and o['sku'] in catalog and catalog[o['sku']].get('list_price',0)>0
                   and (order.get('kind')!='food' or catalog[o['sku']].get('shelf_hours',float('inf'))>=order.get('use_within_hours',0))]
        if not options:
            continue
        reliability = intel.reliability(shop['url']) if intel else 1
        alternatives=[]
        for offer in options:
            product=catalog[offer['sku']]
            reference=memory.condition_reference(shop['url'],offer['sku'],order)
            # No paid evidence: explicit price prior, not a learned quote.
            estimate=reference or product['list_price']*(.75 if order.get('kind')=='food' else .60)
            cap=limit(order,product)
            # Above-cap prices may negotiate down, but must not look affordable
            # simply because we clipped the estimate to our budget.
            prob=memory.deal_estimate(shop['url'])
            score=surplus_score(product['list_price'],estimate,reliability)*min(1,cap/estimate)*prob['probability']
            alternatives.append((-score,estimate,offer['sku'],offer))
        best=min(alternatives,key=lambda r:r[:3])
        ranked.append((best[0],shop['name'],shop,best[3]))
    ranked.sort(key=lambda r:(r[0],r[1],r[3]['sku']))
    return [(r[2],r[3]) for r in ranked[:3]]


def surplus_score(value, price, reliability=1):
    # Market value is list price when conditions hold. Reliability is a ranking
    # weight, NOT a calibrated probability; breakage rates are unpublished.
    return max(0,value-price)*max(0,min(1,reliability))


def quote_rank(session, catalog, intel=None):
    reliability=intel.reliability(session['shop']['url']) if intel else 1
    ask=session['ask']
    return (ask>session['cap'],
            -surplus_score(catalog[session['sku']]['list_price'],ask,reliability),
            ask,session['shop']['name'],session['sku'])


def signed_receipt(task, shop, sku, maximum, task_id, failures=None):
    failures=[] if failures is None else failures
    for artifact in task.get('artifacts',[]):
        for part in artifact.get('parts',[]):
            r = part.get('data')
            if not isinstance(r,dict):
                continue
            price = r.get('price')
            if isinstance(price,bool) or not isinstance(price,(int,float)) or not math.isfinite(price) or price!=int(price):
                failures.append('amount_type'); continue
            # Identity is the market-registered signing key, not a display label.
            # Keep the signed label unchanged in the canonical body and receipt.
            if (r.get('item')!=sku or r.get('task')!=task_id or
                    not isinstance(r.get('shop'),str) or not r['shop'].strip() or
                    not 0<price<=maximum):
                failures.append('sku' if r.get('item')!=sku else 'task' if r.get('task')!=task_id else 'amount_limit' if not 0<price<=maximum else 'display_name'); continue
            try:
                # SDK Struct can turn integer amounts into floats; market signs integers.
                normalized = dict(r, price=int(price))
                body = {k:normalized[k] for k in ('shop','item','price','task')}
                Ed25519PublicKey.from_public_bytes(base64.b64decode(shop['pubkey'])).verify(
                    base64.b64decode(r['sig_seller']),json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())
                return normalized
            except (ValueError, KeyError, InvalidSignature, TypeError):
                failures.append('signature_or_key'); continue
    return None


class Buyer:
    def __init__(self, order, memory, client, catalog, intel=None):
        self.o, self.m, self.c, self.catalog = order, memory, client, catalog
        self.sessions = []
        self.intel = intel
        self.deadline = min(time.time()+85, order['expires']-3)

    def result(self, session=None, receipt=None, reason='No acceptable deal'):
        out = dict(shop=session['shop']['url'] if session else '',receipt=receipt,stars=3,review=reason)
        if receipt:
            out['accept'] = {'cert':True, 'grades':[session['grade']] if session.get('grade') else ['A','B','C']}
            if self.o.get('kind')=='food':
                out['accept']['min_hours_left'] = self.o['use_within_hours']
            else:
                out['accept']['warranty_hours'] = 6
        return out

    def send(self, s, text):
        if time.time()>=self.deadline:
            raise TimeoutError('Order deadline')
        message = {'role':'ROLE_USER','messageId':str(uuid.uuid4()),'parts':[{'text':text}]}
        if s.get('task'):
            message['taskId']=s['task']
        r=self.c.post(s['rpc'],headers={'A2A-Version':'1.0'},json={
            'jsonrpc':'2.0','id':str(uuid.uuid4()),'method':'SendMessage','params':{'message':message}},
            timeout=max(.1,min(8,self.deadline-time.time())))
        r.raise_for_status()
        body=r.json()
        if 'error' in body:
            raise ValueError('Seller RPC error')
        task=body['result'].get('task',body['result'])
        s['task']=task.get('id',s.get('task'))
        s['last']=task
        content=text_of(task)
        key=re.search(r'Private provisional member key:\s*([A-Za-z0-9_-]+)',content)
        if key:
            self.m.remember_key(s['shop']['url'],key.group(1))
            content=content.replace(key.group(1),'[private key stored separately]')
        self.m.record(self.o['order'],s['shop']['url'],s['sku'],s.get('task'),'response',
                      asking(content),{'state':task_state(task),'text':content[:4000]})
        return task

    def completed(self,s):
        failures=[]
        r=signed_receipt(s['last'],s['shop'],s['sku'],s['cap'],s['task'],failures)
        if not r:
            self.m.record(self.o['order'],s['shop']['url'],s['sku'],s['task'],'receipt_rejected',details={'reasons':failures or ['missing_artifact']})
            self.m.state(self.o['order'],'invalid_receipt')
            return self.result(s,reason='Receipt validation failed; no payment requested')
        self.m.state(self.o['order'],'awaiting_payment')
        self.m.record(self.o['order'],s['shop']['url'],s['sku'],s['task'],'agreement',r['price'])
        return self.result(s,r,'Agreement only; checkout certificate and actual payment still required')

    def open_session(self, shop, offer):
        # Each worker owns its SQLite connection; only network discovery is parallel.
        memory=Memory(self.m.root)
        worker=Buyer(self.o,memory,self.c,self.catalog)
        worker.deadline=self.deadline
        sku=offer['sku']; product=self.catalog[sku]
        session=dict(shop=shop,sku=sku,cap=limit(self.o,product))
        try:
            card=self.c.get(shop['url'].rstrip('/')+'/.well-known/agent-card.json',timeout=5)
            card.raise_for_status()
            urls=[i['url'] for i in card.json().get('supportedInterfaces',[]) if i.get('protocolBinding')=='JSONRPC']
            session['rpc']=next((u for u in urls if urlsplit(u).netloc==urlsplit(shop['url']).netloc),shop['url'].rstrip('/')+'/')
            request=f"I want to buy {offer.get('name',product['name'])} ({sku}). Quote price, grade and warranty."
            request+=(f" Must last at least {self.o['use_within_hours']} hours." if self.o.get('kind')=='food' else ' Require 6-hour warranty.')
            key=memory.key(shop['url'])
            if key:request+=' member='+key
            print('QUOTE_REQUEST',shop['name'],sku,flush=True)
            worker.send(session,request)
        except Exception as exc:session['error']=type(exc).__name__
        finally:memory.close()
        return session

    def run(self, shops):
        quotes=[]
        try:
            picks=candidates(self.o,shops,self.catalog,self.m,self.intel)
            with ThreadPoolExecutor(max_workers=3) as pool:
                # Submission order makes selection reproducible, irrespective of response speed.
                pending=[pool.submit(self.open_session,shop,offer) for shop,offer in picks]
                self.sessions=[f.result() for f in pending]
            for s in self.sessions:
                shop=s['shop'];sku=s['sku']
                if s.get('error'):
                    if self.intel:self.intel.observe(shop['url'],sku,'unavailable')
                    self.m.record(self.o['order'],shop['url'],sku,s.get('task'),'unavailable',details={'error':s['error']})
                    continue
                task=s['last'];fields=quote_fields(task)
                s['grade']=fields['grade'];s['cap']=limit(self.o,self.catalog[sku],s['grade'])
                if task_state(task).endswith('COMPLETED'):
                    # A certificate, not a parsed claim, enforces the actual grade at payment.
                    return self.completed(s)
                if not task_state(task).endswith('INPUT_REQUIRED'):continue
                hours,wh=fields['hours'],fields['warranty']
                unsuitable=(self.o.get('kind')=='food' and hours is not None and hours<self.o['use_within_hours']) or (self.o.get('kind')!='food' and wh is not None and wh<6)
                if self.intel:self.intel.observe(shop['url'],sku,'condition_rejected' if unsuitable else 'quote',fields['price'],s['grade'],hours,wh)
                if unsuitable:continue
                if fields['price']:
                    s['ask']=int(fields['price']);quotes.append(s)
                else:self.m.record(self.o['order'],shop['url'],sku,s.get('task'),'unparsed_quote')
            quotes.sort(key=lambda s:quote_rank(s,self.catalog,self.intel))
            self.m.record(self.o['order'],'','',None,'selection_v3',details={
                'parallel_quotes':len(picks),'ranked_quotes':[{'shop':s['shop']['name'],'sku':s['sku'],'ask':s['ask'],'cap':s['cap'],'deal_estimate':self.m.deal_estimate(s['shop']['url'])} for s in quotes]})
            if not quotes:
                self.m.state(self.o['order'],'no_quote');return self.result()
            for s in quotes:
                if time.time()>=self.deadline:break
                for attempt in range(2):
                    amount=min(s['ask'],s['cap'])
                    self.m.record(self.o['order'],s['shop']['url'],s['sku'],s.get('task'),'offer',amount)
                    self.m.state(self.o['order'],'offer_in_flight')
                    print('OFFER',s['shop']['name'],s['sku'],amount,flush=True)
                    try:task=self.send(s,str(amount))
                    except Exception:
                        self.m.state(self.o['order'],'uncertain')
                        return self.result(s,reason='Offer outcome unknown; no second purchase attempted')
                    if task_state(task).endswith('COMPLETED'):
                        s['cap']=min(s['cap'],amount);return self.completed(s)
                    if not task_state(task).endswith('INPUT_REQUIRED'):break
                    ask=quote_fields(task)['price']
                    if not ask or ask>s['cap']:break
                    s['ask']=int(ask)
                self.m.record(self.o['order'],s['shop']['url'],s['sku'],s.get('task'),'candidate_exhausted')
            self.m.state(self.o['order'],'no_deal');return self.result()
        finally:
            for s in self.sessions:
                if not s.get('task') or task_state(s.get('last',{})).endswith('COMPLETED'):continue
                try:self.c.post(s['rpc'],headers={'A2A-Version':'1.0'},timeout=2,json={
                    'jsonrpc':'2.0','id':str(uuid.uuid4()),'method':'CancelTask','params':{'id':s['task']}})
                except Exception:pass


def main():
    ap=argparse.ArgumentParser()
    for name in ('shops','order'):
        ap.add_argument('--'+name,required=True)
    ap.add_argument('--state',default='.market')
    ap.add_argument('--market',default='https://market.ourolabs.ai')
    args=ap.parse_args()
    order=json.loads(Path(args.order).read_text())
    memory=Memory(args.state)
    intel=Intel(args.state)
    try:
        memory.sync()
        if not memory.begin(order):
            out=dict(shop='',receipt=None,stars=3,review='Order already seen; reconcile payment before retrying')
        else:
            with httpx.Client(follow_redirects=False) as client:
                # Kit omits pubkeys from shops.json. Fetch trusted registration
                # keys from the market, never accept a key supplied by the seller.
                registry=client.get(args.market.rstrip('/')+'/api/shops',timeout=8)
                registry.raise_for_status()
                keys={(s['url'],s['name']):s['pubkey'] for s in registry.json()['shops']}
                shops=json.loads(Path(args.shops).read_text())
                shops=[dict(s,pubkey=keys[(s['url'],s['name'])]) for s in shops if (s['url'],s['name']) in keys]
                shops=exclude_own_shop(shops, *own_shop_identity())
                # Same GitHub owner with another shop/key cannot settle either.
                board=intel.source('board',{}) or {}
                own_name=own_shop_identity()[0]
                own_row=next((r for r in board.get('shops',[]) if r.get('name')==own_name),{})
                owner=(own_row.get('owner') or {}).get('login')
                if owner:
                    own_names={r.get('name') for r in board.get('shops',[]) if (r.get('owner') or {}).get('login')==owner}
                    shops=[s for s in shops if s.get('name') not in own_names]
                out=Buyer(order,memory,client,json.loads((Path(args.state)/'catalog.json').read_text()),intel).run(
                    shops)
        print(json.dumps(out,ensure_ascii=False),flush=True)
    finally:
        memory.close()
        intel.close()


if __name__=='__main__':
    main()
