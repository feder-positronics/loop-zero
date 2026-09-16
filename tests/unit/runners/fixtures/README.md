# Containment reference

`intelflo_containment_reference.py` preserves `worker_isolated_command` and
`_worker_mount_parent_args` verbatim from `feder-positronics/intelflo`, commit
`5baa7f25db75069c9b2de9c7c7ef4a1353aed155`, `scripts/util/agent_dispatch.py`.
This is the parent of the consumer cutover commit `b649223cc`.
The functions are historical reference data, not production containment code.

Only imports and explicit failing dependency stubs were added. The test supplies
the executable and runtime-root seams and fixes filesystem observations to the
original golden scenario. Repair mounts are outside that scenario and fail if
accidentally exercised. The JSON golden is retained unchanged.

Do not regenerate this reference from current package code or a live checkout:
that would remove the independent pre-extraction comparison. Any intentional
reference update must name its source commit and explain the golden change.
