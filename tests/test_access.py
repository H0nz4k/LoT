import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'iot'))
from access import RemoteAccess, remote_route_allowed
from server import make_server, request_access
from test_iot import ManagerFixture

TEAM = 'fixture-team.cloudflareaccess.com'
ORIGIN = 'https://iot.example.com'
AUD = 'fixture-iot-audience'


class SignedTokens:
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.private_key.public_key()))
        cls.jwk.update(kid='fixture-key', alg='RS256', use='sig')

    def token(self, **changes):
        now = int(time.time())
        claims = dict(iss='https://' + TEAM, aud=[AUD], exp=now + 600, iat=now,
                      sub='fixture-user', email='owner@example.com', type='app')
        claims.update(changes)
        return jwt.encode(claims, self.private_key, algorithm='RS256', headers={'kid':'fixture-key'})

    def access(self, emails=''):
        access = RemoteAccess(TEAM, AUD, ORIGIN, emails)
        access.client.fetch_data = lambda: {'keys':[self.jwk]}
        return access


class AccessTokenTests(SignedTokens, unittest.TestCase):
    def test_valid_signed_token_and_optional_email_restriction(self):
        self.assertTrue(self.access().verify(self.token()))
        self.assertTrue(self.access('OWNER@example.com').verify(self.token()))
        self.assertFalse(self.access('someone-else@example.com').verify(self.token()))

    def test_expired_wrong_issuer_audience_future_and_missing_identity_denied(self):
        for claims in ({'exp':int(time.time())-1}, {'iss':'https://other.cloudflareaccess.com'},
                       {'aud':['other-app']}, {'iat':int(time.time())+600},
                       {'nbf':int(time.time())+600}, {'email':''}, {'sub':''}):
            with self.subTest(claims=claims):
                self.assertFalse(self.access().verify(self.token(**claims)))
        claims = jwt.decode(self.token(), options={'verify_signature':False})
        for field in ('exp','iat','iss','aud','sub','email'):
            without = {k:v for k,v in claims.items() if k != field}
            token = jwt.encode(without, self.private_key, algorithm='RS256', headers={'kid':'fixture-key'})
            self.assertFalse(self.access().verify(token), field)

    def test_forged_signature_unsigned_and_symmetric_tokens_denied(self):
        claims = jwt.decode(self.token(), options={'verify_signature':False})
        for key, algorithm in ((self.other_key,'RS256'), ('fixture-only-secret-at-least-32-bytes','HS256'), (None,'none')):
            token = jwt.encode(claims, key, algorithm=algorithm, headers={'kid':'fixture-key'})
            self.assertFalse(self.access().verify(token), algorithm)
        for value in ('', 'malformed', 'x' * 16385):
            self.assertFalse(self.access().verify(value))

    def test_jwks_outage_and_wrong_key_fail_closed(self):
        access = self.access()
        with patch.object(access.client, 'fetch_data', side_effect=jwt.PyJWKClientConnectionError('offline')):
            self.assertFalse(access.verify(self.token()))
        access.client.fetch_data = lambda: {'keys':[]}
        self.assertFalse(access.verify(self.token()))

    def test_configuration_cannot_fetch_arbitrary_key_server(self):
        for team in ('', 'http://attacker.example', 'fixture.cloudflareaccess.com.attacker.example',
                     'fixture.cloudflareaccess.com/path', 'user@fixture.cloudflareaccess.com'):
            access = RemoteAccess(team, AUD, ORIGIN)
            self.assertFalse(access.enabled, team)
            self.assertFalse(access.verify(self.token()))
        for origin in ('http://iot.example.com', 'https://user@iot.example.com', 'https://iot.example.com/path'):
            self.assertFalse(RemoteAccess(TEAM,AUD,origin).enabled)
        self.assertFalse(RemoteAccess(TEAM,'',ORIGIN).enabled)

    def test_identity_headers_cannot_bypass_validation_or_local_origin_guard(self):
        access = self.access()
        def handler(headers, peer='127.0.0.1'):
            return SimpleNamespace(client_address=(peer,1234), headers=headers,
                                   server=SimpleNamespace(remote_access=access))
        self.assertEqual(request_access(handler({'Host':'127.0.0.1:4011'})), 'lan')
        self.assertIsNone(request_access(handler({'Host':'iot.example.com', 'Cf-Access-Authenticated-User-Email':'owner@example.com'})))
        self.assertIsNone(request_access(handler({'Host':'iot.example.com', 'CF-Connecting-IP':'192.168.1.99'})))
        self.assertIsNone(request_access(handler({'Host':'127.0.0.1:4011','Cf-Access-Jwt-Assertion':'fake'})))
        self.assertIsNone(request_access(handler({'Host':'127.0.0.1:4011'}, peer='203.0.113.20')))
        headers={'Host':'iot.example.com','CF-Connecting-IP':'203.0.113.20',
                 'Cf-Access-Jwt-Assertion':self.token(),'Origin':ORIGIN}
        self.assertEqual(request_access(handler(headers)), 'remote')
        for origin in ('https://attacker.example','http://iot.example.com','https://iot.example.com:1234'):
            self.assertIsNone(request_access(handler(dict(headers,Origin=origin))))


class RemoteHTTPTests(SignedTokens, ManagerFixture):
    def setUp(self):
        super().setUp()
        self.server = make_server('127.0.0.1',0,self.manager,remote_access=self.access())
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        super().tearDown()

    def request(self,path,method='GET',data=None,token=None,origin=ORIGIN):
        headers={'Host':'iot.example.com','CF-Connecting-IP':'203.0.113.20',
                 'Origin':origin,'Cf-Access-Jwt-Assertion':token or self.token()}
        if data is not None:
            headers['Content-Type']='application/json'
        request=Request(self.base+path,method=method,headers=headers,
                        data=None if data is None else json.dumps(data).encode())
        try:
            response=urlopen(request,timeout=4)
        except HTTPError as error:
            response=error
        with response:
            return response.status,json.load(response)

    def test_authenticated_remote_controls_heater_with_confirmed_result(self):
        code,info=self.request('/api/iot/info')
        self.assertEqual(code,200)
        self.assertEqual(info['access'],'remote')
        self.assertFalse(info['can_manage'])
        self.assertEqual(self.request('/api/iot/devices')[0],200)
        for control,value in (('power',False),('target_temp_c',28),('power',True)):
            code,result=self.request(f'/api/iot/devices/{self.id}/command','POST',{'control':control,'value':value})
            self.assertEqual(code,200)
            self.assertTrue(result['ok'])
            self.assertIs(result['state']['online'],True)
            field='target_temp_c' if control=='target_temp_c' else 'power'
            self.assertEqual(result['state'][field],value)
        self.assertEqual(self.request(f'/api/iot/devices/{self.id}/state')[0],200)
        self.assertEqual(self.request('/api/iot/events?id='+self.id)[0],200)

    def test_remote_registry_changes_import_and_secret_configuration_denied(self):
        before=self.manager.list_modules()
        for path,method,data in (('/api/iot/devices','POST',{}),('/api/iot/import','POST',{}),
                                  ('/api/iot/candidates','GET',None),
                                  (f'/api/iot/devices/{self.id}','PUT',{'local_key':'replacement'}),
                                  (f'/api/iot/devices/{self.id}','DELETE',None),
                                  (f'/api/iot/devices/{self.id}','GET',None)):
            with self.subTest(path=path,method=method):
                code,result=self.request(path,method,data)
                self.assertEqual(code,403)
                self.assertEqual(result['code'],'management_lan_only')
        self.assertEqual(self.manager.list_modules(),before)
        self.assertEqual(self.device.writes,[])

    def test_invalid_token_foreign_origin_and_read_only_command_never_write(self):
        path=f'/api/iot/devices/{self.id}/command'
        for token,origin in (('fake',ORIGIN),(self.token(exp=int(time.time())-1),ORIGIN),
                             (self.token(),'https://attacker.example')):
            self.assertEqual(self.request(path,'POST',{'control':'power','value':True},token,origin)[0],403)
        self.assertEqual(self.request(path,'POST',{'control':'current_temp_c','value':99})[0],400)
        self.assertEqual(self.device.writes,[])


if __name__ == '__main__':
    unittest.main()
