from dataclasses import dataclass
import os
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


def validate_proxy_url(value):
    proxy = str(value or '').strip()
    if not proxy:
        raise ValueError('proxy URL is required')
    parsed = urllib.parse.urlsplit(proxy if '://' in proxy else f'http://{proxy}')
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        raise ValueError('only http/https proxy URLs are supported')
    if parsed.username or parsed.password:
        raise ValueError('proxy credentials are not stored in CFR config')
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def resolve_cfr_proxy(environment=None, system_proxies=None, config_store=None):
    """Resolve CFR's effective proxy with persistent user policy.

    ``direct`` and ``proxy`` are explicit user choices and therefore outrank
    inherited HTTP(S)_PROXY variables.  ``auto`` is the only mode that consults
    environment and system proxy discovery.
    """
    env = dict(os.environ if environment is None else environment)
    if config_store is None:
        from cfr.feishu.credentials import LocalConfigStore
        config_store = LocalConfigStore()
    policy = config_store.get_network_policy()
    mode = policy.get('mode', 'auto')
    if mode == 'direct':
        return ProxyResolution('direct', None, None, _env_value(env, 'NO_PROXY', 'no_proxy'), 'persistent')
    if mode == 'proxy':
        proxy = validate_proxy_url(policy.get('proxy_url'))
        return ProxyResolution('proxy', proxy, proxy, _env_value(env, 'NO_PROXY', 'no_proxy'), 'persistent')
    return resolve_proxy(environment=env, system_proxies=system_proxies)


def proxy_child_env(resolution: ProxyResolution):
    proxy_keys = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')
    if resolution.mode != 'proxy' or not resolution.selected_proxy:
        # AppServerClient overlays this mapping onto os.environ. Empty values
        # are therefore required to make an explicit Direct policy real rather
        # than silently inheriting the parent process proxy.
        environment = {key: '' for key in proxy_keys}
        environment.update({
            'NO_PROXY': resolution.no_proxy or 'localhost,127.0.0.1,::1',
            'no_proxy': resolution.no_proxy or 'localhost,127.0.0.1,::1',
        })
        return environment
    environment = {
        'HTTP_PROXY': resolution.http_proxy or resolution.selected_proxy,
        'HTTPS_PROXY': resolution.https_proxy or resolution.selected_proxy,
        'http_proxy': resolution.http_proxy or resolution.selected_proxy,
        'https_proxy': resolution.https_proxy or resolution.selected_proxy,
        'ALL_PROXY': '',
        'all_proxy': '',
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
