# Separate Control and Push TCP connections

Each Remote uses two long-lived TCP connections from Bot to Gateway, both authenticated with `bridge.auth`: a Control Connection for request/response passthrough, and a Push Connection for fan-out events and Terminal Output Events. Rejected a single multiplexed socket because frequent terminal pushes must not stall behind large or slow control responses (TCP head-of-line blocking).
