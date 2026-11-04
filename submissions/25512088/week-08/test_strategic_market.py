import json,sqlite3,tempfile,time,unittest
from pathlib import Path
from strategic_market import rank_products,owned_targets,restrict_order,snapshot

class StrategyTests(unittest.TestCase):
 def test_selection_waits_until_three_minutes_before_supply(self):
  self.assertIsNone(snapshot('/must-not-be-created',{},time.time()+1000,'https://unused'))
 def test_top10_requires_demand_nonfood_and_prefers_short_supply(self):
  cat={str(i):dict(kind='goods',name=str(i),list_price=500000) for i in range(15)}
  cat['food']=dict(kind='food',name='food',list_price=900000)
  trades=[dict(sku=k,price=400000,warranty=6,honest=1) for k in cat for _ in range(5)]
  shops=[dict(name='other',url='other',stock={'0':100}),dict(name='25512088',url='own',stock={'1':100})]
  rows=rank_products(cat,trades,shops)
  self.assertEqual(len(rows),10)
  self.assertNotIn('food',[r['sku'] for r in rows]);self.assertNotIn('0',[r['sku'] for r in rows])
  self.assertEqual(next(r for r in rows if r['sku']=='1')['competitor_units'],0)
  self.assertEqual(rank_products(cat,trades[:1],shops),[])
 def test_buyer_intersection_held_paid_not_signed_and_not_food(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);inv=dict(round=3,units=[dict(unit='u',sku='X',kind='goods',grade='A'),dict(unit='v',sku='Y',kind='goods',grade='B')])
   (p/'inventory.json').write_text(json.dumps(inv))
   with sqlite3.connect(p/'procurement.sqlite3') as db:
    db.execute('create table purchases(sku,status)');db.executemany('insert into purchases values(?,?)',[('X','paid'),('Y','sent')])
   self.assertEqual(owned_targets(p),{'X'})
   self.assertEqual(restrict_order(dict(kind='goods',acceptable=['X','Z']),p)['acceptable'],['X'])
   self.assertEqual(restrict_order(dict(kind='food',acceptable=['X']),p)['acceptable'],[])
   self.assertEqual(owned_targets(p,time.time()+130),set())
   with sqlite3.connect(p/'referrals.sqlite3') as db:
    db.execute('create table sales(unit)');db.execute("insert into sales values('u')")
   self.assertEqual(owned_targets(p),set())
if __name__=='__main__':unittest.main()
