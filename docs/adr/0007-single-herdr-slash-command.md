# Single `/herdr` slash command with subcommands

Discord exposes one top-level `/herdr`; actions are subcommands (optionally grouped). We do not invent top-level `/herdr-pane` / `/herdr-workspace` families—Herdr’s CLI is `herdr <resource> <action>`, not separate `herdr-*` binaries. Structural ops may run from a Control Channel; Pane-scoped ops may omit ids when invoked inside a Mapped Pane channel.
