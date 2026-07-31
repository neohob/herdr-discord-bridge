# Gateway emits plain-text terminal output

The Gateway consumes Herdr terminal observe (or equivalent), strips ANSI, coalesces updates, and pushes `bridge.terminal_output` with Discord-ready plain text. The Bot only buffers and edits the Terminal Message. Rejected: forwarding raw frames to the Bot (duplicates work per client) and Gateway-local `pane.read` polling (not a true observe stream).
