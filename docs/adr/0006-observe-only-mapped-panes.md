# Observe only Mapped Panes

The Gateway pushes Terminal Views only for Mapped Panes. After the Bot creates/updates a Discord channel mapping, it enables observe via the Control Connection (`bridge.observe_pane`). Lifecycle events (e.g. `pane.created`) still flow on the Push Connection so the Bot can map new Panes. Rejected observing all Herdr panes (wasted work) and observe-only-via-explicit-list without lifecycle push (harder bootstrap).
