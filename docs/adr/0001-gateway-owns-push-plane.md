# Gateway owns the push plane

The Herdr plugin (Gateway) is the sole owner of Herdr `events.subscribe` and terminal observe streams; it fans out push frames to authenticated Bot TCP connections. The Bot does not passthrough-own long-lived Herdr subscriptions. Control-plane RPCs remain request/response passthrough. Chosen over Bot-owned subscribe so reconnect and multi-reader fan-out stay on the Remote, matching the Gateway’s reason to exist.
