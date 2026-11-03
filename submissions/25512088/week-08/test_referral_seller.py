import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import httpx
import referral_seller
from referral_program import Program


class SellerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_a2a_quote_signed_artifact_and_private_enrollment(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state/'inventory.json').write_text(json.dumps({'round':3, 'units': [dict(
                unit='u1', sku='G00001', name='김치', category='김치', cost=8000,
                list_price=10000, grade='A', expires_at=time.time()+86400, supplied_at=time.time())]}))
            program = Program(state)
            program.put('cursor', 100)
            program.close()
            with patch.object(referral_seller, 'STATE', state), patch.object(referral_seller, 'sign', side_effect=lambda r: dict(r, sig_seller='test-only')):
                app = referral_seller.build(9301, publish=False)
                # ASGI transport deliberately does not run lifespan/network worker.
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    async def send(text, task=None):
                        msg = {'role':'ROLE_USER', 'messageId':str(time.time_ns()), 'parts':[{'text':text}]}
                        if task:
                            msg['taskId'] = task
                        response = await client.post('/', headers={'A2A-Version':'1.0'}, json={
                            'jsonrpc':'2.0','id':'1','method':'SendMessage','params':{'message':msg}})
                        self.assertEqual(response.status_code, 200)
                        data = response.json()
                        self.assertNotIn('error', data, data)
                        return data['result'].get('task', data['result'])
                    result = await send('I want to buy G00001')
                    task = result['id']
                    result = await send('9500', task)
                    self.assertIn('COMPLETED', result['status']['state'])
                    artifacts = json.dumps(result['artifacts'])
                    self.assertIn('sig_seller', artifacts)
                    self.assertNotIn('member', artifacts)
                    self.assertIn('Private provisional member key', json.dumps(result))
                    self.assertIn('Public referral code: REF-',json.dumps(result))
                    self.assertIn('SHARE member=',json.dumps(result))
                    discovery=await send('OFFERS')
                    self.assertIn('COMPLETED',discovery['status']['state'])
                    self.assertNotIn('Private provisional',json.dumps(discovery))
                    rejected=await send('SHARE member=invalid')
                    self.assertIn('requires your private member key',json.dumps(rejected))
                    program = Program(state)
                    self.assertEqual(program.db.execute('SELECT COUNT(*) FROM sales WHERE ledger IS NULL').fetchone()[0], 1)
                    self.assertEqual(program.db.execute('SELECT COUNT(*) FROM members WHERE active=1').fetchone()[0], 0)
                    program.close()


if __name__ == '__main__':
    unittest.main()
