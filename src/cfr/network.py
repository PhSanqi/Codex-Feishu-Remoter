from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping
import urllib.parse
import urllib.request


@dataclass(frozen=True)
class ProxyResolution:
    mode: str
    http_proxy: str | None
    https_proxy: str | None
    no_proxy: str | None
    source: str

    @property
    def selected_proxy(self):
        return self.https_proxy or self.http_proxy


def _env_value(environment: Mapping[str, str], upper: str, lower: str):
    return environment.get(upper) or environment.get(lower)


def _proxy_url(value):
    value = str(value or '').strip()
    if not value:
        return None
    return value if '://' in value else f'http://{value}'


def _parse_wininet_proxy_server(value):
    value = str(value or '').strip()
    if not value:
        return {}
    if '=' not in value:
        proxy = _proxy_url(value)
        return {'http': proxy, 'https': proxy} if proxy else {}
    proxies = {}
    for entry in value.split(';'):
        if '=' not in entry:
            continue
        scheme, address = entry.split('=', 1)
        scheme = scheme.strip().lower()
        if scheme in {'http', 'https'}:
            proxy = _proxy_url(address)
            if proxy:
                proxies[scheme] = proxy
    return proxies


def _wininet_proxies():
    if os.name != 'nt':
        return {}
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
            enabled = int(winreg.QueryValueEx(key, 'ProxyEnable')[0] or 0)
            if not enabled:
                return {}
            proxies = _parse_wininet_proxy_server(winreg.QueryValueEx(key, 'ProxyServer')[0])
            try:
                override = str(winreg.QueryValueEx(key, 'ProxyOverride')[0] or '')
            except OSError:
                override = ''
    except (OSError, TypeError, ValueError):
        return {}
    bypass = [item.strip() for item in override.split(';') if item.strip() and item.strip().lower() != '<local>']
    if bypass:
        proxies['no'] = ','.join(bypass)
    return proxies


def resolve_proxy(explicit=None, environment=None, system_proxies=None):
    environment = dict(os.environ if environment is None else environment)
    if explicit:
        http_proxy = explicit.get('http') or explicit.get('http_proxy')
        https_proxy = explicit.get('https') or explicit.get('https_proxy') or http_proxy
        return ProxyResolution('proxy', http_proxy, https_proxy, explicit.get('no_proxy') or explicit.get('NO_PROXY'), 'explicit')
    http_proxy = _env_value(environment, 'HTTP_PROXY', 'http_proxy')
    https_proxy = _env_value(environment, 'HTTPS_PROXY', 'https_proxy')
    no_proxy = _env_value(environment, 'NO_PROXY', 'no_proxy')
    if http_proxy or https_proxy:
        return ProxyResolution('proxy', http_proxy, https_proxy or http_proxy, no_proxy, 'environment')
    system = dict(urllib.request.getproxies() if system_proxies is None else system_proxies)
    http_proxy = system.get('http')
    https_proxy = system.get('https') or http_proxy
    if http_proxy or https_proxy:
        return ProxyResolution('proxy', http_proxy, https_proxy, system.get('no') or system.get('no_proxy'), 'system')
    if system_proxies is None:
        wininet = _wininet_proxies()
        http_proxy = wininet.get('http')
        https_proxy = wininet.get('https') or http_proxy
        if http_proxy or https_proxy:
            return ProxyResolution('proxy', http_proxy, https_proxy, wininet.get('no'), 'wininet')
    return ProxyResolution('direct', None, None, no_proxy, 'direct')


def proxy_child_env(resolution: ProxyResolution):
    if resolution.mode != 'proxy' or not resolution.selected_proxy:
        return None
    environment = {
        'HTTP_PROXY': resolution.http_proxy or resolution.selected_proxy,
        'HTTPS_PROXY': resolution.https_proxy or resolution.selected_proxy,
        'http_proxy': resolution.http_proxy or resolution.selected_proxy,
        'https_proxy': resolution.https_proxy or resolution.selected_proxy,
        'NO_PROXY': resolution.no_proxy or 'localhost,127.0.0.1,::1',
        'no_proxy': resolution.no_proxy or 'localhost,127.0.0.1,::1',
    }
    return environment


def sanitized_proxy_url(value):
    if not value:
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        if not parsed.hostname:
            return '[REDACTED]'
        host = parsed.hostname
        if ':' in host and not host.startswith('['):
            host = f'[{host}]'
        port = f':{parsed.port}' if parsed.port else ''
        return f'{parsed.scheme}://{host}{port}'
    except Exception:
        return '[REDACTED]'


def proxy_endpoint(value):
    parsed = urllib.parse.urlsplit(value or '')
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('only http/https proxy URLs are supported')
    return parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)
