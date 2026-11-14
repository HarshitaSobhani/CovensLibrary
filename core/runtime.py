"""Process-state files shared by the server and the Windows launcher (standard library only).

Protocol (all files live in COVEN_RUN_DIR, a folder only the owner's Windows account can open):
* ``server.json``  - written by the server when it is listening: pid, port, start time.
* ``stop.request`` - written by "Stop Library"; the server notices it, stops accepting requests,
  lets running requests finish, removes ``server.json`` and exits.
Stopping never kills a process by name: an unresponsive server is only ever stopped by the exact
process the server recorded about itself (see deploy/windows/Stop-CovensLibrary.ps1).
"""

import http.client
import json
import os
import socket
import time
from pathlib import Path

INSTANCE_FILE = "server.json"
STOP_FILE = "stop.request"
HEALTH_PATH = "/healthz/"


def run_dir(base=None):
    path = Path(base or os.environ.get("COVEN_RUN_DIR") or "run")
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_instance(base=None):
    try:
        return json.loads((run_dir(base) / INSTANCE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_instance(port, base=None):
    data = {"pid": os.getpid(), "port": port, "started": time.time(), "host": "127.0.0.1"}
    target = run_dir(base) / INSTANCE_FILE
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temp, target)  # atomic: a reader never sees half a file
    return data


def clear_instance(base=None, pid=None):
    """Remove the instance file (only if it is this process's, when ``pid`` is given)."""
    current = read_instance(base)
    if current is None or (pid is not None and current.get("pid") != pid):
        return
    try:
        (run_dir(base) / INSTANCE_FILE).unlink()
    except FileNotFoundError:
        pass


def request_stop(base=None):
    (run_dir(base) / STOP_FILE).write_text(str(time.time()), encoding="utf-8")


def stop_requested(base=None):
    return (run_dir(base) / STOP_FILE).exists()


def clear_stop(base=None):
    try:
        (run_dir(base) / STOP_FILE).unlink()
    except FileNotFoundError:
        pass


def port_in_use(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        return probe.connect_ex((host, port)) == 0


def health(port, host="127.0.0.1", timeout=3):
    """'ok' (our application answered healthy), 'unhealthy' (it answered but reported a problem),
    'foreign' (something else answers on the port) or 'down' (nothing listens). Loopback only."""
    if host != "127.0.0.1":
        raise ValueError("the health probe talks to 127.0.0.1 only")
    if not port_in_use(port, host):
        return "down"
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", HEALTH_PATH, headers={"Host": f"{host}:{port}"})
        response = connection.getresponse()
        body = json.loads(response.read(2000).decode("utf-8", "replace"))
    except (OSError, ValueError, http.client.HTTPException):
        return "foreign"
    finally:
        connection.close()
    if not isinstance(body, dict) or body.get("app") != "coven-library":
        return "foreign"
    return "ok" if response.status == 200 and body.get("status") == "ok" else "unhealthy"
