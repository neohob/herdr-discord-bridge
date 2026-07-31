"""SSH stdio ↔ remote Unix-domain socket relay (no socat required)."""

from __future__ import annotations

import shlex

# Remote Python snippet: bidirectional copy between stdio and AF_UNIX.
# Kept as a single -c argument so we don't need a file on the remote host.
_RELAY_PY = r"""
import os, sys, socket, select
path = os.path.expanduser(os.environ.get("HERDR_SOCKET_PATH", ""))
if not path:
    sys.stderr.write("HERDR_SOCKET_PATH missing\n"); sys.exit(2)
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    s.connect(path)
except Exception as e:
    sys.stderr.write(f"connect {path}: {e}\n"); sys.exit(1)
stdin = sys.stdin.buffer
stdout = sys.stdout.buffer
sockets = [s, stdin]
try:
    while True:
        r, _, _ = select.select([s, stdin], [], [])
        if stdin in r:
            data = stdin.read1(65536) if hasattr(stdin, "read1") else os.read(stdin.fileno(), 65536)
            if not data:
                break
            s.sendall(data)
        if s in r:
            data = s.recv(65536)
            if not data:
                break
            stdout.write(data); stdout.flush()
except BrokenPipeError:
    pass
finally:
    try: s.close()
    except Exception: pass
""".strip()


def relay_remote_command(herdr_socket: str) -> str:
    """Shell command run on the remote host to bridge stdio to herdr.sock."""
    sock = shlex.quote(herdr_socket)
    # Use python3 -c; pass socket via env for cleaner quoting.
    py = shlex.quote(_RELAY_PY)
    return f"HERDR_SOCKET_PATH={sock} python3 -c {py}"
