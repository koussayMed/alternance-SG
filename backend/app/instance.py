"""Which instance served the request (replica and revision names on Container Apps, hostname locally)."""

from __future__ import annotations

import os
import socket
import time

_STARTED_AT = time.time()


def instance_info() -> dict:
    return {
        "replica": os.environ.get("CONTAINER_APP_REPLICA_NAME") or socket.gethostname(),
        "revision": os.environ.get("CONTAINER_APP_REVISION", "local"),
        "pid": os.getpid(),
        "uptime_s": round(time.time() - _STARTED_AT, 1),
    }
