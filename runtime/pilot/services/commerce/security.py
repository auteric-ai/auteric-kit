"""Authentication and SSRF-safe discovery fetch. No arbitrary merchant API proxying."""

import hashlib
import hmac
import ipaddress
import secrets
import socket
from urllib.parse import urlsplit

import httpx


def password_hash(password):
    salt = secrets.token_hex(16)
    result = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ":" + result


def check_password(password, stored):
    salt, expected = stored.split(":")
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return hmac.compare_digest(expected, actual)


def domain_name(value):
    name = value.strip().lower().rstrip(".")
    if "://" in name:
        parsed = urlsplit(name)
        if (
            parsed.scheme != "https"
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username
        ):
            raise ValueError("Use the store hostname, not a path or credential-bearing URL")
        name = parsed.netloc
    name = name.encode("idna").decode()
    if (
        not name
        or len(name) > 253
        or "." not in name
        or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in name)
    ):
        raise ValueError("Enter a valid public store hostname")
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        raise ValueError("Use a domain, not an IP address")
    if any(not part or part.startswith("-") or part.endswith("-") for part in name.split(".")):
        raise ValueError("Invalid domain label")
    return name


async def fetch_discovery(domain):
    """Pin TLS hostname to a validated public IP, no redirects or proxy env vars.

    Async resolver work is bounded by the calling route timeout. No localhost exceptions.
    """
    import asyncio

    name = domain_name(domain)
    addresses = await asyncio.to_thread(socket.getaddrinfo, name, 443, type=socket.SOCK_STREAM)
    ips = sorted(
        {row[4][0] for row in addresses},
        key=lambda value: (ipaddress.ip_address(value).version, value),
    )
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError("Discovery DNS must resolve only to public addresses")
    ip = ips[0]
    host = "[" + ip + "]" if ":" in ip else ip
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        async with client.stream(
            "GET", f"https://{host}/.well-known/ucp", headers={"Host": name}, extensions={"sni_hostname": name}
        ) as response:
            response.raise_for_status()
            data = bytearray()
            async for part in response.aiter_bytes():
                data.extend(part)
                if len(data) > 1024 * 1024:
                    raise ValueError("Discovery response exceeds 1 MB")
    import json

    return json.loads(data)
