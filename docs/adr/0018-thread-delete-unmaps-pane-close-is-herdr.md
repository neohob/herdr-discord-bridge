# Thread delete unmaps; Pane close is a Herdr command

Deleting a Pane Thread only stops observe and clears Discord mapping; Sync can recreate it. Truly removing a Pane is a Herdr control-plane close via `/herdr`, after which the Bot removes or archives the Thread. Rejected treating Thread deletion as pane teardown.
