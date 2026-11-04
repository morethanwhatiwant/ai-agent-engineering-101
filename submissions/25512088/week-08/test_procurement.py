import unittest
from procurement_campaign import choose
class PurchaseTests(unittest.TestCase):
 def setUp(self):
  self.p={'budget':2500000,'cash_floor':500000,'priority':['a','b','c','d']}
  self.cat={k:dict(kind='goods',list_price=400000) for k in 'abcd'}
 def test_cash_and_cumulative_budget_not_stock_count(self):
  self.assertEqual(choose(self.p,self.cat,0,0,4782055,0,0),('a',300000))
  self.assertIsNone(choose(self.p,self.cat,2300000,0,4782055,0,0))
  self.assertIsNone(choose(self.p,self.cat,0,2300000,4782055,0,0))
  self.assertIsNone(choose(self.p,self.cat,0,0,700000,0,0))
 def test_priority_rotation_and_food_exclusion(self):
  self.assertEqual(choose(self.p,self.cat,0,0,4782055,0,1)[0],'b')
  self.cat['b']['kind']='food'
  self.assertEqual(choose(self.p,self.cat,0,0,4782055,0,1)[0],'c')
 def test_refund_exposure_preserved(self):
  self.assertIsNone(choose(self.p,self.cat,0,0,1000000,250000,0))
 def test_sale_releases_inventory_budget_without_resetting_lifetime_spend(self):
  # Current inventory falls after settlement even if historical spend exceeds budget.
  self.assertIsNone(choose(self.p,self.cat,2400000,0,5000000,0,0))
  self.assertEqual(choose(self.p,self.cat,1900000,0,5000000,0,0),('a',300000))
 def test_reward_and_warranty_liabilities_limit_cash(self):
  self.assertIsNone(choose(self.p,self.cat,0,0,1000000,300000,0))
if __name__=='__main__':unittest.main()
