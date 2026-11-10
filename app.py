"""Vercel-compatible ASGI entrypoint. The persistent workers run elsewhere."""
import os

os.environ.setdefault("STATELESS_GATEWAY", "1")

from main import build_app

app = build_app()
