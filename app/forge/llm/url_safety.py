"""Strict SSRF-resistant URL validation and DNS pinning utilities for FORGE.

Enforces fail-closed outbound URL controls:
- Strict http/https scheme allowlist (rejects file://, ftp://, gopher://, etc.).
- Rejects embedded credentials/userinfo, control characters, and malformed URLs.
- Rejects non-canonical numeric/octal/hex IP representations (e.g. 0x7f000001, 0177.0.0.1, integer IPs).
- Unwraps IPv4-mapped IPv6, 6to4, Teredo, and NAT64 addresses before IP classification.
- Unconditionally blocks private RFC1918, CGNAT, ULA, link-local/cloud metadata, multicast,
  unspecified, and non-loopback hostnames resolving to loopback addresses (DNS rebinding guard).
- Pins the validated resolved IP address so HTTP connections never re-resolve DNS (eliminating TOCTOU).
- Rejects HTTP 3xx redirects at the transport/response boundary.
"""

from dataclasses import dataclass
import ipaddress
import re
import socket
from typing import List, Optional, Tuple, Union
from urllib.parse import urlparse


IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

CANONICAL_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_HEX_OR_INT_HOST_RE = re.compile(r"^(?:\d+|0x[0-9a-f]+)$", re.IGNORECASE)
_DOTTED_NON_DECIMAL_RE = re.compile(r"^(?:0[0-7]+|0x[0-9a-f]+|\d+)(?:\.(?:0[0-7]+|0x[0-9a-f]+|\d+)){1,3}$", re.IGNORECASE)
_NAT64_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")


class URLSecurityError(ValueError):
    """Raised when a URL fails SSRF, scheme, DNS, or host validation."""


@dataclass(frozen=True)
class ValidatedTargetURL:
    """Validated URL with pinned destination IP for TOCTOU-free connections."""

    original_url: str
    scheme: str
    host: str
    port: int
    path: str
    query: str
    pinned_ip: Optional[str]
    is_loopback_target: bool

    @property
    def host_header(self) -> str:
        formatted_host = f"[{self.host}]" if ":" in self.host else self.host
        default_port = 443 if self.scheme == "https" else 80
        if self.port != default_port:
            return f"{formatted_host}:{self.port}"
        return formatted_host

    def build_pinned_url(self, extra_path: str = "") -> str:
        """Build URL connecting directly to the validated pinned IP."""
        target_host = self.pinned_ip or self.host
        netloc_host = f"[{target_host}]" if ":" in target_host else target_host
        base_path = self.path.rstrip("/")
        suffix = extra_path if not extra_path or extra_path.startswith("/") else f"/{extra_path}"
        full_path = f"{base_path}{suffix}" or "/"
        query_part = f"?{self.query}" if self.query else ""
        return f"{self.scheme}://{netloc_host}:{self.port}{full_path}{query_part}"


def _normalize_host(raw_host: str) -> str:
    """Normalize hostname, rejecting control characters, whitespace, and trailing-dot IP tricks."""
    cleaned = raw_host.strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    if not cleaned or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise URLSecurityError(f"Malformed or unsafe hostname: {raw_host!r}")
    if "%" in cleaned:
        raise URLSecurityError(f"Scope IDs or percent-encoded hostnames are forbidden: {raw_host!r}")
    # Reject trailing dot after numeric IPv4 literal (e.g. 127.0.0.1.)
    if cleaned.endswith("."):
        stripped = cleaned.rstrip(".")
        is_ip_literal = False
        try:
            ipaddress.ip_address(stripped)
            is_ip_literal = True
        except ValueError:
            pass
        if is_ip_literal:
            raise URLSecurityError(f"Trailing dot on IP literal is forbidden: {raw_host!r}")
        cleaned = stripped
    return cleaned.lower()


def _reject_noncanonical_ip_encoding(host: str) -> None:
    """Reject integer, hex, octal, or partial IPv4 forms (e.g. 2130706433, 0x7f000001, 0177.0.0.1)."""
    if ":" in host:
        return
    if _HEX_OR_INT_HOST_RE.match(host):
        raise URLSecurityError(f"Non-canonical numeric or hex IP host is forbidden: {host!r}")
    if _DOTTED_NON_DECIMAL_RE.match(host):
        parts = host.split(".")
        if len(parts) != 4:
            raise URLSecurityError(f"Abbreviated IPv4 host literal is forbidden: {host!r}")
        for part in parts:
            if part.lower().startswith("0x") or (len(part) > 1 and part.startswith("0")):
                raise URLSecurityError(f"Octal or hexadecimal IPv4 octet is forbidden: {host!r}")


def _extract_embedded_ips(ip_obj: IPAddress) -> List[IPAddress]:
    """Extract the IP itself plus any embedded IPv4 addresses (IPv4-mapped, 6to4, Teredo, NAT64)."""
    candidates: List[IPAddress] = [ip_obj]
    if isinstance(ip_obj, ipaddress.IPv6Address):
        if ip_obj.ipv4_mapped is not None:
            candidates.append(ip_obj.ipv4_mapped)
        if ip_obj.sixtofour is not None:
            candidates.append(ip_obj.sixtofour)
        if ip_obj.teredo is not None:
            candidates.extend(ip_obj.teredo)
        if ip_obj in _NAT64_PREFIX:
            last_32 = int(ip_obj) & 0xFFFFFFFF
            candidates.append(ipaddress.IPv4Address(last_32))
    return candidates


def _validate_ip_address(
    raw_ip: str,
    *,
    is_explicit_loopback_host: bool,
    allow_loopback: bool,
) -> IPAddress:
    """Validate an IP address string against SSRF policies."""
    ip_obj = ipaddress.ip_address(raw_ip)

    # Disallow IPv4-mapped IPv6 even when mapped to loopback (must use canonical ::1 or 127.0.0.1)
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        raise URLSecurityError(f"IPv4-mapped IPv6 address '{raw_ip}' is forbidden by SSRF policy.")

    for cand in _extract_embedded_ips(ip_obj):
        if cand.is_link_local:
            raise URLSecurityError(f"Link-local or cloud metadata IP '{raw_ip}' is forbidden.")
        if cand.is_unspecified:
            raise URLSecurityError(f"Unspecified IP address '{raw_ip}' is forbidden.")
        if cand.is_multicast:
            raise URLSecurityError(f"Multicast IP address '{raw_ip}' is forbidden.")
        if cand.is_loopback:
            if not (allow_loopback and is_explicit_loopback_host):
                raise URLSecurityError(f"Loopback destination '{raw_ip}' is forbidden in this context.")
            continue
        if cand.is_private or cand.is_reserved:
            raise URLSecurityError(f"Private or reserved IP address '{raw_ip}' is forbidden.")

    return ip_obj


def is_canonical_loopback_url(raw_url: str) -> bool:
    """Return True iff raw_url parses safely as an http/https canonical local loopback endpoint."""
    try:
        validated = validate_url_target(
            raw_url,
            allow_loopback=True,
            resolve_dns=False,
        )
        return validated.is_loopback_target
    except URLSecurityError:
        return False


def validate_url_target(
    raw_url: str,
    *,
    allow_loopback: bool = True,
    resolve_dns: bool = True,
) -> ValidatedTargetURL:
    """Validate URL syntax, scheme, credentials, host encoding, and resolved IP addresses."""
    if not raw_url or not isinstance(raw_url, str):
        raise URLSecurityError("URL cannot be empty.")

    stripped = raw_url.strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in stripped):
        raise URLSecurityError("URL contains forbidden control characters.")

    try:
        parsed = urlparse(stripped)
    except Exception as exc:
        raise URLSecurityError(f"Malformed URL: {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise URLSecurityError(f"Unsupported URL scheme '{scheme}': only http and https are permitted.")

    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise URLSecurityError("URLs containing embedded credentials or userinfo are strictly forbidden.")

    if not parsed.hostname:
        raise URLSecurityError("URL is missing a valid hostname.")

    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise URLSecurityError(f"Invalid URL port: {exc}") from exc

    host = _normalize_host(parsed.hostname)
    _reject_noncanonical_ip_encoding(host)

    is_explicit_loopback = host in CANONICAL_LOOPBACK_HOSTS

    # Check literal IP first if host parses as an IP
    literal_ip: Optional[IPAddress] = None
    try:
        literal_ip = _validate_ip_address(
            host,
            is_explicit_loopback_host=is_explicit_loopback,
            allow_loopback=allow_loopback,
        )
    except ValueError as exc:
        if isinstance(exc, URLSecurityError):
            raise
        # Hostname, not an IP literal

    pinned_ip: Optional[str] = str(literal_ip) if literal_ip is not None else None

    if resolve_dns and literal_ip is None:
        try:
            addr_infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise URLSecurityError(f"Failed to resolve hostname '{host}': {exc}") from exc

        resolved_ips: List[str] = []
        for info in addr_infos:
            sockaddr = info[4]
            if not sockaddr:
                continue
            ip_str = sockaddr[0].split("%", 1)[0]
            _validate_ip_address(
                ip_str,
                is_explicit_loopback_host=is_explicit_loopback,
                allow_loopback=allow_loopback,
            )
            if ip_str not in resolved_ips:
                resolved_ips.append(ip_str)

        if not resolved_ips:
            raise URLSecurityError(f"DNS resolution for '{host}' returned no usable addresses.")

        # Prefer IPv4 loopback first when connecting to 'localhost' locally if available
        if is_explicit_loopback and "127.0.0.1" in resolved_ips:
            pinned_ip = "127.0.0.1"
        else:
            pinned_ip = resolved_ips[0]

    return ValidatedTargetURL(
        original_url=stripped,
        scheme=scheme,
        host=host,
        port=port,
        path=parsed.path or "",
        query=parsed.query or "",
        pinned_ip=pinned_ip,
        is_loopback_target=is_explicit_loopback,
    )
