import base64,datetime as dt,hashlib,hmac,json,multiprocessing,socket,time,unittest,urllib.request,urllib.error
from memberships import Config,State,Reconciler,serve
ADMIN='test-only-admin-token-at-least-32-characters'
SECRET='test-only-hook-secret-not-real'
class EmptyAPI:
    def validate(self,c):pass
    def members(self,c):return set()
def run(port):
    c=Config({'store':'example','live':False,'plans':{'monthly':{'product_id':1,'period':30,'category_id':10,'promotion_id':20,'category_name':'Membership App / Monthly'}}})
    s=State(':memory:',c);serve(c,s,Reconciler(s,EmptyAPI()),SECRET,ADMIN,port=port,interval=1)
class HTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as s:s.bind(('127.0.0.1',0));cls.port=s.getsockname()[1]
        cls.process=multiprocessing.Process(target=run,args=(cls.port,),daemon=True);cls.process.start()
        for _ in range(50):
            try:urllib.request.urlopen(f'http://127.0.0.1:{cls.port}/healthz',timeout=1);break
            except OSError:time.sleep(.1)
        else:raise RuntimeError('Server failed')
    @classmethod
    def tearDownClass(cls):cls.process.terminate();cls.process.join(5)
    def request(self,path='/',headers=None,body=None):
        req=urllib.request.Request(f'http://127.0.0.1:{self.port}'+path,data=body,headers=headers or {})
        try:
            with urllib.request.urlopen(req,timeout=3) as r:return r.status,r.read()
        except urllib.error.HTTPError as e:return e.code,e.read()
    def auth(self):return {'Authorization':'Basic '+base64.b64encode(('admin:'+ADMIN).encode()).decode()}
    def test_private_dashboard(self):self.assertEqual(401,self.request()[0]);self.assertEqual(200,self.request(headers=self.auth())[0])
    def test_unauthenticated_webhook(self):self.assertEqual(401,self.request('/webhooks/subscriptions',body=b'{}')[0])
    def test_signed_delivery_to_dashboard(self):
        current=dt.datetime.now(dt.timezone.utc);stamp=str(int(current.timestamp()));delivery='http-fixture'
        body=json.dumps({'product_subscription':{'id':1001,'customer':{'id':42},'product':{'id':1,'variant_id':None},'recurring_period':30,'status':'active','updated_at':current.isoformat(),'next_order_date':(current+dt.timedelta(days=20)).isoformat()}}).encode()
        signature=base64.b64encode(hmac.new(SECRET.encode(),stamp.encode()+b'.'+delivery.encode()+b'.'+body,hashlib.sha256).digest()).decode()
        headers={'Jumpseller-Timestamp':stamp,'Jumpseller-Delivery-Id':delivery,'Jumpseller-Signature':signature,'Jumpseller-Store-Code':'example'}
        self.assertEqual(202,self.request('/webhooks/subscriptions',headers,body)[0]);code,data=self.request('/api/members',self.auth());self.assertEqual(200,code);self.assertIn([42,'monthly'],json.loads(data)['active'])
        headers=self.auth();headers['X-Admin-Action']='suspend';self.assertEqual(202,self.request('/api/suspend',headers,b'{"subscription_id":1001}')[0]);self.assertEqual([],json.loads(self.request('/api/members',self.auth())[1])['active'])
