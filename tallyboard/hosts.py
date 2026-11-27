"""Machine names: map Tailscale IPs to host names, canonicalise spellings,
and know which tailnet machines are online. Falls back gracefully when the
`tailscale` CLI isn't available (then only reverse DNS is used)."""
import ipaddress
import json
import socket
import subprocess
import threading
import time

from .store import hostname

_TTL = 30.0
_lock = threading.Lock()
_cache = {"at": 0.0, "by_ip": {}, "by_name": {}, "online": {}, "self_ips": set(), "self_dns": None}


def _short(dns):
    return (dns or "").rstrip(".").split(".")[0].lower()


def _refresh(force=False):
    with _lock:
        if not force and time.time() - _cache["at"] < _TTL:
            return _cache
        by_ip, by_name, online, self_ips, self_dns = {}, {}, {}, set(), None
        try:
            out = subprocess.run(["tailscale", "status", "--json"], capture_output=True,
                                 text=True, timeout=5).stdout
            data = json.loads(out or "{}")
        except (OSError, subprocess.SubprocessError, ValueError):
            data = {}
        self_node = data.get("Self") or {}
        if self_node.get("DNSName"):
            self_dns = str(self_node["DNSName"]).rstrip(".")
        nodes = [("self", self_node)] + \
                [("peer", p) for p in (data.get("Peer") or {}).values()]
        for kind, node in nodes:
            name = node.get("HostName")
            if not name:
                continue
            for ip in node.get("TailscaleIPs") or []:
                by_ip[ip] = name
                if kind == "self":
                    self_ips.add(ip)
            by_name[name.lower()] = name
            by_name.setdefault(_short(node.get("DNSName")), name)
            online[name] = True if kind == "self" else bool(node.get("Online"))
        _cache.update(at=time.time(), by_ip=by_ip, by_name=by_name, online=online,
                      self_ips=self_ips, self_dns=self_dns)
        return _cache


def self_dns():
    """Tailscale MagicDNS FQDN for this host (e.g. 'workstation.tailnet.ts.net'), or None."""
    return _refresh().get("self_dns")


def tailscale_serve_url(target_port):
    """Detect if `tailscale serve` is proxying to target_port and return the HTTPS URL."""
    c = _refresh()
    # Allow test mocks or custom overrides in _cache
    if "serve_urls" in c:
        return c["serve_urls"].get(target_port)
    try:
        out = subprocess.run(["tailscale", "serve", "status", "--json"],
                             capture_output=True, text=True, timeout=3).stdout
        data = json.loads(out or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    web = data.get("Web") or {}
    needle = f":{target_port}"
    for host_port, spec in web.items():
        handlers = spec.get("Handlers") or {}
        for path, target in handlers.items():
            proxy = target.get("Proxy") or ""
            if needle in proxy:
                host, _, port = host_port.partition(":")
                prefix = f"https://{host}" + (f":{port}" if port and port != "443" else "")
                clean_path = path.rstrip("/")
                return f"{prefix}{clean_path}/"
    return None


def canonical(name):
    """Tailnet spelling of a host name (e.g. 'gpu-win11' -> 'GPU-NODE')."""
    if not name:
        return name
    c = _refresh()
    key = str(name).strip().lower()
    return c["by_name"].get(key) or c["by_name"].get(_short(key)) or name


def is_local_ip(ip):
    try:
        if ipaddress.ip_address(ip).is_loopback:
            return True
    except ValueError:
        return False
    return ip in _refresh()["self_ips"]


def host_for_ip(ip):
    """Host name for a client address, or None when it is this machine/unknown."""
    if not ip or is_local_ip(ip):
        return None
    name = _refresh()["by_ip"].get(ip)
    if name:
        return name
    try:
        return canonical(socket.gethostbyaddr(ip)[0].split(".")[0])
    except (OSError, UnicodeError):
        return None


def online_map():
    """{host: True/False} for tailnet machines; this host is always True."""
    m = dict(_refresh()["online"])
    m.setdefault(hostname(), True)
    return m
