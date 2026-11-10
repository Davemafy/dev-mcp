"""Single-owner OAuth 2.1 + PKCE for a private remote MCP gateway.

This avoids any GitHub or external identity-provider account. OAuth login accepts
OWNER_PASSWORD; access and refresh credentials are HMAC-signed and audience-bound.
For durable one-time-use state on Railway, attach a volume at AUTH_DB_PATH.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit, parse_qsl

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse, RedirectResponse, Response
from starlette.routing import Route


class OAuthError(ValueError):
    pass


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + '=' * (-len(data) % 4))


def resolve_owner_credentials(db_path: str) -> tuple[str, str]:
    """Bootstrap single-owner login without crashing the service.

    Explicit Railway variables take precedence. Generated credentials are stored
    beside the OAuth database when possible; a Railway volume makes them durable.
    No secret is returned by an unauthenticated HTTP endpoint.
    """
    configured_password = os.getenv('OWNER_PASSWORD', '')
    configured_secret = os.getenv('TOKEN_SIGNING_SECRET', '')
    password = configured_password if len(configured_password) >= 20 else ''
    secret = configured_secret if len(configured_secret) >= 32 else ''

    path = Path(db_path).with_name('dev-mcp-bootstrap.json')
    persisted = {}
    try:
        if path.exists():
            persisted = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(persisted, dict):
                persisted = {}
    except (OSError, json.JSONDecodeError, ValueError):
        persisted = {}

    if not password and isinstance(persisted.get('owner_password'), str):
        password = persisted['owner_password']
    if not secret and isinstance(persisted.get('signing_secret'), str):
        secret = persisted['signing_secret']
    if len(password) < 20:
        password = secrets.token_urlsafe(32)
        new_password = True
    else:
        new_password = False
    if len(secret) < 32:
        secret = secrets.token_urlsafe(48)

    if not configured_password or len(configured_password) < 20 or not configured_secret or len(configured_secret) < 32:
        # Store only generated credentials, never a password supplied explicitly
        # through Railway Variables. The file is owner-readable only.
        data = {
            'owner_password': password if len(configured_password) < 20 else None,
            'signing_secret': secret if len(configured_secret) < 32 else None,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            fd = os.open(path, flags, 0o600)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as file:
                    json.dump(data, file)
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
        except OSError:
            print('[dev-mcp] WARNING: Bootstrap credentials could not be saved. '
                  'They will change if this instance restarts.', flush=True)

    if new_password:
        # Railway deployment logs are owner-visible. Never show this on /health.
        print('[dev-mcp] Initial owner login password (save privately): ' + password,
              flush=True)
        print('[dev-mcp] For stable credentials across deployments, set '
              'OWNER_PASSWORD / TOKEN_SIGNING_SECRET in Railway Variables, or '
              'attach a volume for AUTH_DB_PATH.', flush=True)
    return password, secret


@dataclass(frozen=True)
class Settings:
    base_url: str
    password: str
    secret: str
    db_path: str = '/tmp/dev-mcp-auth.sqlite'

    @classmethod
    def from_env(cls) -> 'Settings':
        # Railway exposes its public hostname on many deployments. A manually
        # configured PUBLIC_BASE_URL takes precedence; hostname-only values are OK.
        raw = os.getenv('PUBLIC_BASE_URL', '').strip()
        domain = os.getenv('RAILWAY_PUBLIC_DOMAIN', '').strip()
        base = raw or domain
        if base and '://' not in base:
            base = 'https://' + base
        base = base.rstrip('/')
        split = urlsplit(base)
        loopback = (split.scheme == 'http' and split.hostname in ('localhost', '127.0.0.1')
                    and split.port is not None)
        valid_scheme = split.scheme == 'https' or loopback
        if not base or not valid_scheme or not split.hostname or split.path or split.query or split.fragment or split.username or split.password:
            raise RuntimeError(
                'Invalid public URL. Set PUBLIC_BASE_URL in Railway Variables to '
                'https://YOUR-DOMAIN.up.railway.app (no path or placeholder). '
                'Alternatively configure a Railway public domain so RAILWAY_PUBLIC_DOMAIN is present.'
            )
        if os.getenv('STATELESS_GATEWAY') == '1' and not os.getenv('AUTH_REDIS_URL'):
            raise RuntimeError('Shared Redis state is required for stateless OAuth')
        db_path = os.getenv('AUTH_DB_PATH', '/tmp/dev-mcp-auth.sqlite')
        password, secret = resolve_owner_credentials(db_path)
        return cls(base, password, secret, db_path)


class OAuth:
    scope = 'dev:run offline_access'

    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.attempts: dict[str, list[float]] = {}
        self.shared_state = None
        if os.getenv('AUTH_REDIS_URL'):
            from shared_state import SharedState
            self.shared_state = SharedState(os.environ['AUTH_REDIS_URL'])
        else:
            Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS used_tokens (jti TEXT PRIMARY KEY, used_at INTEGER NOT NULL)')

    def _connect(self):
        return sqlite3.connect(self.cfg.db_path, timeout=5.0)

    def _mark_once(self, jti: str) -> bool:
        try:
            with self.lock, self._connect() as db:
                db.execute('DELETE FROM used_tokens WHERE used_at < ?', (int(time.time())-35*86400,))
                db.execute('INSERT INTO used_tokens (jti, used_at) VALUES (?, ?)', (jti, int(time.time())))
            return True
        except sqlite3.IntegrityError:
            return False

    def sign(self, values: dict[str, Any], lifetime: int) -> str:
        now = int(time.time())
        header = b64(b'{"alg":"HS256","typ":"JWT"}')
        payload = dict(values, iat=now, exp=now+lifetime, iss=self.cfg.base_url)
        body = b64(json.dumps(payload, separators=(',', ':'), sort_keys=True).encode())
        message = f'{header}.{body}'
        signature = b64(hmac.new(self.cfg.secret.encode(), message.encode(), hashlib.sha256).digest())
        return f'{message}.{signature}'

    def verify(self, token: str, kind: str, audience: str | None = None) -> dict[str, Any]:
        try:
            if len(token) > 10000:
                raise OAuthError('Invalid token')
            header, body, signature = token.split('.')
            message = f'{header}.{body}'
            expected = b64(hmac.new(self.cfg.secret.encode(), message.encode(), hashlib.sha256).digest())
            if not secrets.compare_digest(signature, expected):
                raise OAuthError('Invalid signature')
            if json.loads(unb64(header)) != {'alg':'HS256','typ':'JWT'}:
                raise OAuthError('Invalid algorithm')
            data = json.loads(unb64(body))
            if (data.get('typ') != kind or data.get('iss') != self.cfg.base_url or
                not isinstance(data.get('exp'), int) or data['exp'] <= time.time()):
                raise OAuthError('Expired or invalid token')
            if audience is not None and data.get('aud') != audience:
                raise OAuthError('Wrong audience')
            return data
        except (TypeError, ValueError, UnicodeDecodeError, KeyError, binascii.Error) as exc:
            raise OAuthError('Invalid token') from exc

    def client(self, client_id: str) -> dict[str, Any]:
        return self.verify(client_id, 'client')

    @staticmethod
    def valid_redirect(uri: str) -> bool:
        if len(uri) > 2048:
            return False
        p = urlsplit(uri)
        if p.username or p.password or p.fragment or not p.netloc or not p.hostname:
            return False
        if p.scheme == 'https':
            return True
        return p.scheme == 'http' and p.hostname in ('127.0.0.1', 'localhost', '::1')

    async def register(self, request: Request):
        try:
            obj = await request.json()
            redirects = obj.get('redirect_uris')
            if (not isinstance(redirects, list) or not redirects or len(redirects) > 6 or
                any(not isinstance(u, str) or not self.valid_redirect(u) for u in redirects)):
                raise OAuthError('Invalid redirect_uris')
            cid = self.sign({'typ':'client', 'redirects':redirects}, 365*86400)
            return JSONResponse({'client_id':cid, 'client_id_issued_at':int(time.time()),
                'client_name':str(obj.get('client_name','dev-mcp'))[:80],
                'redirect_uris': redirects, 'grant_types':['authorization_code','refresh_token'],
                'response_types':['code'], 'token_endpoint_auth_method':'none', 'scope':self.scope},status_code=201,
                headers={'Cache-Control':'no-store'})
        except (ValueError, TypeError, KeyError):
            return JSONResponse({'error':'invalid_client_metadata'}, status_code=400)

    def _authorization_request(self, q: dict[str, str]) -> str:
        client = self.client(q.get('client_id',''))
        if (q.get('response_type') != 'code' or q.get('redirect_uri') not in client['redirects'] or
            q.get('code_challenge_method') != 'S256'):
            raise OAuthError('Invalid authorization request')
        challenge = q.get('code_challenge','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', challenge):
            raise OAuthError('PKCE S256 challenge required')
        scopes = set(q.get('scope','').split())
        if scopes and not scopes.issubset({'dev:run','offline_access'}):
            raise OAuthError('Invalid scope')
        return self.sign({'typ':'auth_request', 'client_id':q['client_id'],
            'redirect_uri':q['redirect_uri'], 'challenge':challenge,
            'state':q.get('state','')[:2000], 'scope':self.scope}, 600)

    async def authorize_page(self, request: Request):
        try:
            flow = self._authorization_request(dict(request.query_params))
        except (ValueError, KeyError):
            return HTMLResponse('Invalid OAuth authorization request', status_code=400)
        callback = urlsplit(self.verify(flow, 'auth_request')['redirect_uri'])
        callback_origin = f'{callback.scheme}://{callback.netloc}'
        page = ("<!doctype html><html lang='en'><meta charset='utf-8'><title>Authorize dev-mcp</title>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<main style='max-width:25rem;margin:12vh auto;font:16px system-ui;padding:1rem'>"
            "<h1>Connect dev-mcp</h1><p>This connection grants terminal and filesystem access to a disposable E2B machine.</p>"
            "<style>label{display:block} input{display:block;box-sizing:border-box;width:100%;min-height:44px;padding:.6rem;margin:.5rem 0 1rem;font:inherit} button{min-height:48px;padding:.7rem 1.5rem;font:inherit;cursor:pointer}</style>"
            "<form method='post' action='/authorize'>"
            f"<p>Client callback: <code>{html.escape(urlsplit(self.verify(flow, 'auth_request')['redirect_uri']).netloc)}</code></p>"
            f"<input type='hidden' name='request' value='{html.escape(flow,quote=True)}'>"
            "<label>Owner password <input type='password' name='password' required autocomplete='current-password'></label>"
            "<p><button type='submit'>Authorize</button></p></form></main></html>")
        return HTMLResponse(page, headers={'Cache-Control':'no-store',
            'Content-Security-Policy':f"default-src 'none'; style-src 'unsafe-inline'; form-action 'self' {callback_origin}"})

    async def authorize_submit(self, request: Request):
        ip = request.client.host if request.client else 'unknown'
        try:
            fields = parse_qs((await request.body()).decode(), keep_blank_values=True)
            token = fields.get('request',[''])[0]
            pw = fields.get('password',[''])[0]
            flow = self.verify(token, 'auth_request')
            now = time.monotonic()
            with self.lock:
                recent = [x for x in self.attempts.get(ip,[]) if now - x < 300]
                recent.append(now)
                self.attempts[ip] = recent
            if len(recent) > 10:
                return HTMLResponse('Too many login attempts',status_code=429)
            if not secrets.compare_digest(pw, self.cfg.password):
                return HTMLResponse('Incorrect password',status_code=401)
            code = self.sign({'typ':'code', 'jti':secrets.token_urlsafe(24),
                'aud':self.cfg.base_url+'/token', 'client_id':flow['client_id'],
                'redirect_uri':flow['redirect_uri'], 'challenge':flow['challenge']}, 120)
            p = urlsplit(flow['redirect_uri'])
            query = parse_qsl(p.query,keep_blank_values=True)
            query.extend([('code',code),('state',flow['state'])])
            location = urlunsplit((p.scheme,p.netloc,p.path,urlencode(query),''))
            return RedirectResponse(location,status_code=303, headers={'Cache-Control':'no-store'})
        except (OAuthError, KeyError, UnicodeDecodeError, ValueError):
            return HTMLResponse('Invalid authorization',status_code=400)

    def _issue_tokens(self, client_id: str, scope: str | None = None) -> dict[str, Any]:
        scope = scope or self.scope
        access = self.sign({'typ':'access', 'aud':self.cfg.base_url+'/mcp',
            'sub':'owner','client_id':client_id,'scope':scope,'jti':secrets.token_urlsafe(18)},3600)
        refresh = self.sign({'typ':'refresh','aud':self.cfg.base_url+'/token',
            'sub':'owner','client_id':client_id,'scope':scope,'jti':secrets.token_urlsafe(24)},30*86400)
        return {'access_token':access, 'token_type':'Bearer', 'expires_in':3600,
                'refresh_token':refresh, 'scope':scope}

    async def token(self, request: Request):
        try:
            form = parse_qs((await request.body()).decode(),keep_blank_values=True)
            val = lambda key:form.get(key,[''])[0]
            client_id = val('client_id')
            self.client(client_id)
            grant = val('grant_type')
            if grant == 'authorization_code':
                code = self.verify(val('code'),'code',self.cfg.base_url+'/token')
                verifier = val('code_verifier')
                if (code['client_id'] != client_id or code['redirect_uri'] != val('redirect_uri') or
                    not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}',verifier) or
                    b64(hashlib.sha256(verifier.encode()).digest()) != code['challenge']):
                    raise OAuthError('Invalid code or PKCE')
                if not self._mark_once('code:'+code['jti']):
                    raise OAuthError('Authorization code already redeemed')
                issued = self._issue_tokens(client_id)
            elif grant == 'refresh_token':
                refresh = self.verify(val('refresh_token'),'refresh',self.cfg.base_url+'/token')
                if refresh['client_id'] != client_id or not self._mark_once('refresh:'+refresh['jti']):
                    raise OAuthError('Invalid or already redeemed refresh token')
                issued = self._issue_tokens(client_id,refresh['scope'])
            else:
                return JSONResponse({'error':'unsupported_grant_type'},status_code=400)
            return JSONResponse(issued,headers={'Cache-Control':'no-store','Pragma':'no-cache'})
        except (ValueError,KeyError,UnicodeDecodeError):
            return JSONResponse({'error':'invalid_grant'},status_code=400,headers={'Cache-Control':'no-store'})

    async def metadata(self, request: Request):
        base = self.cfg.base_url
        return JSONResponse({'issuer':base, 'authorization_endpoint':base+'/authorize',
            'token_endpoint':base+'/token', 'registration_endpoint':base+'/register',
            'response_types_supported':['code'],
            'grant_types_supported':['authorization_code','refresh_token'],
            'code_challenge_methods_supported':['S256'], 'token_endpoint_auth_methods_supported':['none'],
            'scopes_supported':['dev:run','offline_access']})

    async def resource_metadata(self, request: Request):
        return JSONResponse({'resource':self.cfg.base_url+'/mcp',
            'authorization_servers':[self.cfg.base_url], 'scopes_supported':['dev:run','offline_access'],
            'bearer_methods_supported':['header']})

    def routes(self):
        return [
            Route('/.well-known/oauth-authorization-server',self.metadata),
            Route('/.well-known/oauth-protected-resource',self.resource_metadata),
            Route('/.well-known/oauth-protected-resource/mcp',self.resource_metadata),
            Route('/register',self.register, methods=['POST']),
            Route('/authorize',self.authorize_page,methods=['GET']),
            Route('/authorize',self.authorize_submit,methods=['POST']),
            Route('/token',self.token,methods=['POST']),
        ]


class BearerGate:
    """Enforces audience-bound access tokens before FastMCP sees any /mcp traffic."""
    def __init__(self, app, oauth: OAuth):
        self.app = app
        self.oauth = oauth

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            headers = dict(scope.get('headers',[]))
            auth = headers.get(b'authorization',b'').decode('ascii',errors='ignore')
            try:
                if not auth.startswith('Bearer '):
                    raise OAuthError('Missing bearer token')
                token = self.oauth.verify(auth[7:],'access',self.oauth.cfg.base_url+'/mcp')
                if 'dev:run' not in token.get('scope','').split():
                    raise OAuthError('Insufficient scope')
            except OAuthError:
                response = JSONResponse({'error':'unauthorized'},status_code=401,
                    headers={'WWW-Authenticate':f'Bearer resource_metadata="{self.oauth.cfg.base_url}/.well-known/oauth-protected-resource/mcp"'})
                return await response(scope,receive,send)
        return await self.app(scope,receive,send)
