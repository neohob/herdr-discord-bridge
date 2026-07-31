# Trust Gateway TLS by certificate fingerprint pin

Plugin `setup` generates a self-signed certificate and prints its SHA-256 fingerprint. The Bot stores that fingerprint in the Remote Registry and verifies the Gateway cert against the pin on each Control/Push connect. Rejected skip-verify (defeats TLS) and mandatory public CA (poor fit for private Remotes).
