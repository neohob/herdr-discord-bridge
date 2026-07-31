# TLS required for Gateway connections

Control and Push connections are TLS-wrapped TCP, not plaintext. Token and Terminal View traffic must not ride cleartext. Certificate trust uses fingerprint pinning (ADR-0015).
