import json,math,tempfile,time,unittest
from pathlib import Path
from referral_program import Program
from seller_strategy import sale_quote,negotiate,protected_floor
from buyer_memory import Memory
from growth_offers import offers
from market_intel import Intel

class RolloutTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.p=Program(self.root)
  self.u=dict(unit='u',sku='sku',name='item',grade='B',kind='goods',list_price=20000,cost=8000,expires_at=None)
 def tearDown(self):self.p.close();self.tmp.cleanup()
 def test_haggle_keeps_benefits_and_warranty_margin(self):
  q=sale_quote(self.u,{},self.p)
  for offer in (1,10000,14000,20000):
   n=negotiate(self.u,q,offer,{},self.p)
   self.assertLessEqual(n['price'],q['price']);self.assertEqual(n['warranty_hours'],6)
   self.assertGreaterEqual(n['price']*.85-n['reward'],self.u['cost']+math.ceil(self.u['cost']*.09))
  self.assertLess(negotiate(self.u,q,14000,{},self.p)['price'],q['price'])
 def test_full_refund_cash_reserved_atomically(self):
  q=sale_quote(self.u,{},self.p);inv=dict(start=1000000,balance=500000+q['price'])
  self.p.accept('one','u','sku',8000,q,inventory=inv)
  with self.assertRaises(ValueError):self.p.accept('two','v','sku',8000,q,inventory=inv)
  self.assertEqual(self.p.db.execute('select count(*) from warranties').fetchone()[0],1)
 def test_live_quote_holds_excluded_from_offers_expired_holds_clear(self):
  (self.root/'inventory.json').write_text(json.dumps(dict(round=3,units=[self.u])))
  with self.p.db:
   self.p.db.execute('CREATE TABLE quote_holds(task TEXT PRIMARY KEY,unit TEXT,until REAL)')
   self.p.db.execute('INSERT INTO quote_holds VALUES(?,?,?)',('q','u',time.time()+60))
  intel=Intel(self.root)
  try:
   self.assertEqual(offers(self.root,self.p,intel),[])
   with self.p.db:self.p.db.execute('UPDATE quote_holds SET until=0')
   self.assertEqual(offers(self.root,self.p,intel)[0]['warranty_hours'],6)
  finally:intel.close()
 def test_model_excludes_quote_only_and_uncertain_orders(self):
  m=Memory(self.root)
  try:
   for oid,kind,state in [('a','response','paid'),('b','offer','uncertain'),('c','offer','no_deal')]:
    m.begin(dict(order=oid));m.record(oid,'shop','sku','task',kind);m.state(oid,state)
   x=m.deal_estimate('shop');self.assertEqual(x['trials'],1);self.assertEqual(x['probability'],.4)
  finally:m.close()
 def test_conditions_do_not_learn_unwarranted_goods(self):
  m=Memory(self.root)
  try:
   m.begin(dict(order='a'));m.state('a','paid')
   with m.db:
    m.db.execute('INSERT INTO payments VALUES(?,?,?,?,?,?,?)',('a','shop','sku',8000,1,1,None))
    m.db.execute('INSERT INTO outcomes VALUES(?,?)',('a',json.dumps(dict(grade='B',warranty_hours=0))))
   self.assertIsNone(m.condition_reference('shop','sku',dict(kind='goods')))
   with m.db:m.db.execute('UPDATE outcomes SET details=?',(json.dumps(dict(grade='B',warranty_hours=6)),))
   self.assertEqual(m.condition_reference('shop','sku',dict(kind='goods'),'B'),8000)
   self.assertIsNone(m.condition_reference('shop','sku',dict(kind='goods'),'A'))
  finally:m.close()

if __name__=='__main__':unittest.main()
