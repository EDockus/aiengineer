"""``python -m ticket_to_pr`` runs the Invocations server.

The container ENTRYPOINT is :mod:`ticket_to_pr.launch`, which scrubs the managed-identity
plumbing out of the exec-time environment and then execs this module (see that module's
docstring). ``main`` here drains the handed-off credentials back into ``os.environ`` before
anything else.
"""

from __future__ import annotations

from .main import main

if __name__ == "__main__":
    raise SystemExit(main())
