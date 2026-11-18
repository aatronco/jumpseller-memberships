import base64,datetime as dt,hashlib,hmac,json,os,tempfile,unittest
from memberships import Config,State,Reconciler,timestamp
SECRET='test-only-not-a-production-secret'
CLOCK=timestamp('2026-11-16T12:00:00Z')
def configuration(live=False):return Config({'store':'example','grace_days':0,'live':live,'plans':{'monthly':{'product_id':1,'period':30,'category_id':10,'promotion_id':20,'category_name':'Membership App / Monthly'}}})
def event(**overrides):
    d={'id':1001,'customer':{'id':42},'product':{'id':1,'variant_id':None},'status':'active','recurring_period':30,'updated_at':'2026-11-16T11:00:00Z','next_order_date':'2026-12-16T00:00:00Z'};d.update(overrides);return d
class Events(unittest.TestCase):
    def setUp(self):self.s=State(':memory:',configuration())
    def receive(self,e=None):
        raw=json.dumps(e or event()).encode();signature=base64.b64encode(hmac.new(SECRET.encode(),raw,hashlib.sha256).digest()).decode()
        return self.s.receive(raw,signature,SECRET,'example',CLOCK)
    def test_initial_active(self):self.receive();self.assertEqual({(42,'monthly')},self.s.active(CLOCK))
    def test_pending(self):self.receive(event(status='pending',next_order_date=None));self.assertFalse(self.s.active(CLOCK))
    def test_active_without_end(self):
        self.receive(event(next_order_date=None));self.assertFalse(self.s.active(CLOCK));self.assertEqual('accepted',self.receive());self.assertEqual({(42,'monthly')},self.s.active(CLOCK))
    def test_expired_active(self):self.receive(event(next_order_date='2026-11-16T12:00:00Z'));self.assertFalse(self.s.active(CLOCK))
    def test_duplicate(self):self.receive();self.assertEqual('duplicate',self.receive())
    def test_stale_cannot_restore(self):
        self.receive(event(status='pending',updated_at='2026-11-16T11:30:00Z',next_order_date=None));self.assertEqual('stale',self.receive());self.assertFalse(self.s.active(CLOCK))
    def test_equal_timestamp_conflict(self):
        self.receive();self.assertEqual('quarantined',self.receive(event(status='pending',next_order_date=None)));self.assertFalse(self.s.active(CLOCK))
    def test_cancel_preserves_paid_window(self):
        self.receive();self.receive(event(status='canceled',next_order_date=None,updated_at='2026-11-16T11:30:00Z'));self.assertEqual({(42,'monthly')},self.s.active(CLOCK));self.assertFalse(self.s.active(timestamp('2026-12-16T00:00:00Z')))
    def test_unknown_cancel_no_access(self):self.receive(event(status='canceled',next_order_date=None));self.assertFalse(self.s.active(CLOCK))
    def test_unmapped_product(self):self.assertEqual('ignored_unmapped_product',self.receive(event(product={'id':2})))
    def test_wrong_store(self):
        with self.assertRaises(PermissionError):self.s.receive(b'{}','',SECRET,'other',CLOCK)
    def test_invalid_signature(self):
        with self.assertRaises(PermissionError):self.s.receive(b'{}','bad',SECRET,'example',CLOCK)
    def test_future(self):
        with self.assertRaises(ValueError):self.receive(event(updated_at='2027-01-01T00:00:00Z'))
    def test_customer_change_quarantined(self):
        self.receive();self.receive(event(customer={'id':99},updated_at='2026-11-16T11:30:00Z'));self.assertFalse(self.s.active(CLOCK))
    def test_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            self.s=State(os.path.join(d,'state.db'),configuration());self.receive();other=State(os.path.join(d,'state.db'),configuration());self.assertEqual({(42,'monthly')},other.active(CLOCK))
class FakeAPI:
    def __init__(self):self.groups={10:{99}};self.calls=[]
    def validate(self,c):pass
    def members(self,g):return self.groups[g].copy()
    def add(self,g,c):self.calls.append(('add',g,c));self.groups[g].add(c)
    def remove(self,g,c):self.calls.append(('remove',g,c));self.groups[g].remove(c)
class Synchronization(unittest.TestCase):
    def state(self,live):
        s=State(':memory:',configuration(live));raw=json.dumps(event()).encode();sig=base64.b64encode(hmac.new(SECRET.encode(),raw,hashlib.sha256).digest()).decode();s.receive(raw,sig,SECRET,'example',CLOCK);return s
    def test_dry_run_no_write(self):
        api=FakeAPI();sync=Reconciler(self.state(False),api);self.assertEqual([('remove',10,99),('add',10,42)],sync.sync(CLOCK));self.assertEqual([],api.calls)
    def test_write_and_idempotency(self):
        api=FakeAPI();sync=Reconciler(self.state(True),api);sync.sync(CLOCK);self.assertEqual({42},api.groups[10]);self.assertEqual([],sync.sync(CLOCK))
    def test_expiry_removes(self):
        api=FakeAPI();sync=Reconciler(self.state(True),api);sync.sync(CLOCK);sync.sync(timestamp('2026-12-16T00:00:00Z'));self.assertEqual(set(),api.groups[10])
    def test_write_verified(self):
        api=FakeAPI();api.add=lambda *a:None
        with self.assertRaises(ValueError):Reconciler(self.state(True),api).sync(CLOCK)
    def test_read_failure_does_not_mutate(self):
        api=FakeAPI();api.members=lambda *a:(_ for _ in ()).throw(ValueError('bad'))
        with self.assertRaises(ValueError):Reconciler(self.state(True),api).sync(CLOCK)
        self.assertEqual([],api.calls)
if __name__=='__main__':unittest.main()

class SignedDelivery(unittest.TestCase):
    def setUp(self):self.s=State(':memory:',configuration());self.raw=json.dumps(event()).encode()
    def headers(self,stamp=None,delivery='test-delivery'):
        stamp=str(int(CLOCK.timestamp())) if stamp is None else stamp
        signature=base64.b64encode(hmac.new(SECRET.encode(),stamp.encode()+b'.'+delivery.encode()+b'.'+self.raw,hashlib.sha256).digest()).decode()
        return {'Jumpseller-Store-Code':'example','Jumpseller-Timestamp':stamp,'Jumpseller-Delivery-Id':delivery,'Jumpseller-Signature':signature}
    def test_valid_v2(self):self.assertEqual('accepted',self.s.signed_delivery(self.raw,self.headers(),SECRET,CLOCK))
    def test_delivery_dedup(self):
        self.s.signed_delivery(self.raw,self.headers(),SECRET,CLOCK);self.assertEqual('duplicate_delivery',self.s.signed_delivery(self.raw,self.headers(),SECRET,CLOCK))
    def test_stale_rejected(self):
        with self.assertRaises(PermissionError):self.s.signed_delivery(self.raw,self.headers(str(int(CLOCK.timestamp())-301)),SECRET,CLOCK)
    def test_body_tamper(self):
        with self.assertRaises(PermissionError):self.s.signed_delivery(self.raw+b' ',self.headers(),SECRET,CLOCK)
    def test_delivery_tamper(self):
        headers=self.headers();headers['Jumpseller-Delivery-Id']='different'
        with self.assertRaises(PermissionError):self.s.signed_delivery(self.raw,headers,SECRET,CLOCK)
    def test_manual_suspend(self):
        self.s.signed_delivery(self.raw,self.headers(),SECRET,CLOCK);self.s.suspend(1001);self.assertFalse(self.s.active(CLOCK))

class AdditionalSafety(unittest.TestCase):
    setUp=Events.setUp
    receive=Events.receive
    def test_unmapped_change_removes_benefit(self):
        self.receive();self.assertEqual('quarantined',self.receive(event(product={'id':9},updated_at='2026-11-16T11:30:00Z')));self.assertFalse(self.s.active(CLOCK))
    def test_stale_unmapped_does_not_revoke(self):
        self.receive();self.receive(event(product={'id':9},updated_at='2026-11-16T10:00:00Z'));self.assertTrue(self.s.active(CLOCK))
    def test_null_after_dated_same_time_is_superseded(self):
        self.receive();self.assertEqual('superseded_initial',self.receive(event(next_order_date=None)));self.assertTrue(self.s.active(CLOCK))
    def test_live_boolean(self):
        with self.assertRaises(ValueError):Config({'store':'example','live':'false','plans':configuration().plans})
    def test_database_store_binding(self):
        with tempfile.TemporaryDirectory() as d:
            State(os.path.join(d,'state'),configuration());c=configuration();c.store='other'
            with self.assertRaises(ValueError):State(os.path.join(d,'state'),c)

class AdditionalDelivery(unittest.TestCase):
    setUp=SignedDelivery.setUp
    headers=SignedDelivery.headers
    def test_alias(self):
        headers=self.headers();headers['Jumpseller-Hmac-Sha256-V2']=headers.pop('Jumpseller-Signature');self.assertEqual('accepted',self.s.signed_delivery(self.raw,headers,SECRET,CLOCK))
    def test_retry_new_delivery_same_body(self):
        self.s.signed_delivery(self.raw,self.headers(),SECRET,CLOCK);self.assertEqual('duplicate',self.s.signed_delivery(self.raw,self.headers(delivery='retry'),SECRET,CLOCK))

class TimestampFormats(unittest.TestCase):
    def test_rails_utc(self):self.assertEqual(timestamp('2026-11-16T12:00:00Z'),timestamp('2026-11-16 12:00:00 UTC'))
    def test_naive_rejected(self):
        with self.assertRaises(ValueError):timestamp('2026-11-16 12:00:00')

class DeletedCustomer(unittest.TestCase):
    setUp=Events.setUp
    receive=Events.receive
    def test_deleted_customer_cancel_revokes(self):
        self.receive();self.receive(event(customer=None,status='canceled',next_order_date=None,updated_at='2026-11-16T11:30:00Z'));self.assertFalse(self.s.active(CLOCK))
    def test_unknown_deleted_customer_ignored(self):self.assertEqual('ignored_deleted_customer',self.receive(event(customer=None,status='canceled',next_order_date=None)))

class RenewalLease(unittest.TestCase):
    setUp=Events.setUp
    receive=Events.receive
    def test_paid_renewal_replaces_expiration(self):
        self.receive(event(next_order_date='2026-11-16T13:00:00Z'))
        self.receive(event(updated_at='2026-11-16T11:30:00Z',next_order_date='2026-12-16T03:00:00Z'))
        self.assertEqual({(42,'monthly')},self.s.active(timestamp('2026-11-17T00:00:00Z')))
        self.assertEqual('2026-12-16T03:00:00+00:00',self.s.rows()[0]['valid_until'])
    def test_old_paid_event_cannot_extend_after_pending(self):
        self.receive();self.receive(event(status='pending',updated_at='2026-11-16T11:30:00Z',next_order_date=None))
        self.assertEqual('stale',self.receive());self.assertTrue(self.s.active(CLOCK));self.assertFalse(self.s.active(timestamp('2026-12-16T00:00:00Z')))
