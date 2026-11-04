"""eWeLink OAuth and single-switch Sonoff support. Secrets stay on the Pi."""
import base64,hashlib,hmac,json,os,secrets,string,threading,time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from iot_driver import IoTError,check_bool

HOSTS={region:'https://'+region+'-apia.coolkit.'+('cn' if region=='cn' else 'cc') for region in ('eu','us','as','cn')}
def sign(secret,message):return base64.b64encode(hmac.new(secret.encode(),message.encode(),hashlib.sha256).digest()).decode()

class Ewelink:
    def __init__(self,directory=None):
        self.directory=Path(directory or os.environ.get('HANZHUB_EWELINK_DATA_DIR','/var/lib/hanzhub-ewelink'))
        self.lock=threading.RLock();self.last_call=0;self.cached=[];self.cached_at=0
    def read(self,name):
        try:return json.loads((self.directory/name).read_text())
        except (OSError,ValueError):return {}
    def write(self,name,data):
        self.directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        target=self.directory/name;tmp=target.with_suffix('.tmp')
        fd=os.open(tmp,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w') as f:json.dump(data,f)
        tmp.replace(target);target.chmod(0o600)
    def status(self):
        return {'configured':bool(self.read('client.json').get('app_secret')),'connected':bool(self.read('token.json').get('accessToken'))}
    def login(self):
        with self.lock:
            client=self.read('client.json')
            if not client.get('app_secret'):raise IoTError('Chybí aplikace eWeLink.',409,'ewelink_setup')
            state=secrets.token_urlsafe(32);seq=str(int(time.time()*1000))
            self.write('oauth-state.json',{'state':state,'expires':time.time()+600})
            params={'clientId':client['app_id'],'seq':seq,'authorization':sign(client['app_secret'],client['app_id']+'_'+seq),'redirectUrl':client['redirect_url'],'grantType':'authorization_code','state':state,'nonce':''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(8)),'showQRCode':'true'}
            return 'https://c2ccdn.coolkit.cc/oauth/index.html?'+urlencode(params)
    def request(self,path,payload=None,region=None,token=None,signed=False):
        # Global client lock serializes calls and token refresh. Stay below the API rate limit.
        with self.lock:
            time.sleep(max(0,.65-(time.monotonic()-self.last_call)))
            client=self.read('client.json');saved=self.read('token.json');region=region or saved.get('region','eu')
            if region not in HOSTS:raise IoTError('Neplatný region eWeLink.')
            body=json.dumps(payload,separators=(',',':')) if payload is not None else None
            auth='Sign '+sign(client['app_secret'],body or '') if signed else 'Bearer '+(token or saved.get('accessToken',''))
            headers={'X-CK-Appid':client['app_id'],'Authorization':auth,'Content-Type':'application/json','X-CK-Nonce':secrets.token_hex(4)}
            try:
                with urlopen(Request(HOSTS[region]+path,data=body.encode() if body is not None else None,headers=headers),timeout=15) as r:result=json.load(r)
            except (OSError,ValueError,KeyError):raise IoTError('Cloud eWeLink není dostupný.',502,'ewelink_unavailable') from None
            finally:self.last_call=time.monotonic()
            if not isinstance(result,dict):raise IoTError('Neplatná odpověď eWeLink.',502,'ewelink_response')
            if result.get('error')!=0:
                code=result.get('error')
                if code in (401,402):raise IoTError('Obnov přihlášení k eWeLink.',401,'ewelink_auth')
                raise IoTError('eWeLink odmítl požadavek (kód '+str(code)+').',502,'ewelink_error')
            return result.get('data') or {}
    def callback(self,query):
        with self.lock:
            state=self.read('oauth-state.json');provided=query.get('state',[''])[0]
            if not provided or time.time()>state.get('expires',0) or not hmac.compare_digest(provided,state.get('state','')):raise IoTError('Přihlášení vypršelo. Spusť připojení znovu.',400,'ewelink_state')
            self.write('oauth-state.json',{})
            code=query.get('code',[''])[0];region=query.get('region',['eu'])[0]
            if not code or region not in HOSTS:raise IoTError('eWeLink nevrátil platné přihlášení.')
            client=self.read('client.json')
            data=self.request('/v2/user/oauth/token',{'code':code,'redirectUrl':client['redirect_url'],'grantType':'authorization_code'},region=region,signed=True)
            if not data.get('accessToken') or not data.get('refreshToken'):raise IoTError('eWeLink nevrátil přístupový token.',502)
            self.write('token.json',{**data,'region':region});self.cached_at=0
    def ensure_token(self):
        with self.lock:
            saved=self.read('token.json')
            if not saved.get('accessToken'):raise IoTError('Nejdřív připoj účet eWeLink.',409,'ewelink_auth')
            expires=saved.get('atExpiredTime',0)
            if isinstance(expires,(int,float)) and time.time()*1000>=expires-300000:
                data=self.request('/v2/user/refresh',{'rt':saved['refreshToken']})
                at=data.get('at') or data.get('accessToken');rt=data.get('rt') or data.get('refreshToken')
                if not at or not rt:raise IoTError('Obnov přihlášení k eWeLink.',401,'ewelink_auth')
                saved.update(accessToken=at,refreshToken=rt,atExpiredTime=data.get('atExpiredTime',int((time.time()+29*86400)*1000)))
                self.write('token.json',saved)
            return saved
    @staticmethod
    def power(params):
        if params.get('switch') in ('on','off'):return params['switch']=='on'
        values=params.get('switches')
        if isinstance(values,list) and len(values)==1 and values[0].get('switch') in ('on','off'):return values[0]['switch']=='on'
        return None
    def things(self,force=False):
        with self.lock:
            self.ensure_token()
            if not force and self.cached_at and time.monotonic()-self.cached_at<120:return list(self.cached)
            families=self.read('families.json')
            if time.time()-families.get('fetched',0)>3600:
                values=self.request('/v2/family')
                families={'fetched':time.time(),'families':values.get('familyList',[])};self.write('families.json',families)
            result=[]
            for family in families.get('families',[]):
                index=None
                for _ in range(50):
                    params={'familyid':family['id'],'num':30}
                    if index is not None:params['beginIndex']=index
                    data=self.request('/v2/device/thing?'+urlencode(params));rows=data.get('thingList',[])
                    for row in rows:
                        item=row.get('itemData') or {}
                        if row.get('itemType') not in (1,2) or str(item.get('brandName','')).casefold() not in ('sonoff','coolkit'):continue
                        if self.power(item.get('params') or {}) is None:continue
                        rooms={room['id']:room.get('name','') for room in family.get('roomList',[])}
                        result.append({**item,'room_label':rooms.get((item.get('family') or {}).get('roomid'),'')})
                    if len(rows)<30:break
                    next_index=rows[-1].get('index')
                    if next_index is None or next_index==index:raise IoTError('Neúplný seznam eWeLink zařízení.',502)
                    index=next_index
                else:raise IoTError('Příliš mnoho eWeLink zařízení.',502)
            self.cached=result;self.cached_at=time.monotonic();return list(result)
    def devices(self):
        return [{'device_id':x['deviceid'],'name':x.get('name') or 'Sonoff','room':x.get('room_label',''),'model':x.get('productModel',''),'online':x.get('online') is True} for x in self.things()]

class SonoffAdapter:
    def __init__(self,client=None):self.client=client or Ewelink()
    def status(self,module,force=False):
        item=next((x for x in self.client.things(force) if x.get('deviceid')==module['device_id']),None)
        if item is None:raise IoTError('Sonoff není v připojeném účtu eWeLink.',404,'ewelink_missing')
        power=self.client.power(item.get('params') or {})
        return {'online':item.get('online') is True,'power':power,'source':'ewelink','cloud':True}
    def command(self,module,command):
        if command.get('control')!='power':raise IoTError('Sonoff podporuje pouze zapnutí a vypnutí.')
        desired=check_bool(command.get('value'))
        with self.client.lock:
            item=next((x for x in self.client.things() if x.get('deviceid')==module['device_id']),None)
            if item is None or item.get('online') is not True:raise IoTError('Zásuvka Sonoff je offline.',409)
            params=item.get('params') or {}
            if 'switch' in params:update={'switch':'on' if desired else 'off'}
            else:
                switch=dict(params['switches'][0]);switch['switch']='on' if desired else 'off';update={'switches':[switch]}
            self.client.request('/v2/device/thing/status',{'type':1,'id':module['device_id'],'params':update})
            state=self.status(module,force=True)
            if state['online'] is not True or state['power'] is not desired:raise IoTError('eWeLink zatím nepotvrdil změnu zásuvky.',502,'not_confirmed')
            return state
