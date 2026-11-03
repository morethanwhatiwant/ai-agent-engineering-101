"""Round-3 stock seller with deterministic, public-price referral benefits.

PROGRAM explains the program; STATUS member=<secret> returns membership status.
Purchase: I want to buy <SKU> [ref=<public code>] [member=<private secret>].
Member keys are bearer credentials: transferable, not account authentication.
"""
import argparse
import asyncio
import base64
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uvicorn
from starlette.applications import Starlette
from a2a.helpers import get_message_text, new_data_part, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from cryptography.hazmat.primitives import serialization
from referral_program import Program, policy
from referral_campaign import tick
from stock_pricing import public_price

STATE = Path(os.environ.get('MARKET_DIR', '.market'))


def sign(receipt):
    key = serialization.load_pem_private_key(Path('shop_key.pem').read_bytes(), password=None)
    body = {k: receipt[k] for k in ('shop', 'item', 'price', 'task')}
    canonical = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    return dict(receipt, sig_seller=base64.b64encode(key.sign(canonical)).decode())


def credentials(text):
    values = {}
    for key in ('member', 'ref'):
        found = re.search(r'\b'+key+r'=([A-Za-z0-9_-]+)', text)
        values[key] = found.group(1) if found else ''
    return values


def select_unit(text, units, reserved):
    want = re.sub(r'\b(?:ref|member)=[A-Za-z0-9_-]+', '', text).lower()
    valid = [u for u in units if u['unit'] not in reserved and u.get('grade') != 'expired'
             and (not u.get('expires_at') or u['expires_at'] > time.time())]
    named = [u for u in valid if u['sku'].lower() in want]
    fits = named or [u for u in valid if any(str(v).lower() in want for v in
                    [u.get('name'), u.get('category', '').split('>')[-1]] if v)]
    return min(fits, key=lambda u: (u.get('expires_at') or float('inf'), u.get('supplied_at', 0)), default=None)


class Seller(AgentExecutor):
    def __init__(self):
        self.open = {}

    async def execute(self, context, event_queue):
        task = context.current_task or new_task_from_user_message(context.message)
        if context.current_task is None:
            await event_queue.enqueue_event(task)
        up = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        say = lambda s: up.new_agent_message(parts=[new_text_part(s)])
        text = get_message_text(context.message) or ''
        cred = credentials(text)
        program = Program(STATE)
        try:
            if text.strip().upper() == 'PROGRAM':
                p = program.p
                await up.complete(say(
                    f"창립 단골 {p['founder_limit']}명: 결제+리뷰(별점 무관)+거래번호 refs를 첨부한 buyer 포럼 후기. "
                    f"사후 확인 후 {p['ends_at']}까지 일반 견적에서 {p['founder_percent']}% 할인. "
                    "리뷰/포럼 참여 혜택임을 후기에서 밝혀주세요. 구매 후 받은 비공개 키로 STATUS member=<key>. "
                    "공개 추천 코드는 구매문의에 ref=<code>. 직접 추천 보상만 지급하며 중복 할인 없음. "
                    "회원키는 소지자에게 혜택을 주는 비공개 키이므로 공유하지 마세요."))
                return
            if text.upper().startswith('STATUS '):
                await up.complete(say(json.dumps(program.status(cred['member']), ensure_ascii=False)))
                return
            # Capture a cursor before any sale can be issued; otherwise the first
            # worker run might skip that sale while initializing at the ledger tail.
            if program.get('cursor') is None:
                await up.reject(say('Market synchronization is starting. Please retry shortly.'))
                return
            inv = json.loads((STATE/'inventory.json').read_text())
            catalog_path = STATE/'catalog.json'
            catalog = json.loads(catalog_path.read_text()) if catalog_path.exists() else {}
            if inv.get('round', 0) != 3:
                await up.reject(say('This seller requires Round 3 inventory.'))
                return
            # Unsigned negotiations expire; signed promises stay reserved in SQLite.
            self.open = {k: v for k, v in self.open.items() if v['until'] > time.time()}
            deal = self.open.get(task.id)
            if not deal:
                reserved = program.reserved_units() | {v['unit']['unit'] for v in self.open.values()}
                unit = select_unit(text, inv['units'], reserved)
                if not unit:
                    await up.reject(say('No matching stock available.'))
                    return
                try:
                    base = public_price(unit, catalog)
                except ValueError:
                    await up.reject(say('Cannot verify this stock grade or expiry.'))
                    return
                q = program.quote(base, unit['cost'], token=cred['member'], ref=cred['ref'])
                deal = dict(unit=unit, quote=q, credentials=cred, until=time.time()+120)
                self.open[task.id] = deal
            else:
                unit = next((u for u in inv['units'] if u['unit'] == deal['unit']['unit']), None)
                if not unit or unit.get('grade') == 'expired' or (unit.get('expires_at') and unit['expires_at'] <= time.time()):
                    self.open.pop(task.id, None)
                    await up.reject(say('The quoted stock is no longer available.'))
                    return
                try:
                    latest = program.quote(public_price(unit, catalog), unit['cost'],
                                           token=deal['credentials']['member'], ref=deal['credentials']['ref'])
                except ValueError:
                    self.open.pop(task.id, None)
                    await up.reject(say('Cannot verify this stock grade or expiry.'))
                    return
                # Honor the lower of the promised quote and the current price.
                if latest['price'] < deal['quote']['price']:
                    deal['quote'] = latest
                match = re.fullmatch(r'\s*(?:offer\s*[=:]?\s*)?(\d+)\s*', text, flags=re.I)
                if match and int(match.group(1)) >= deal['quote']['price']:
                    q = deal['quote']
                    try:
                        secret = program.accept(task.id, unit['unit'], unit['sku'], unit['cost'], q)
                    except (ValueError, sqlite3.IntegrityError):
                        self.open.pop(task.id, None)
                        await up.reject(say('Stock or benefit already reserved. Please request a new quote.'))
                        return
                    receipt = sign(dict(shop=program.p['shop_name'], item=unit['sku'], price=q['price'],
                                        task=task.id, unit=unit['unit'], cert=True))
                    await up.add_artifact(parts=[new_data_part(receipt)], name='receipt')
                    private = (f' Private provisional member key: {secret}. Save privately; never post it. '
                               'After payment use STATUS member=<key> for your referral code.') if secret else ''
                    await up.complete(say(f"Agreed at {q['price']}. Membership activates only after market settlement."+private))
                    self.open.pop(task.id, None)
                    return
            q = deal['quote']
            await up.requires_input(say(f"{deal['unit']['name']}: public price {q['base']}; benefit {q['benefit']}. "
                                       f"Send a number to accept. Asking {q['price']}"))
        finally:
            program.close()

    async def cancel(self, context, event_queue):
        task = context.current_task
        self.open.pop(task.id, None)
        await TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id).cancel()


def build(port, publish=True):
    @asynccontextmanager
    async def lifespan(app):
        async def loop():
            while True:
                try:
                    await asyncio.to_thread(tick, STATE, 'shop_key.pem', publish)
                except Exception as exc:
                    print('referral synchronization:', type(exc).__name__, flush=True)
                await asyncio.sleep(15)
        worker = asyncio.create_task(loop())
        try:
            yield
        finally:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
    card = AgentCard(name=policy()['shop_name'], description='Stock shop with founding-member discount. Send PROGRAM for referral terms.',
        version='1.0.0', default_input_modes=['text/plain'], default_output_modes=['application/json','text/plain'],
        capabilities=AgentCapabilities(streaming=False), supported_interfaces=[AgentInterface(
            protocol_binding='JSONRPC', url=f'http://127.0.0.1:{port}/', protocol_version='1.0')],
        skills=[AgentSkill(id='shop', name='Buy and refer', description='Ask for SKU; optionally append ref=code or member=private_key.',
                           tags=['shopping','referral'], examples=['PROGRAM', 'I want to buy G00001 ref=REF-EXAMPLE'])])
    handler = DefaultRequestHandler(agent_executor=Seller(), task_store=InMemoryTaskStore(), agent_card=card)
    return Starlette(routes=create_agent_card_routes(card)+create_jsonrpc_routes(handler, '/'), lifespan=lifespan)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=9301)
    ap.add_argument('--no-post', action='store_true')
    args = ap.parse_args()
    uvicorn.run(build(args.port, publish=not args.no_post), host='127.0.0.1', port=args.port, log_level='warning')
