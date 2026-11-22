"""Code shared by the website system (web/) and its ingest step.

Only two things live here, because both sides need them:
- load_config: read the repo-root db.conf file
- SshTunnel: reach Postgres on Altare when the intranet address is not routed
The collector (collector/) does not use this package.
"""
from .config import load_config
from .ssh_tunnel import SshTunnel

__all__ = ["load_config", "SshTunnel"]
