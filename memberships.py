"""Self-hosted Jumpseller membership entitlements. Python 3.12+ standard library."""
import base64, datetime as dt, hashlib, hmac, html, json, os, sqlite3, threading, time, urllib.request
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
UTC=dt.timezone.utc

def timestamp(value):
    if not isinstance(value,str): raise ValueError('Timestamp required')
    result=dt.datetime.fromisoformat(value.replace('Z','+00:00').replace(' UTC','+00:00'))
    if result.tzinfo is None: raise ValueError('Timezone required')
    return result.astimezone(UTC)

def now(): return dt.datetime.now(UTC)
def positive_id(value):
    if type(value) is not int or value<1: raise ValueError('Positive integer ID required')
    return value

class Config:
    def __init__(self,data):
        self.store=data['store'];self.plans=data['plans'];self.live=data.get('live',False)
        self.allow_local_readonly=data.get('allow_local_readonly',False)
        if type(self.allow_local_readonly) is not bool:raise ValueError('allow_local_readonly must be boolean')
        self.grace_days=data.get('grace_days',5)
        if type(self.grace_days) is not int or not 0<=self.grace_days<=30:raise ValueError('grace_days must be an integer from 0 to 30')
        if type(self.live) is not bool:raise ValueError('live must be a JSON boolean')
        if not isinstance(self.store,str) or not self.store:raise ValueError('Store required')
        if not self.plans:raise ValueError('At least one plan required')
        groups=set();selectors=set()
        for name,p in self.plans.items():
            positive_id(p['product_id']);positive_id(p['category_id']);positive_id(p['promotion_id'])
            if p['period'] not in (30,365):raise ValueError('Supported periods: 30 monthly, 365 annual')
            selector=(p['product_id'],p.get('variant_id'),p['period'])
            if selector in selectors or p['category_id'] in groups:raise ValueError('Plans need unique selector and dedicated category')
            selectors.add(selector);groups.add(p['category_id'])
            if not p['category_name'].startswith(('Membership App / ','Membership App TEST - ')):raise ValueError('Dedicated category prefix required')
    def plan(self,body):
        product=body['product']
        for name,p in self.plans.items():
            if (product['id'],product.get('variant_id'),body['recurring_period'])==(p['product_id'],p.get('variant_id'),p['period']):return name
        return None

class State:
    def __init__(self,path,config):
        self.config=config;self.lock=threading.RLock();self.db=sqlite3.connect(path,check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS subscriptions(id INTEGER PRIMARY KEY,customer INTEGER NOT NULL,plan TEXT NOT NULL,version TEXT NOT NULL,digest TEXT NOT NULL,status TEXT NOT NULL,valid_until TEXT,quarantine INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,digest TEXT NOT NULL,received_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,event_time TEXT NOT NULL,kind TEXT NOT NULL,subscription INTEGER,detail TEXT NOT NULL);
        ''')
        with self.db:
            saved=self.db.execute("SELECT value FROM metadata WHERE key='store'").fetchone()
            if saved and saved['value']!=config.store:raise ValueError('Database belongs to another store')
            self.db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('store',?)",(config.store,))
    def audit(self,kind,sid,detail):
        self.db.execute('INSERT INTO audit(event_time,kind,subscription,detail) VALUES(?,?,?,?)',(now().isoformat(),kind,sid,detail))
    def signed_delivery(self,raw,headers,secret,clock=None):
        clock=clock or now()
        sent=headers.get('Jumpseller-Timestamp','');delivery=headers.get('Jumpseller-Delivery-Id','')
        if not sent.isdigit() or abs(clock.timestamp()-int(sent))>300 or not 1<=len(delivery)<=128:
            raise PermissionError('Stale or invalid delivery')
        signature=headers.get('Jumpseller-Signature',headers.get('Jumpseller-Hmac-Sha256-V2',''))
        expected=base64.b64encode(hmac.new(secret.encode(),sent.encode()+b'.'+delivery.encode()+b'.'+raw,hashlib.sha256).digest()).decode()
        if not hmac.compare_digest(expected,signature):raise PermissionError('Invalid signature')
        digest=hashlib.sha256(raw).hexdigest()
        with self.lock:
            existing=self.db.execute('SELECT digest FROM deliveries WHERE id=?',(delivery,)).fetchone()
            if existing:
                if existing['digest']!=digest:raise PermissionError('Conflicting delivery ID')
                return 'duplicate_delivery'
            legacy=base64.b64encode(hmac.new(secret.encode(),raw,hashlib.sha256).digest()).decode()
            result=self.receive(raw,legacy,secret,headers.get('Jumpseller-Store-Code',''),clock)
            with self.db:self.db.execute('INSERT INTO deliveries(id,digest,received_at) VALUES(?,?,?)',(delivery,digest,clock.isoformat()))
            return result
    def suspend(self,sid):
        with self.lock,self.db:
            self.db.execute('UPDATE subscriptions SET quarantine=1 WHERE id=?',(positive_id(sid),))
            self.audit('manual_suspend',sid,'Administrator suspended benefits')
    def receive(self,raw,signature,secret,store,clock=None):
        clock=clock or now()
        expected=base64.b64encode(hmac.new(secret.encode(),raw,hashlib.sha256).digest()).decode()
        if not hmac.compare_digest(expected,signature or '') or store!=self.config.store:raise PermissionError('Invalid webhook authentication')
        payload=json.loads(raw)
        if not isinstance(payload,dict):raise ValueError('Object payload required')
        body=payload.get('product_subscription',payload.get('subscription',payload))
        if not isinstance(body,dict):raise ValueError('Object subscription required')
        sid=positive_id(body['id'])
        customer_missing=body.get('customer') is None
        if customer_missing:
            if body.get('status')!='canceled':raise ValueError('Customer required')
            with self.lock:known=self.db.execute('SELECT customer FROM subscriptions WHERE id=?',(sid,)).fetchone()
            if not known:return 'ignored_deleted_customer'
            customer=known['customer']
        else:customer=positive_id(body['customer']['id'])
        plan=self.config.plan(body)
        if plan is None:
            with self.lock,self.db:
                old=self.db.execute('SELECT version FROM subscriptions WHERE id=?',(sid,)).fetchone()
                if old and timestamp(body['updated_at'])>=timestamp(old['version']):
                    self.db.execute('UPDATE subscriptions SET quarantine=1 WHERE id=?',(sid,));self.audit('unmapped_change',sid,'Configured subscription changed to unmapped plan');return 'quarantined'
            return 'ignored_unmapped_product'
        status=body['status'];version=timestamp(body['updated_at'])
        if status not in ('active','pending','canceled'):raise ValueError('Unknown subscription status')
        if version>clock+dt.timedelta(minutes=5):raise ValueError('Future event timestamp')
        end=timestamp(body['next_order_date']) if body.get('next_order_date') else None
        # A native active status alone is insufficient: require a bounded authoritative next date.
        if status=='active' and end and end>clock+dt.timedelta(days=370):raise ValueError('Implausible next_order_date')
        digest=hashlib.sha256(json.dumps([sid,customer,plan,status,version.isoformat(),end.isoformat() if end else None]).encode()).hexdigest()
        with self.lock,self.db:
            old=self.db.execute('SELECT * FROM subscriptions WHERE id=?',(sid,)).fetchone()
            if old:
                prior=timestamp(old['version'])
                if version<prior:return 'stale'
                if version==prior:
                    if digest==old['digest']:return 'duplicate'
                    # Native creation and dated update may share timestamp precision.
                    # Only enrich null->dated active for the exact same identity; never extend an existing lease.
                    if old['customer']==customer and old['plan']==plan and old['status']==status=='active' and not old['quarantine']:
                        if old['valid_until'] and end is None:return 'superseded_initial'
                        if old['valid_until'] is None and end is not None:
                            self.db.execute('UPDATE subscriptions SET digest=?,valid_until=? WHERE id=?',(digest,end.isoformat(),sid))
                            self.audit('accepted',sid,json.dumps({'native_status':'active','native_updated_at':version.isoformat(),'paid_through':end.isoformat(),'basis':'authenticated_subscription_event'}));return 'accepted'
                    self.db.execute('UPDATE subscriptions SET quarantine=1 WHERE id=?',(sid,));self.audit('conflict',sid,'Equal timestamp, different payload; access suspended');return 'quarantined'
                if old['customer']!=customer or old['plan']!=plan:
                    self.db.execute('UPDATE subscriptions SET quarantine=1 WHERE id=?',(sid,));self.audit('identity_conflict',sid,'Identity or plan changed; access suspended');return 'quarantined'
                if old['quarantine']:return 'quarantined'
                # Cancellation stops renewal but preserves a previously signed paid window.
                # Never derive a new paid window from cancellation or pending events.
                if status=='canceled':end=timestamp(old['valid_until']) if not customer_missing and old['status'] in ('active','pending','canceled') and old['valid_until'] else None
            elif status=='canceled':end=None
            if status=='pending' or (status=='active' and end is None):end=timestamp(old['valid_until']) if old and old['valid_until'] else None
            self.db.execute('INSERT OR REPLACE INTO subscriptions(id,customer,plan,version,digest,status,valid_until,quarantine) VALUES(?,?,?,?,?,?,?,0)',(sid,customer,plan,version.isoformat(),digest,status,end.isoformat() if end else None))
            self.audit('accepted',sid,json.dumps({'native_status':status,'native_updated_at':version.isoformat(),'paid_through':end.isoformat() if end else None,'basis':'authenticated_subscription_event'}))
        return 'accepted'
    def entitlement(self,row,clock=None):
        clock=clock or now()
        end=timestamp(row['valid_until']) if row['valid_until'] else None
        # Canceling future billing preserves only the paid period, not renewal grace.
        grace=end+dt.timedelta(days=self.config.grace_days) if end and row['status']!='canceled' else end
        if row['quarantine']:status='suspended'
        elif not end:status='awaiting_payment'
        elif clock<end:status='active'
        elif clock<grace:status='grace'
        else:status='expired'
        return {'benefit_state':status,'benefit_active':status in ('active','grace'),
                'paid_through':end.isoformat() if end else None,
                'grace_until':grace.isoformat() if grace else None}
    def active(self,clock=None):
        with self.lock:
            return {(r['customer'],r['plan']) for r in self.rows() if self.entitlement(r,clock)['benefit_active']}
    def rows(self):
        with self.lock:return [dict(r) for r in self.db.execute('SELECT id,customer,plan,status,version,valid_until,quarantine FROM subscriptions ORDER BY id')]
    def evidence(self,sid):
        with self.lock:return [dict(r) for r in self.db.execute('SELECT event_time,kind,detail FROM audit WHERE subscription=? ORDER BY id DESC LIMIT 20',(sid,))]

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):raise ValueError('API redirect refused')

class StoreAPI:
    def __init__(self,login,token):
        self.auth='Basic '+base64.b64encode((login+':'+token).encode()).decode()
    def request(self,method,path,data=None):
        request=urllib.request.Request('https://api.jumpseller.com/v1/'+path,method=method,data=json.dumps(data).encode() if data is not None else None,headers={'Authorization':self.auth,'Content-Type':'application/json','User-Agent':'JumpsellerMemberships/0.1'})
        with urllib.request.build_opener(NoRedirect()).open(request,timeout=15) as response:return json.load(response)
    def validate(self,config):
        self.verified_promotions={}
        if self.request('GET','store/info.json')['store']['code']!=config.store:raise ValueError('Wrong API store')
        for p in config.plans.values():
            c=self.request('GET',f'customer_categories/{p["category_id"]}.json')['customer_category']
            if c['name']!=p['category_name']:raise ValueError('Category ownership check failed')
            promo=self.request('GET',f'promotions/{p["promotion_id"]}.json')['promotion']
            if promo['customers']!='categories' or promo['customer_categories']!=[{'id':p['category_id']}] or promo['cumulative']:raise ValueError('Unsafe promotion targeting')
            if config.live and not promo['enabled']:raise ValueError('Live plan promotion is disabled')
            self.verified_promotions[p['promotion_id']]={k:promo.get(k) for k in ('id','name','enabled','discount_amount_percent','discount_amount_fix','type')}
    def members(self,category):
        result=set()
        for page in range(1,10001):
            batch=self.request('GET',f'customer_categories/{category}/customers.json?limit=100&page={page}')
            if not isinstance(batch,list):raise ValueError('Unexpected customer response')
            result.update(positive_id(x['customer']['id']) for x in batch)
            if len(batch)<100:return result
        raise ValueError('Pagination safety limit')
    def add(self,category,customer):return self.request('POST',f'customer_categories/{category}/customers.json',{'customers':[{'id':customer}]})
    def remove(self,category,customer):return self.request('DELETE',f'customer_categories/{category}/customers/{customer}.json')

class Reconciler:
    def __init__(self,state,api):self.state=state;self.api=api;self.lock=threading.Lock();self.last_success=None;self.error=None;self.last_actions=[];self.observed={}
    def sync(self,clock=None):
        with self.lock:
            try:
                self.api.validate(self.state.config)
                actions=[]
                # Read all groups before any mutation. Serialize the final entitlement snapshot
                # and writes with event acceptance so no acknowledged revocation is overwritten.
                actual={name:self.api.members(p['category_id']) for name,p in self.state.config.plans.items()}
                with self.state.lock:
                    desired=self.state.active(clock)
                    for name,p in self.state.config.plans.items():
                        want={customer for customer,plan in desired if plan==name};have=actual[name]
                        for customer in sorted(have-want):actions.append(('remove',p['category_id'],customer))
                        for customer in sorted(want-have):actions.append(('add',p['category_id'],customer))
                    if self.state.config.live:
                        for method,group,customer in actions:
                            getattr(self.api,method)(group,customer)
                        for name,p in self.state.config.plans.items():
                            if self.api.members(p['category_id'])!={c for c,n in desired if n==name}:raise ValueError('Store write verification failed')
                self.observed={name:sorted(self.api.members(p['category_id'])) for name,p in self.state.config.plans.items()}
                self.last_actions=actions;self.last_success=now().isoformat();self.error=None
                return actions
            except Exception as exc:
                self.error=type(exc).__name__;raise

def local_read_allowed(client_host,headers,port):
    hosts={f'127.0.0.1:{port}',f'localhost:{port}'}
    if client_host not in ('127.0.0.1','::1') or headers.get('Host') not in hosts:return False
    if any(headers.get(k) is not None for k in ('Forwarded','X-Forwarded-For','X-Forwarded-Host','X-Forwarded-Proto')):return False
    if headers.get('Origin') is not None and headers.get('Origin') not in {'http://'+h for h in hosts}:return False
    if headers.get('Sec-Fetch-Site') not in (None,'none','same-origin'):return False
    return True

def serve(config,state,reconciler,secret,admin_token,host='127.0.0.1',port=4248,interval=30):
    if config.allow_local_readonly and host not in ('127.0.0.1','::1'):raise ValueError('Password-free reads require loopback bind')
    if len(secret)<16 or len(admin_token)<32:raise ValueError('Strong hook secret/admin token required')
    from renewal_audit import RenewalAuditor
    from dashboard import snapshot,render
    auditor=RenewalAuditor(state,reconciler.api)
    wake=threading.Event()
    def worker():
        while True:
            try:reconciler.sync()
            except Exception:pass # surfaced in authenticated dashboard / readiness, no secret-bearing exception logs
            wake.wait(interval);wake.clear()
    def audit_worker():
        while True:
            try:auditor.run_due()
            except Exception:pass
            time.sleep(60)
    class Handler(BaseHTTPRequestHandler):
        def setup(self):super().setup();self.connection.settimeout(10)
        def log_message(self,*args):pass
        def send(self,code,body,kind='application/json'):
            body=body.encode();self.send_response(code);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'");self.end_headers();self.wfile.write(body)
        def authorized(self):
            expected='Basic '+base64.b64encode(('admin:'+admin_token).encode()).decode()
            return hmac.compare_digest(self.headers.get('Authorization',''),expected)
        def do_GET(self):
            if self.path=='/healthz':return self.send(200,'{"alive":true}')
            local_read=config.allow_local_readonly and local_read_allowed(self.client_address[0],self.headers,port)
            if not self.authorized() and not local_read:
                self.send_response(401);self.send_header('WWW-Authenticate','Basic realm="Memberships"');self.send_header('Content-Length','0');self.end_headers();return
            if self.path not in ('/','/api/members'):return self.send(404,'{}')
            data=snapshot(state,reconciler,auditor)
            if self.path=='/api/members':return self.send(200,json.dumps(data))
            self.send(200,render(data),'text/html; charset=utf-8')
        def do_POST(self):
            if self.path=='/api/suspend':
                if not self.authorized() or self.headers.get('X-Admin-Action')!='suspend':return self.send(403,'{}')
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<1024:return self.send(413,'{}')
                    state.suspend(json.loads(self.rfile.read(length))['subscription_id']);wake.set();return self.send(202,'{}')
                except (ValueError,KeyError,TypeError):return self.send(400,'{}')
            if self.path!='/webhooks/subscriptions':return self.send(404,'{}')
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=131072:return self.send(413,'{}')
                result=state.signed_delivery(self.rfile.read(length),self.headers,secret)
                wake.set();self.send(202,json.dumps({'result':result}))
            except PermissionError:self.send(401,'{}')
            except (ValueError,KeyError,TypeError):self.send(400,'{"error":"invalid event"}')
    threading.Thread(target=worker,daemon=True).start()
    threading.Thread(target=audit_worker,daemon=True).start()
    ThreadingHTTPServer((host,port),Handler).serve_forever()

if __name__=='__main__':
    import fcntl
    config=Config(json.loads(Path(os.environ.get('CONFIG_FILE','config.json')).read_text()))
    database=Path(os.environ.get('DATABASE_FILE','data/memberships.sqlite'));database.parent.mkdir(parents=True,exist_ok=True)
    lockfile=open(str(database)+'.lock','w');fcntl.flock(lockfile,fcntl.LOCK_EX|fcntl.LOCK_NB)
    state=State(str(database),config);api=StoreAPI(os.environ['JUMPSELLER_LOGIN'],os.environ['JUMPSELLER_TOKEN'])
    serve(config,state,Reconciler(state,api),os.environ['JUMPSELLER_HOOK_SECRET'],os.environ['ADMIN_TOKEN'],os.environ.get('HOST','127.0.0.1'),int(os.environ.get('PORT','4248')))
