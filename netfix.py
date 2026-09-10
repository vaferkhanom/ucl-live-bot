"""DNS fallback: if the system resolver returns a bogus/private IP for a host,
resolve via DNS-over-HTTPS and patch socket.getaddrinfo for that host only.
Keeps the bot alive on hosts with broken/poisoned DNS."""
import ipaddress
import json
import socket
import threading
import urllib.request

_lock = threading.Lock()
_patched = set()


def _doh(host):
    for url in (f"https://dns.google/resolve?name={host}&type=A",
                f"https://cloudflare-dns.com/dns-query?name={host}&type=A"):
        try:
            req = urllib.request.Request(url, headers={"accept": "application/dns-json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            for a in data.get("Answer", []):
                if a.get("type") == 1:
                    ip = a.get("data")
                    addr = ipaddress.ip_address(ip)
                    if not (addr.is_private or addr.is_reserved or addr.is_loopback):
                        return ip
        except Exception:  # noqa: BLE001
            continue
    return None


def ensure(host):
    """Return True when a healthy resolver is active for host (patching if needed)."""
    with _lock:
        if host in _patched:
            return True
        ip = _doh(host)
        if not ip:
            return False
        orig = socket.getaddrinfo

        def patched(h, port, *a, **kw):
            if h == host:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
            return orig(h, port, *a, **kw)

        socket.getaddrinfo = patched
        _patched.add(host)
        print(f"[netfix] {host} -> {ip} (DoH fallback active)", flush=True)
        return True
