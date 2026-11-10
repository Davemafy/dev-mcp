"""Persistent-per-workspace E2B machine adapter; Railway never runs user code."""
from __future__ import annotations

import os
import hashlib
import base64
import shlex
from pathlib import PurePosixPath
from typing import Callable
from auth import OAuth, OAuthError

HOME='/home/user'


def checked_path(value: str) -> str:
    if not isinstance(value,str) or not value or len(value)>1000 or '\x00' in value:
        raise ValueError('Invalid path')
    path=PurePosixPath(value)
    if '..' in path.parts:
        raise ValueError('Path traversal denied')
    if not path.is_absolute():
        path=PurePosixPath(HOME)/path
    if path != PurePosixPath(HOME) and PurePosixPath(HOME) not in path.parents:
        raise ValueError('File tools are restricted to /home/user')
    return str(path)


class Machine:
    def __init__(self, oauth: OAuth, sandbox_factory: Callable | None = None):
        self.oauth=oauth
        self.issuer = hashlib.sha256(oauth.cfg.base_url.encode()).hexdigest()[:24]
        self.e2b_configured = bool(os.getenv("E2B_API_KEY")) or sandbox_factory is not None
        if sandbox_factory is None:
            if os.getenv("E2B_API_KEY"):
                from e2b import Sandbox
                sandbox_factory=Sandbox
            else:
                # Keep the authenticated MCP gateway and /health online so an
                # absent E2B key is a recoverable configuration issue.
                class MissingE2B:
                    @staticmethod
                    def create(**kwargs):
                        raise RuntimeError("E2B_API_KEY is missing. Set it in Railway Variables to create a workspace.")
                    @staticmethod
                    def connect(sandbox_id):
                        raise RuntimeError("E2B_API_KEY is missing. Set it in Railway Variables to reconnect.")
                sandbox_factory=MissingE2B
                print("[dev-mcp] E2B_API_KEY not configured; gateway is online, workspace tools are unavailable.",flush=True)
        self.sandbox_factory=sandbox_factory
        self.timeout=min(3600,max(120,int(os.getenv('E2B_TIMEOUT_SECONDS','1800'))))
        self.max_command=min(300,max(5,int(os.getenv('MAX_COMMAND_SECONDS','120'))))
        self.output_limit=min(100000,max(1000,int(os.getenv('MAX_OUTPUT_CHARS','30000'))))

    def create(self):
        sandbox=self.sandbox_factory.create(timeout=self.timeout,
            metadata={'dev_mcp_issuer': self.issuer})
        sid=str(sandbox.sandbox_id)
        return {'workspace_id': self.oauth.sign({'typ':'workspace','sandbox_id':sid},86400),
                'timeout_seconds':self.timeout,'home':HOME}

    def connect(self,workspace_id: str):
        claims=self.oauth.verify(workspace_id,'workspace')
        sandbox=self.sandbox_factory.connect(claims['sandbox_id'])
        if hasattr(sandbox,"set_timeout"):
            sandbox.set_timeout(self.timeout)
        return sandbox

    def status(self,workspace_id: str):
        if workspace_id == 'recover':
            return self.recover()
        s=self.connect(workspace_id)
        return {'ready':True,'sandbox_id':s.sandbox_id,'home':HOME}

    def recover(self):
        """Read the configured owner's E2B project inventory; never create/resume.

        Excludes workspaces tagged for a different dev-mcp installation. Older
        untagged project workspaces are explicit legacy candidates, not silently
        selected. This operation is behind the same single-owner OAuth gate as
        every terminal operation. Handles remain signed and issuer-bound.
        """
        if not self.e2b_configured:
            raise RuntimeError('E2B_API_KEY is missing; workspace recovery is unavailable.')
        listing = getattr(self.sandbox_factory, 'list', None)
        if not callable(listing):
            raise RuntimeError('Installed E2B SDK does not provide workspace discovery.')
        from e2b import SandboxQuery, SandboxState
        pages = listing(query=SandboxQuery(state=[SandboxState.RUNNING, SandboxState.PAUSED]),
                        limit=100, request_timeout=10, retries=0)
        workspaces = []
        seen = set()
        # Bounded management-plane query; this is not a reconnect heartbeat.
        for _ in range(2):
            if not pages.has_next:
                break
            for item in pages.next_items():
                sid = str(item.sandbox_id)
                state = getattr(item.state, 'value', item.state)
                metadata = item.metadata or {}
                tag = metadata.get('dev_mcp_issuer')
                if state not in ('running', 'paused') or tag not in (None, self.issuer) or sid in seen:
                    continue
                seen.add(sid)
                started = getattr(item, 'started_at', None)
                workspaces.append({
                    'sandbox_id': sid, 'state': state,
                    'started_at': started.isoformat() if hasattr(started, 'isoformat') else None,
                    'legacy_untagged': tag is None,
                    'workspace_id': self.oauth.sign({'typ':'workspace','sandbox_id':sid},86400),
                })
        workspaces.sort(key=lambda x: (x['legacy_untagged'], x['started_at'] or ''))
        return {'ready': True, 'recovery_available': True, 'workspaces': workspaces,
                'truncated': bool(pages.has_next), 'created_workspace': False,
                'message': ('Choose a returned signed workspace_id to reconnect. '
                            'Expired or deleted sandbox files cannot be restored by discovery.')}

    def close(self,workspace_id: str):
        self.connect(workspace_id).kill()
        return {'closed':True}

    def write(self,workspace_id: str,path: str,content: str):
        if not isinstance(content,str) or len(content.encode('utf-8'))>500_000:
            raise ValueError('File text must be <= 500 KB')
        p=checked_path(path)
        self.connect(workspace_id).files.write(p,content)
        return {'path':p,'bytes':len(content.encode('utf-8')),'written':True}

    def read(self,workspace_id: str,path: str):
        p=checked_path(path)
        data=self.connect(workspace_id).files.read(p)
        if not isinstance(data,str):
            raise ValueError('Only text files are supported')
        return {'path':p,'content':data[:self.output_limit],
                'truncated':len(data)>self.output_limit}

    def list_files(self,workspace_id: str,path: str='.'):
        p=checked_path(path)
        files=self.connect(workspace_id).files.list(p)
        return {'path':p, 'files':[{'name':str(getattr(f,'name','')),
            'path':str(getattr(f,'path','')),'type':str(getattr(f,'type','')),
            'size':getattr(f,'size',None)} for f in files[:100]]}

    def run(self,workspace_id: str,command: str,timeout_seconds: int=120):
        if not isinstance(command,str) or not command or len(command)>10000:
            raise ValueError('Invalid command')
        t=max(1,min(int(timeout_seconds),self.max_command))
        sandbox=self.connect(workspace_id)
        try:
            result=sandbox.commands.run(command,timeout=t,cwd=HOME)
            return self._execution_result(result)
        except Exception as exc:
            # E2B raises for nonzero exit codes; preserve the actual status and streams.
            if hasattr(exc,'exit_code'):
                return self._execution_result(exc)
            raise

    def _execution_result(self, result):
        stdout=str(getattr(result,'stdout','') or '')
        stderr=str(getattr(result,'stderr','') or '')
        code=getattr(result,'exit_code',None)
        return {'exit_code':code,'stdout':stdout[:self.output_limit],
                'stderr':stderr[:self.output_limit],
                'stdout_truncated':len(stdout)>self.output_limit,
                'stderr_truncated':len(stderr)>self.output_limit}

    def start_process(self,workspace_id: str,command: str):
        if not isinstance(command,str) or not command or len(command)>10000:
            raise ValueError('Invalid command')
        handle=self.connect(workspace_id).commands.run(command,cwd=HOME,background=True)
        return {'pid':getattr(handle,'pid',None),'started':True}

    def list_processes(self,workspace_id: str):
        processes=self.connect(workspace_id).commands.list()
        return {'processes':[{'pid':getattr(p,'pid',None),
            'cmd':str(getattr(p,'cmd',''))[:500]} for p in processes[:100]]}

    def stop_process(self,workspace_id: str,pid: int):
        self.connect(workspace_id).commands.kill(int(pid))
        return {'stopped':True,'pid':int(pid)}

    def read_capture(self, workspace_id: str) -> bytes:
        """Return only the fixed, bounded JPEG created by browser_journey.

        Binary reads are performed inside the signed sandbox. Do not expose
        arbitrary filesystem paths as MCP image inputs.
        """
        code = (
            "from pathlib import Path;import base64;"
            "p=Path('/home/user/browser-captures/latest.jpg');"
            "d=p.read_bytes();"
            "assert len(d)<=750000,'Capture too large';"
            "print(base64.b64encode(d).decode())"
        )
        sandbox = self.connect(workspace_id)
        try:
            result = sandbox.commands.run(
                "python3 -c " + shlex.quote(code), timeout=20, cwd=HOME
            )
        except Exception:
            raise RuntimeError("Browser capture unavailable.") from None
        if getattr(result, "exit_code", 1) != 0:
            raise RuntimeError("Browser capture missing; run browser_journey first.")
        encoded = str(getattr(result, "stdout", "") or "").strip()
        if len(encoded) > 1_100_000:
            raise ValueError("Capture exceeds encoded output limit")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error):
            raise ValueError("Invalid capture encoding") from None
        if len(raw) > 750_000 or not raw.startswith(b"\\xff\\xd8"):
            raise ValueError("Invalid or excessive JPEG capture")
        return raw

    def preview_url(self,workspace_id: str,port: int):
        port=int(port)
        if port < 1024 or port>65535:
            raise ValueError('Preview port must be between 1024 and 65535')
        return {'url':'https://'+self.connect(workspace_id).get_host(port),'port':port}
