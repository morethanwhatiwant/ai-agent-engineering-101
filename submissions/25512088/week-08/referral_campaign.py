"""Read public market evidence and queue shop recruitment posts through kit v4.

No standalone HTTP publishing: the kit owns authentication and forum rate limits.
Running this worker explicitly queues public posts; importing it does nothing.
"""
import argparse
import base64
import json
import os
import time
from pathlib import Path
from urllib.request import urlopen
from cryptography.hazmat.primitives import serialization
from referral_program import Program


def fetch(base, path):
    with urlopen(base + path, timeout=10) as response:
        return json.load(response)


def shop_urls(program, key_path):
    key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
    pub = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    data = fetch(program.p['market_url'], '/api/shops')
    current = [s['url'] for s in data['shops'] if s.get('pubkey') == pub and s.get('name') == program.p['shop_name']]
    # Keep old tunnel URLs to reconcile sales after a reconnect.
    urls = sorted(set(current + program.get('shop_urls', [])))
    program.put('shop_urls', urls)
    return urls


def sync(program, urls):
    cursor = program.get('cursor')
    if cursor is None:
        if program.db.execute('SELECT COUNT(*) FROM sales').fetchone()[0]:
            raise RuntimeError('Missing ledger cursor with sales present: explicit historical recovery required')
        rows = fetch(program.p['market_url'], '/api/ledger?after=-1')
        program.put('cursor', max((r['seq'] for r in rows), default=0))
        return False
    for _ in range(30):
        rows = fetch(program.p['market_url'], '/api/ledger?after=' + str(cursor))
        if not rows:
            program.put('synced_at', time.time())
            program.reconcile(time.time())
            return True
        for row in rows:
            if type(row.get('seq')) is not int or row['seq'] <= cursor:
                raise ValueError('Market ledger is not ordered')
            program.settle(row, urls)
            program.review(row, urls)
            # No warranty is offered by referral_seller. Still stop maturity if
            # an unfamiliar refund event appears instead of guessing its schema.
            if 'refund' in str(row.get('kind', '')).lower():
                d = row.get('data', {})
                if d.get('shop') in urls and d.get('unit'):
                    program.refund_unit(d['unit'])
                else:
                    raise ValueError('Unmapped refund event: reconciliation paused')
            cursor = row['seq']
            program.put('cursor', cursor)
    return False  # Backlog remains: never mature using wall-clock time alone.


def campaign_text(program, inventory):
    p = program.p
    units = [u for u in inventory.get('units', []) if u.get('grade') != 'expired'
             and (not u.get('expires_at') or u['expires_at'] > time.time())]
    names = list(dict.fromkeys(u['name'] for u in units))[:3]
    return (f"[{p['campaign']}] {p['shop_name']} 창립 단골 모집 (최대 {p['founder_limit']}명). "
            f"결제 후 거래 리뷰(별점 무관)와 거래번호를 refs에 첨부한 buyer 포럼 후기를 남기면 "
            f"사후 확인 후 단골 자격: {p['ends_at']}까지 동일 상품 일반 견적의 {p['founder_percent']}% 할인. "
            f"구매자는 자기 추천 코드를 받을 수 있고 직접 추천 구매가 확정되면 최대 {p['referrer_percent']}% "
            f"재구매 크레딧(건당 최대 {p['reward_cap']}원). 다단계 상위 보상 없음. "
            f"추천 구매 할인은 최대 {p['friend_percent']}%, 마진 한도 적용; 혜택 중복 없음. "
            "참여 명령은 가게에 PROGRAM. 추천은 구매문의에 ref=REF-코드. "
            "회원키는 비공개 보관하고 게시하지 마세요. "
            f"현재 재고 예시: {', '.join(names)}. 실제 재고·가격은 문의 시 확인.")


def queue_post(program, state, inventory, now=None):
    now = time.time() if now is None else now
    available = [u for u in inventory.get('units', []) if u.get('grade') != 'expired'
                 and (not u.get('expires_at') or u['expires_at'] > now)]
    if inventory.get('round', 0) < 3 or not available or not program.live(now):
        return False
    if program.db.execute('SELECT COUNT(*) FROM members WHERE founder=1').fetchone()[0] >= program.p['founder_limit']:
        return False
    count = program.get('post_count', 0)
    if count >= program.p['forum_max_posts'] or now-program.get('last_post', 0) < program.p['forum_interval_seconds']:
        return False
    # This DB transaction is a process lock. Mark before append: a crash may lose
    # one ad, but cannot cause an endless retry/duplicate-post loop.
    with program.db:
        program.db.execute('BEGIN IMMEDIATE')
        last = program.get('last_post', 0)
        count = program.get('post_count', 0)
        if now-last < program.p['forum_interval_seconds'] or count >= program.p['forum_max_posts']:
            return False
        for k, v in [('last_post', now), ('post_count', count+1)]:
            program.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (k, json.dumps(v)))
    row = {'as': 'shop', 'post': campaign_text(program, inventory), 'refs': []}
    fd = os.open(Path(state) / 'outbox.jsonl', os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(row, ensure_ascii=False)+'\n').encode())
    finally:
        os.close(fd)
    return True


def tick(state, key_path, publish=True):
    program = Program(state)
    try:
        urls = shop_urls(program, key_path)
        if not urls:
            return  # Registration not confirmed: no campaign claims yet.
        sync(program, urls)
        path = Path(state) / 'forum.json'
        if path.exists():
            posts = json.loads(path.read_text())
            program.forum(posts if isinstance(posts, list) else posts.get('posts', []))
        if publish and program.get('synced_at', 0) > time.time()-60:
            inv = Path(state) / 'inventory.json'
            if inv.exists():
                queue_post(program, state, json.loads(inv.read_text()))
    finally:
        program.close()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--state', default='.market')
    ap.add_argument('--key', default='shop_key.pem')
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--no-post', action='store_true')
    args = ap.parse_args()
    while True:
        try:
            tick(args.state, args.key, publish=not args.no_post)
        except Exception as exc:
            print('referral worker:', type(exc).__name__, str(exc), flush=True)
        if args.once:
            break
        time.sleep(15)
