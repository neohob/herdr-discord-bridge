# Terminal View is a sliding snapshot

`bridge.terminal_output` carries the latest complete Terminal View: a sliding recent window (N lines / viewport), not the full session scrollback and not append-only deltas. The Bot replaces its buffer on each event. Chosen so Discord always shows current state, reconnects self-heal, and history dump bandwidth is avoided.
