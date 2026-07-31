# Unbind keeps credentials; Rebind restores the channel

Deleting a Remote Channel (or explicit unbind) disconnects TLS and clears Discord mappings, but retains host/port/token/fingerprint in the Remote Registry. Operators reattach via a Rebind action onto a new text channel without re-pasting secrets. Rejected wiping the Registry on every channel delete (punishes mistakes) and soft-delete/restore state machines (extra complexity).
