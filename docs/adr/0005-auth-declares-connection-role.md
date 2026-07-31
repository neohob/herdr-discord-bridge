# `bridge.auth` declares connection role

Both TCP sockets authenticate with `bridge.auth`, including `role: "control" | "push"`. One listen port; role is fixed at auth time. Rejected a post-auth bind frame (extra round trip) and separate ports per role (doubled firewall/config).
