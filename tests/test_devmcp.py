import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit, urlencode

import httpx
import pytest
from starlette.applications import Starlette

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from auth import OAuth, Settings, OAuthError, BearerGate, b64
from machine import Machine, checked_path


@pytest.fixture
def oauth(tmp_path):
    return OAuth(Settings('https://example.up.railway.app','p'*30,'s'*42,str(tmp_path/'auth.sqlite')))


class FakeFiles:
    def __init__(self, root): self.root=root
    def local(self,p):
        assert p.startswith('/home/user')
        return self.root / Path(p).relative_to('/home/user')
    def write(self,p,data):
        f=self.local(p);f.parent.mkdir(parents=True,exist_ok=True);f.write_text(data)
    def read(self,p):return self.local(p).read_text()
    def list(self,p):
        return [SimpleNamespace(name=f.name,path=f'/home/user/{f.relative_to(self.root)}',type='file',size=f.stat().st_size) for f in self.local(p).iterdir()]


class FakeCommands:
    def __init__(self,root): self.root=root
    def run(self,cmd,timeout=None,cwd=None,background=False):
        if background:
            return SimpleNamespace(pid=531)
        p=subprocess.run(cmd,shell=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,cwd=self.root,text=True)
        return SimpleNamespace(stdout=p.stdout,stderr=p.stderr,exit_code=p.returncode)
    def list(self): return [SimpleNamespace(pid=531,cmd='python server.py')]
    def kill(self,pid): return True


class FakeSandbox:
    items={}
    next_id=0
    @classmethod
    def create(cls,timeout=1800,metadata=None):
        cls.next_id+=1
        obj=cls.__new__(cls)
        obj.sandbox_id=f'sandbox-{cls.next_id}'
        obj.root=Path(tempfile.mkdtemp(prefix='dev-mcp-fake-'))
        obj.files=FakeFiles(obj.root)
        obj.commands=FakeCommands(obj.root)
        obj.dead=False
        obj.metadata=metadata or {}
        cls.items[obj.sandbox_id]=obj
        return obj
    @classmethod
    def connect(cls,sandbox_id):
        obj=cls.items[sandbox_id]
        if obj.dead:raise ValueError('expired')
        return obj
    def kill(self):self.dead=True
    def get_host(self,port):return f'{port}-{self.sandbox_id}.e2b.app'


@pytest.fixture
def machine(oauth): return Machine(oauth,FakeSandbox)


def test_real_command_and_file_round_trip(machine):
    sid=machine.create()['workspace_id']
    machine.write(sid,'src/hello.py','print("Hello from persistent workspace")')
    x=machine.run(sid,'python src/hello.py')
    assert x['exit_code']==0
    assert x['stdout'].strip()=='Hello from persistent workspace'
    assert machine.read(sid,'src/hello.py')['content']=='print("Hello from persistent workspace")'
    assert 'src' in [f['name'] for f in machine.list_files(sid)['files']]


def test_nonzero_exit_is_not_success(machine):
    sid=machine.create()['workspace_id']
    x=machine.run(sid,'python -c "import sys;print(\'problem\',file=sys.stderr);sys.exit(13)"')
    assert x['exit_code']==13
    assert 'problem' in x['stderr']


def test_workspace_is_cryptographically_scoped(machine):
    sid=machine.create()['workspace_id']
    tampered=sid[:-5] + ('a' if sid[-5]!='a' else 'b') + sid[-4:]
    with pytest.raises(OAuthError): machine.status(tampered)
    assert machine.status(sid)['ready']


def test_new_workspaces_are_tagged_for_this_gateway(machine):
    handle = machine.create()['workspace_id']
    sid = machine.oauth.verify(handle, 'workspace')['sandbox_id']
    assert FakeSandbox.items[sid].metadata == {'dev_mcp_issuer': machine.issuer}


def test_workspace_closure(machine):
    sid=machine.create()['workspace_id']
    assert machine.close(sid)=={'closed':True}
    with pytest.raises(ValueError):machine.status(sid)


def test_path_boundaries():
    assert checked_path('app/main.py')=='/home/user/app/main.py'
    assert checked_path('/home/user/notes.txt')=='/home/user/notes.txt'
    for path in ['../etc/passwd','/etc/passwd','/home/user/../root/.ssh/id_rsa','/home/user-other/file','']:
        with pytest.raises(ValueError):checked_path(path)


def test_process_tools_and_preview(machine):
    sid=machine.create()['workspace_id']
    assert machine.start_process(sid,'python -m http.server 3000')['pid']==531
    assert machine.list_processes(sid)['processes'][0]['pid']==531
    assert machine.stop_process(sid,531)['stopped']
    assert machine.preview_url(sid,3000)['url'].startswith('https://3000-sandbox-')
    with pytest.raises(ValueError):machine.preview_url(sid,80)


def test_output_truncation(machine):
    sid=machine.create()['workspace_id']
    machine.output_limit=12
    x=machine.run(sid,'python -c "print(\'x\'*50)"')
    assert x['stdout_truncated'] and len(x['stdout'])==12


def test_token_signing_and_audience(oauth):
    signed=oauth.sign({'typ':'access','aud':oauth.cfg.base_url+'/mcp','scope':'dev:run'},100)
    assert oauth.verify(signed,'access',oauth.cfg.base_url+'/mcp')['scope']=='dev:run'
    with pytest.raises(OAuthError):oauth.verify(signed,'access','https://attacker/mcp')
    with pytest.raises(OAuthError):oauth.verify(signed,'refresh',oauth.cfg.base_url+'/mcp')


def test_redirect_uri_validation():
    assert OAuth.valid_redirect('https://chatgpt.com/connector/oauth/callback')
    assert OAuth.valid_redirect('http://localhost:8181/callback')
    assert not OAuth.valid_redirect('http://evil.com/callback')
    assert not OAuth.valid_redirect('https://alice:pw@example.com/callback')


def test_oauth_full_authorize_refresh_replay(oauth):
    async def workflow():
        app=Starlette(routes=oauth.routes())
        transport=httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,base_url='https://example.up.railway.app',follow_redirects=False) as client:
            registration=await client.post('/register',json={'redirect_uris':['https://chatgpt.com/connector/oauth/callback']})
            assert registration.status_code==201
            cid=registration.json()['client_id']
            verifier='a'*64
            challenge=b64(hashlib.sha256(verifier.encode()).digest())
            q={'client_id':cid,'response_type':'code','redirect_uri':'https://chatgpt.com/connector/oauth/callback',
               'code_challenge':challenge,'code_challenge_method':'S256','state':'s123', 'scope':'dev:run offline_access'}
            page=await client.get('/authorize',params=q)
            assert page.status_code==200
            assert "form-action 'self' https://chatgpt.com" in page.headers['content-security-policy']
            assert 'min-height:48px' in page.text
            import re,html
            flow=html.unescape(re.search(r'name=\'request\' value=\'([^\']+)',page.text).group(1))
            fail=await client.post('/authorize',data={'request':flow,'password':'wrong-password'})
            assert fail.status_code==401
            valid=await client.post('/authorize',data={'request':flow,'password':oauth.cfg.password})
            assert valid.status_code==303
            parsed=urlsplit(valid.headers['location'])
            assert parsed.netloc=='chatgpt.com'
            params=parse_qs(parsed.query)
            assert params['state']==['s123']
            body={'grant_type':'authorization_code','client_id':cid,'redirect_uri':q['redirect_uri'],
                'code':params['code'][0],'code_verifier':verifier}
            wrong=await client.post('/token',data={**body,'code_verifier':'b'*64})
            assert wrong.status_code==400
            first=await client.post('/token',data=body)
            assert first.status_code==200,first.text
            issued=first.json()
            assert oauth.verify(issued['access_token'],'access',oauth.cfg.base_url+'/mcp')['sub']=='owner'
            replay=await client.post('/token',data=body)
            assert replay.status_code==400
            refresh={'grant_type':'refresh_token','client_id':cid,'refresh_token':issued['refresh_token']}
            refreshed=await client.post('/token',data=refresh)
            assert refreshed.status_code==200
            reused=await client.post('/token',data=refresh)
            assert reused.status_code==400
            meta=await client.get('/.well-known/oauth-authorization-server')
            assert meta.json()['code_challenge_methods_supported']==['S256']
            resource=await client.get('/.well-known/oauth-protected-resource/mcp')
            assert resource.json()['resource']==oauth.cfg.base_url+'/mcp'
    asyncio.run(workflow())


def test_unauthorized_mcp_requests_blocked(oauth):
    async def downstream(scope,receive,send):
        from starlette.responses import JSONResponse
        await JSONResponse({'allowed':True})(scope,receive,send)
    from starlette.routing import Mount
    app=Starlette(routes=[Mount('/mcp',app=BearerGate(downstream,oauth))])
    async def workflow():
        transport=httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,base_url='https://example.up.railway.app') as client:
            x=await client.post('/mcp/',json={'jsonrpc':'2.0','method':'initialize','id':1})
            assert x.status_code==401
            token=oauth.sign({'typ':'access','aud':oauth.cfg.base_url+'/mcp','scope':'dev:run'},90)
            y=await client.post('/mcp/',headers={'Authorization':'Bearer '+token})
            assert y.status_code==200
            z=await client.post('/mcp/',headers={'Authorization':'Bearer '+token+'a'})
            assert z.status_code==401
    asyncio.run(workflow())


def test_complete_gateway_routing_with_fake_fastmcp(monkeypatch,tmp_path):
    """Checks Starlette routing/auth wiring; actual FastMCP package still needs live check."""
    import types
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from main import build_app

    class AppWithLifespan(Starlette):
        @property
        def lifespan(self):
            from contextlib import asynccontextmanager
            @asynccontextmanager
            async def lifecycle(app):
                yield
            return lifecycle

    class StubFastMCP:
        def __init__(self,name):self.registered=[]
        def tool(self):
            def wrap(func):
                self.registered.append(func.__name__)
                return func
            return wrap
        def http_app(self):
            async def endpoint(request):
                return JSONResponse({'mock_mcp':True,'tools':self.registered})
            return AppWithLifespan(routes=[Route('/mcp',endpoint,methods=['POST'])])

    monkeypatch.setitem(sys.modules,'fastmcp',types.SimpleNamespace(FastMCP=StubFastMCP))
    monkeypatch.setenv('PUBLIC_BASE_URL','https://example.up.railway.app')
    monkeypatch.setenv('OWNER_PASSWORD','p'*30)
    monkeypatch.setenv('TOKEN_SIGNING_SECRET','s'*42)
    monkeypatch.setenv('AUTH_DB_PATH',str(tmp_path/'app-auth.sqlite'))
    app=build_app(machine_factory=FakeSandbox)
    async def workflow():
        transport=httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,base_url='https://example.up.railway.app') as client:
            health=await client.get('/health')
            assert health.json()['transport']=='streamable-http'
            forbidden=await client.post('/mcp')
            assert forbidden.status_code==401
            oauth=OAuth(Settings.from_env())
            access=oauth.sign({'typ':'access','aud':oauth.cfg.base_url+'/mcp','scope':'dev:run'},60)
            valid=await client.post('/mcp',headers={'Authorization':f'Bearer {access}'})
            assert valid.status_code==200
            names=valid.json()['tools']
            assert 'run_terminal_command' in names and 'read_file' in names and 'workspace_close' in names
    asyncio.run(workflow())


def test_railway_public_origin_configuration(monkeypatch,tmp_path):
    monkeypatch.setenv('OWNER_PASSWORD','p'*30)
    monkeypatch.setenv('TOKEN_SIGNING_SECRET','s'*42)
    monkeypatch.setenv('AUTH_DB_PATH',str(tmp_path/'auth.sqlite'))
    monkeypatch.setenv('PUBLIC_BASE_URL','dev-mcp-production-735c.up.railway.app/')
    monkeypatch.delenv('RAILWAY_PUBLIC_DOMAIN',raising=False)
    assert Settings.from_env().base_url == 'https://dev-mcp-production-735c.up.railway.app'
    monkeypatch.delenv('PUBLIC_BASE_URL',raising=False)
    monkeypatch.setenv('RAILWAY_PUBLIC_DOMAIN','my-app.up.railway.app')
    assert Settings.from_env().base_url == 'https://my-app.up.railway.app'
    monkeypatch.setenv('PUBLIC_BASE_URL','http://attacker.example.com')
    with pytest.raises(RuntimeError,match='Invalid public URL'):
        Settings.from_env()
    monkeypatch.setenv('PUBLIC_BASE_URL','https://my-app.up.railway.app/oauth/secret')
    with pytest.raises(RuntimeError,match='Invalid public URL'):
        Settings.from_env()



def test_bootstrap_without_owner_secrets(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('PUBLIC_BASE_URL','dev-mcp-production-735c.up.railway.app')
    monkeypatch.setenv('AUTH_DB_PATH',str(tmp_path/'auth.sqlite'))
    monkeypatch.delenv('OWNER_PASSWORD',raising=False)
    monkeypatch.delenv('TOKEN_SIGNING_SECRET',raising=False)

    first=Settings.from_env()
    assert first.base_url=='https://dev-mcp-production-735c.up.railway.app'
    assert len(first.password)>=20 and len(first.secret)>=32
    assert 'Initial owner login password' in capsys.readouterr().out

    # A volume-backed bootstrap file keeps the OAuth issuer and signing key
    # stable across successive server process starts.
    second=Settings.from_env()
    assert second.password==first.password
    assert second.secret==first.secret
    assert 'Initial owner login password' not in capsys.readouterr().out
    file=tmp_path/'dev-mcp-bootstrap.json'
    assert file.exists()
    assert file.stat().st_mode & 0o077 == 0

    signed=OAuth(first).sign({'typ':'access','aud':first.base_url+'/mcp'},120)
    assert OAuth(second).verify(signed,'access',first.base_url+'/mcp')['typ']=='access'


def test_missing_e2b_key_does_not_crash_gateway(monkeypatch,oauth):
    monkeypatch.delenv('E2B_API_KEY',raising=False)
    machine=Machine(oauth)
    assert not machine.e2b_configured
    with pytest.raises(RuntimeError,match='E2B_API_KEY is missing'):
        machine.create()


def test_short_optional_secrets_are_replaced_not_fatal(monkeypatch,tmp_path):
    monkeypatch.setenv('PUBLIC_BASE_URL','https://my-service.up.railway.app')
    monkeypatch.setenv('AUTH_DB_PATH',str(tmp_path/'auth.sqlite'))
    monkeypatch.setenv('OWNER_PASSWORD','short')
    monkeypatch.setenv('TOKEN_SIGNING_SECRET','short')
    settings=Settings.from_env()
    assert settings.password!='short' and settings.secret!='short'
    assert len(settings.password)>=20 and len(settings.secret)>=32
