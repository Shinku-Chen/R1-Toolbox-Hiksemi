"""Canonical LAN source rules shared by API tokens and developer access.

Rules authorize the actual socket peer, never a hostname or a forwarded header.
Every accepted network is wholly inside a supported private or loopback range.
"""

import ipaddress


_PRIVATE_V4 = tuple(ipaddress.ip_network(value) for value in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8'))
_PRIVATE_V6 = (ipaddress.ip_network('fc00::/7'), ipaddress.ip_network('::1/128'))
_MAPPED_V6 = ipaddress.ip_network('::ffff:0:0/96')
MAX_RULES = 32


def _text(value):
    return (isinstance(value, str) and 0 < len(value) <= 128
            and value == value.strip() and '%' not in value
            and not any(character.isspace() for character in value))


def normalize_peer(peer):
    """Return an allowed LAN/loopback address, with IPv4-mapped IPv6 folded."""
    if not _text(peer) or len(peer) > 64:
        return None
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    ranges = _PRIVATE_V4 if isinstance(address, ipaddress.IPv4Address) else _PRIVATE_V6
    return str(address) if any(address in network for network in ranges) else None


def normalize_rule(value):
    """Canonicalize one exact IP or subnet, rejecting partially public ranges.

    Host bits and dotted IPv4 masks are accepted. Single-host subnets become
    exact IPs so an IP and its equivalent /32 or /128 cannot evade duplicate
    detection. Mapped IPv6 subnets must fit inside the /96 mapping block.
    """
    if not _text(value):
        return None
    if '/' not in value:
        return normalize_peer(value)
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError:
        return None
    if (isinstance(network, ipaddress.IPv6Network)
            and network.prefixlen >= 96 and network.subnet_of(_MAPPED_V6)):
        network = ipaddress.ip_network((int(network.network_address) & 0xffffffff,
                                       network.prefixlen - 96), strict=False)
    ranges = _PRIVATE_V4 if isinstance(network, ipaddress.IPv4Network) else _PRIVATE_V6
    if not any(network.subnet_of(allowed) for allowed in ranges):
        return None
    return (str(network.network_address) if network.prefixlen == network.max_prefixlen
            else str(network))


def normalize_rules(values, required=False):
    """Return unique canonical LAN rules; callers choose whether empty is valid."""
    if (type(required) is not bool or not isinstance(values, list)
            or len(values) > MAX_RULES or required and not values):
        raise ValueError('来源规则须为 IP 或 IP／掩码列表，最多 32 项')
    result = [normalize_rule(value) for value in values]
    if any(value is None for value in result) or len(set(result)) != len(result):
        raise ValueError('来源规则须为不重复的局域网 IP 或 IP／掩码')
    return result


def peer_matches(peer, rules):
    """Match one actual peer; invalid or empty rules never authorize a caller."""
    address = normalize_peer(peer)
    if address is None:
        return False
    try:
        canonical = normalize_rules(rules)
    except ValueError:
        return False
    actual = ipaddress.ip_address(address)
    for rule in canonical:
        if '/' in rule:
            network = ipaddress.ip_network(rule)
            if actual.version == network.version and actual in network:
                return True
        elif address == rule:
            return True
    return False
