# Rebind runs in the target channel

To restore an unbound Remote, the Operator creates or opens the desired text channel and runs `/herdr rebind` there, choosing the Remote from a Select of unbound Registry entries. The Bot binds that Remote to the current channel and reconnects TLS. Rejected picking an arbitrary channel from elsewhere and auto-creating channels on rebind.
