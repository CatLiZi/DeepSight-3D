"""Limit browser-origin requests to the loopback UI; no OS authentication."""
import ipaddress
from urllib.parse import urlsplit


def trusted_loopback_request(connection):
    """Permit the same local browser origin and non-browser local clients.

    Browser cross-origin forms and WebSockets carry Origin. Requests without
    Origin remain available to local CLI tools. This is not an authentication
    boundary against other processes on the same machine.
    """
    try:
        target=connection.url
        host=(target.hostname or '').lower()
        client=getattr(connection,'client',None)
        test_client=host=='testserver' and getattr(client,'host',None)=='testclient'
        try:loopback=ipaddress.ip_address(host).is_loopback
        except ValueError:loopback=False
        if not (host=='localhost' or loopback or test_client):return False
        origin=connection.headers.get('origin')
        if origin is None:return True
        parsed=urlsplit(origin)
        if (parsed.scheme not in ('http','https') or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or
                parsed.path not in ('','/') or parsed.query or parsed.fragment):return False
        scheme='https' if target.scheme in ('https','wss') else 'http' if target.scheme in ('http','ws') else None
        default_port=443 if scheme=='https' else 80
        return (parsed.scheme==scheme and parsed.hostname.lower()==host and
                (parsed.port or default_port)==(target.port or default_port))
    except (ValueError,TypeError):
        return False
