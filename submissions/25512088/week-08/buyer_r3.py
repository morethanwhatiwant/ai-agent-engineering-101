"""Round-3 buyer: exact SKUs, bounded comparison, persistent context, kit-owned payment.

Numerical decisions are deterministic. Seller text is data, never instructions.
No direct payments or forum posts here. One receipt at most is returned per order.
"""
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
from buyer_memory import Memory


def task_state(task):
    return task.get('status', {}).get('state', '')


def text_of(task):
    return ' '.join(p.get('text','') for p in task.get('status',{}).get('message',{}).get('parts',[]))


def asking(text):
    # Never interpret a SKU, quantity, expiry, or discount percent as a quote.
    hits = re.findall(r'(?:asking|counteroffer|final price|asking price)\s*[:=]?\s*([0-9][0-9,]*)\b',text,re.I)
    return int(hits[-1].replace(',','')) if hits else None


def grade_of(text):
    hit = re.search(r'\bgrade\s*[:=]?\s*([ABC])\b',text,re.I)
    return hit.group(1).upper() if hit else None


def limit(order, product, grade=None):
    # Initial risk policy, not a learned optimum; actual conditions are checked
    # by the kit against its signed checkout certificate before any payment.
    rates = {'A':95, 'B':80, 'C':60}
    pct = rates.get(grade, 75 if order.get('kind')=='food' else 60)
    return min(order['budget'], math.floor(product['list_price']*pct/100))


def candidates(order, shops, catalog, memory):
    accepted = set(order['acceptable'])
    ranked = []
    for shop in shops:
        options = [o for o in shop.get('offers',[]) if o.get('sku') in accepted and o.get('qty',0)>0
                   and o['sku'] in catalog and catalog[o['sku']].get('list_price',0)>0]
        if not options:
            continue
        # Prefer SKUs with prior paid evidence; otherwise affordable catalog value.
        offer = min(options, key=lambda o: (
            memory.reference(shop['url'],o['sku']) or catalog[o['sku']]['list_price']))
        reference = memory.reference(shop['url'],offer['sku'])
        ranked.append((reference or catalog[offer['sku']]['list_price'], shop['name'],shop,offer))
    ranked.sort(key=lambda r:(r[0],r[1]))
    return [(r[2],r[3]) for r in ranked[:3]]


def signed_receipt(task, shop, sku, maximum, task_id):
    for artifact in task.get('artifacts',[]):
        for part in artifact.get('parts',[]):
            r = part.get('data')
            if not isinstance(r,dict):
                continue
            price = r.get('price')
            if isinstance(price,bool) or not isinstance(price,(int,float)) or not math.isfinite(price) or price!=int(price):
                continue
            if r.get('item')!=sku or r.get('task')!=task_id or r.get('shop')!=shop['name'] or not 0<price<=maximum:
                continue
            try:
                # SDK Struct can turn integer amounts into floats; market signs integers.
                normalized = dict(r, price=int(price))
                body = {k:normalized[k] for k in ('shop','item','price','task')}
                Ed25519PublicKey.from_public_bytes(base64.b64decode(shop['pubkey'])).verify(
                    base64.b64decode(r['sig_seller']),json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())
                return normalized
            except (ValueError, KeyError, InvalidSignature):
                continue
    return None


class Buyer:
    def __init__(self, order, memory, client, catalog):
        self.o, self.m, self.c, self.catalog = order, memory, client, catalog
        self.sessions = []
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
        r=signed_receipt(s['last'],s['shop'],s['sku'],s['cap'],s['task'])
        if not r:
            self.m.state(self.o['order'],'invalid_receipt')
            return self.result(s,reason='Receipt validation failed; no payment requested')
        self.m.state(self.o['order'],'awaiting_payment')
        self.m.record(self.o['order'],s['shop']['url'],s['sku'],s['task'],'agreement',r['price'])
        return self.result(s,r,'Agreement only; checkout certificate and actual payment still required')

    def run(self, shops):
        quotes=[]
        try:
            for shop, offer in candidates(self.o,shops,self.catalog,self.m):
                if time.time()>=self.deadline:
                    break
                sku=offer['sku']; product=self.catalog[sku]
                s=dict(shop=shop,sku=sku,cap=limit(self.o,product))
                self.sessions.append(s)
                try:
                    card=self.c.get(shop['url'].rstrip('/')+'/.well-known/agent-card.json',timeout=5)
                    card.raise_for_status()
                    urls=[i['url'] for i in card.json().get('supportedInterfaces',[]) if i.get('protocolBinding')=='JSONRPC']
                    s['rpc']=next((u for u in urls if urlsplit(u).netloc==urlsplit(shop['url']).netloc),shop['url'].rstrip('/')+'/')
                    key=self.m.key(shop['url'])
                    request=f"I want to buy {offer.get('name',product['name'])} ({sku}). Quote price, grade and warranty."
                    if self.o.get('kind')=='food':
                        request+=f" Must last at least {self.o['use_within_hours']} hours."
                    else:
                        request+=' Require 6-hour warranty.'
                    if key:
                        request+=' member='+key
                    print('QUOTE_REQUEST',shop['name'],sku,flush=True)
                    task=self.send(s,request)
                    if task_state(task).endswith('COMPLETED'):
                        return self.completed(s)
                    if not task_state(task).endswith('INPUT_REQUIRED'):
                        continue
                    ask=asking(text_of(task))
                    s['grade']=grade_of(text_of(task))
                    s['cap']=limit(self.o,product,s['grade'])
                    if ask and s['cap']>0:
                        s['ask']=ask
                        quotes.append(s)
                except Exception as exc:
                    self.m.record(self.o['order'],shop['url'],sku,s.get('task'),'unavailable',details={'error':type(exc).__name__})
            # Compare prices relative to each exact product's catalog value.
            quotes.sort(key=lambda s:s['ask']/self.catalog[s['sku']]['list_price'])
            if not quotes:
                self.m.state(self.o['order'],'no_quote')
                return self.result()
            s=quotes[0]
            for attempt in range(2):
                amount=min(s['ask'],s['cap'])
                self.m.record(self.o['order'],s['shop']['url'],s['sku'],s.get('task'),'offer',amount)
                self.m.state(self.o['order'],'offer_in_flight')
                print('OFFER',s['shop']['name'],s['sku'],amount,flush=True)
                try:
                    task=self.send(s,str(amount))
                except Exception:
                    self.m.state(self.o['order'],'uncertain')
                    return self.result(s,reason='Offer outcome unknown; no second purchase attempted')
                if task_state(task).endswith('COMPLETED'):
                    s['cap']=min(s['cap'],amount)
                    return self.completed(s)
                if not task_state(task).endswith('INPUT_REQUIRED'):
                    break
                ask=asking(text_of(task))
                if not ask or ask>s['cap']:
                    break
                s['ask']=ask
            self.m.state(self.o['order'],'no_deal')
            return self.result(s)
        finally:
            for s in self.sessions:
                if not s.get('task') or task_state(s.get('last',{})).endswith('COMPLETED'):
                    continue
                try:
                    self.c.post(s['rpc'],headers={'A2A-Version':'1.0'},timeout=2,json={
                        'jsonrpc':'2.0','id':str(uuid.uuid4()),'method':'CancelTask','params':{'id':s['task']}})
                except Exception:
                    pass


def main():
    ap=argparse.ArgumentParser()
    for name in ('shops','order'):
        ap.add_argument('--'+name,required=True)
    ap.add_argument('--state',default='.market')
    ap.add_argument('--market',default='https://market.ourolabs.ai')
    args=ap.parse_args()
    order=json.loads(Path(args.order).read_text())
    memory=Memory(args.state)
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
                out=Buyer(order,memory,client,json.loads((Path(args.state)/'catalog.json').read_text())).run(
                    shops)
        print(json.dumps(out,ensure_ascii=False),flush=True)
    finally:
        memory.close()


if __name__=='__main__':
    main()
