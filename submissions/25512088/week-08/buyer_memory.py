"""Durable order state. Agreement is not payment; restart never repeats an order."""
import json
import os
from pathlib import Path
import sqlite3
import time


class Memory:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root/'buyer.sqlite3', timeout=15)
        os.chmod(self.root/'buyer.sqlite3', 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, context TEXT, state TEXT, updated REAL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, order_id TEXT, shop TEXT,
          sku TEXT, task TEXT, kind TEXT, price INTEGER, details TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS payments(order_id TEXT PRIMARY KEY, shop TEXT, sku TEXT,
          price INTEGER, paid INTEGER, ledger INTEGER, reason TEXT);
        CREATE TABLE IF NOT EXISTS outcomes(order_id TEXT PRIMARY KEY, details TEXT);
        CREATE TABLE IF NOT EXISTS credentials(shop TEXT PRIMARY KEY, member_key TEXT, updated REAL);
        ''')

    def begin(self, order):
        safe = {k: order.get(k) for k in ('order','item','category','budget','acceptable','kind','use_within_hours','expires')}
        with self.db:
            cursor = self.db.execute('INSERT OR IGNORE INTO orders VALUES(?,?,?,?)',
                                     (order['order'], json.dumps(safe), 'running', time.time()))
        return cursor.rowcount == 1

    def state(self, order, state):
        with self.db:
            self.db.execute('UPDATE orders SET state=?,updated=? WHERE id=?', (state,time.time(),order))

    def record(self, order, shop, sku, task, kind, price=None, details=None):
        with self.db:
            self.db.execute('INSERT INTO events(order_id,shop,sku,task,kind,price,details,ts) VALUES(?,?,?,?,?,?,?,?)',
                            (order,shop,sku,task,kind,price,json.dumps(details or {},ensure_ascii=False),time.time()))

    def sync(self):
        path = self.root/'payments.jsonl'
        if not path.exists():
            return
        with self.db:
            for line in path.read_text().splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row.get('order'), str) or type(row.get('paid')) is not bool:
                    continue
                self.db.execute('INSERT OR REPLACE INTO payments VALUES(?,?,?,?,?,?,?)',
                                (row['order'],row.get('shop'),row.get('item'),row.get('price'),int(row['paid']),row.get('ledger'),row.get('reason')))
                self.db.execute("UPDATE orders SET state=?,updated=? WHERE id=? AND state NOT IN ('paid','payment_failed')",
                                ('paid' if row['paid'] else 'payment_failed',time.time(),row['order']))

    def reference(self, shop, sku):
        # Only recent paid prices, for this exact SKU at this shop.
        row = self.db.execute('''SELECT p.price FROM payments p JOIN orders o ON o.id=p.order_id
          WHERE p.paid=1 AND p.shop=? AND p.sku=? AND o.updated>? ORDER BY o.updated DESC LIMIT 1''',
          (shop,sku,time.time()-86400)).fetchone()
        return row[0] if row else None

    def remember_key(self, shop, key):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO credentials VALUES(?,?,?)', (shop,key,time.time()))

    def key(self, shop):
        row = self.db.execute('SELECT member_key FROM credentials WHERE shop=?', (shop,)).fetchone()
        return row[0] if row else ''

    def close(self):
        self.db.close()
