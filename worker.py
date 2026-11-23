"""Cloudflare Python Worker entrypoint."""

import os

os.environ["DVARC_CLOUDFLARE"] = "1"
from workers import asgi

from app.main import create_app

app = create_app(cloudflare=True)
Default = asgi.entrypoint(app)
