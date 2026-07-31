# Remote Registry is the runtime source of truth

Remotes registered via `/herdr` (host/port/token + Remote Channel binding) persist in the Bot’s registry (e.g. cache). That registry drives Gateway connections. `config.yaml` holds Discord/bot settings and may seed remotes on first run; afterwards the registry wins. Rejected yaml-only remotes (fights Discord-side registration) and merge-without-winner (conflict-prone).
