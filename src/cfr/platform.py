from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PureWindowsPath
import platform as platform_module
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace


@dataclass(frozen=True)
class PlatformCapabilities:
    os_family: str
    platform_name: str
    python_version: str
    home_directory: Path
    default_cfr_data_directory: Path
    codex_launcher_supported: bool
    app_server_stdio_supported: bool
    rollout_watch_supported: bool
    desktop_continuity_live_validated: bool | None


_DESKTOP_RUNTIME_CACHE_LOCK = threading.RLock()
_DESKTOP_RUNTIME_CACHE: tuple[float, dict] | None = None
_CODEXHOST_VERSION_CACHE: tuple[str, float, str | None] | None = None


def _invalidate_desktop_runtime_cache():
    global _DESKTOP_RUNTIME_CACHE
    with _DESKTOP_RUNTIME_CACHE_LOCK:
        _DESKTOP_RUNTIME_CACHE = None


def resolve_codexhost_command(environment=None):
    """Resolve an executable CodexHost launcher without selecting PowerShell scripts."""
    environment = os.environ if environment is None else environment
    configured = str(environment.get('CFR_CODEXHOST_BIN') or '').strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file() and path.suffix.lower() in {'.exe', '.cmd', '.bat'}:
            return str(path.resolve())
    search_path = environment.get('PATH')
    # PowerShell execution policy can reject codexhost.ps1. Prefer a native
    # executable or cmd shim explicitly so CFR does not depend on shell policy.
    for name in ('codexhost.exe', 'codexhost.cmd', 'codexhost.bat'):
        found = shutil.which(name, path=search_path)
        if found:
            # shutil.which may be mocked with a Windows path while CFR is
            # being validated from Linux/WSL.  Path.resolve() would then turn
            # ``C:/...`` into a bogus POSIX path rooted at the checkout.
            # Preserve native Windows syntax whenever a drive-qualified path
            # is returned; this is also what callers expect on Windows.
            if len(found) >= 3 and found[1] == ':' and found[2] in {'/', '\\'}:
                return str(PureWindowsPath(found))
            return str(Path(found).resolve())
    return None


def _codexhost_version(command, *, runner=None):
    global _CODEXHOST_VERSION_CACHE
    if not command:
        return None
    now = time.monotonic()
    if runner is None:
        with _DESKTOP_RUNTIME_CACHE_LOCK:
            if _CODEXHOST_VERSION_CACHE and _CODEXHOST_VERSION_CACHE[0] == str(command) and now - _CODEXHOST_VERSION_CACHE[1] < 60:
                return _CODEXHOST_VERSION_CACHE[2]
    run = runner or subprocess.run
    command_path = Path(command)
    invocation = [str(command_path), '--version']
    try:
        completed = run(
            invocation,
            timeout=5,
            check=False,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        value = None
    else:
        lines = str(completed.stdout or completed.stderr or '').strip().splitlines()
        value = lines[-1].strip() if completed.returncode == 0 and lines else None
    if runner is None:
        with _DESKTOP_RUNTIME_CACHE_LOCK:
            _CODEXHOST_VERSION_CACHE = (str(command), time.monotonic(), value)
    return value


def codex_desktop_runtime_snapshot(*, system=None, runner=None, cache_ttl=3.0):
    """Return the current stock/CodexHost Desktop ownership without modifying it."""
    global _DESKTOP_RUNTIME_CACHE
    if os_family(system) != 'windows':
        return {
            'supported': False,
            'running': False,
            'mode': 'unsupported',
            'desktop_pids': [],
            'shim_pids': [],
            'codexhost_available': False,
            'codexhost_command': None,
            'codexhost_version': None,
        }
    now = time.monotonic()
    if runner is None and cache_ttl > 0:
        with _DESKTOP_RUNTIME_CACHE_LOCK:
            if _DESKTOP_RUNTIME_CACHE and now - _DESKTOP_RUNTIME_CACHE[0] < cache_ttl:
                return dict(_DESKTOP_RUNTIME_CACHE[1])
    run = runner or subprocess.run
    script = (
        "$pkg=Get-AppxPackage OpenAI.Codex -ErrorAction SilentlyContinue; "
        "$root=if($pkg){$pkg.InstallLocation}else{''}; "
        "$all=@(Get-CimInstance Win32_Process); "
        "$ids=New-Object 'System.Collections.Generic.HashSet[int]'; "
        "foreach($p in $all){ if($p.Name -eq 'ChatGPT.exe' -and $p.ExecutablePath -and $root -and "
        "$p.ExecutablePath.StartsWith($root,[StringComparison]::OrdinalIgnoreCase)){[void]$ids.Add([int]$p.ProcessId)} }; "
        "$changed=$true; while($changed){$changed=$false; foreach($p in $all){ "
        "if($ids.Contains([int]$p.ProcessId) -and $p.ParentProcessId -and -not $ids.Contains([int]$p.ParentProcessId)){"
        "$parent=$all|Where-Object ProcessId -eq $p.ParentProcessId|Select-Object -First 1; "
        "if($parent -and $parent.Name -eq 'ChatGPT.exe'){[void]$ids.Add([int]$parent.ProcessId);$changed=$true}}; "
        "if($ids.Contains([int]$p.ParentProcessId) -and -not $ids.Contains([int]$p.ProcessId)){[void]$ids.Add([int]$p.ProcessId);$changed=$true} "
        "} }; "
        "$desktop=@($all|Where-Object{$ids.Contains([int]$_.ProcessId) -and $_.Name -eq 'ChatGPT.exe'}|ForEach-Object{[int]$_.ProcessId}); "
        "$shim=@($all|Where-Object{$ids.Contains([int]$_.ProcessId) -and $_.Name -eq 'codexhost-shim.exe'}|ForEach-Object{[int]$_.ProcessId}); "
        "$mode=if($shim.Count -gt 0){'codexhost'}elseif($desktop.Count -gt 0){'stock'}else{'stopped'}; "
        "[pscustomobject]@{running=($desktop.Count -gt 0);mode=$mode;desktop_pids=$desktop;shim_pids=$shim}|ConvertTo-Json -Compress"
    )
    payload = {'running': False, 'mode': 'unknown', 'desktop_pids': [], 'shim_pids': []}
    try:
        completed = run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
            timeout=7,
            check=False,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            **hidden_subprocess_kwargs(),
        )
        if completed.returncode == 0:
            lines = [line.strip() for line in str(completed.stdout or '').splitlines() if line.strip()]
            if lines:
                parsed = json.loads(lines[-1])
                if isinstance(parsed, dict):
                    payload.update(parsed)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    codexhost = resolve_codexhost_command()
    result = {
        'supported': True,
        'running': bool(payload.get('running')),
        'mode': str(payload.get('mode') or 'unknown'),
        'desktop_pids': [int(value) for value in payload.get('desktop_pids') or ()],
        'shim_pids': [int(value) for value in payload.get('shim_pids') or ()],
        'codexhost_available': bool(codexhost),
        'codexhost_command': codexhost,
        'codexhost_version': _codexhost_version(codexhost) if codexhost else None,
    }
    if runner is None and cache_ttl > 0:
        with _DESKTOP_RUNTIME_CACHE_LOCK:
            _DESKTOP_RUNTIME_CACHE = (time.monotonic(), dict(result))
    return result


def _desktop_handoff(thread_id, *, restart, launcher='auto', system=None, opener=None, runner=None, codexhost_command=None):
    if os_family(system) != 'windows':
        raise NotImplementedError('Codex Desktop thread handoff is unsupported on this platform')
    thread_id = str(thread_id or '').strip()
    if not thread_id or any(character.isspace() for character in thread_id) or '/' in thread_id or '\\' in thread_id:
        raise ValueError('Codex Desktop thread id is invalid')
    preference = str(launcher or 'auto').strip().lower()
    if preference not in {'auto', 'codexhost', 'stock'}:
        raise ValueError('Codex Desktop launcher must be auto, codexhost, or stock')
    uri = f'codex://threads/{thread_id}'
    if opener is not None and not restart:
        try:
            opener(uri)
        except OSError as error:
            raise RuntimeError('Codex Desktop could not open the thread') from error
        return uri

    host_command = codexhost_command or resolve_codexhost_command()
    run = runner or subprocess.run
    env = os.environ.copy()
    env['CFR_CODEX_URI'] = uri
    env['CFR_CODEX_LAUNCHER'] = preference
    if host_command:
        env['CFR_CODEXHOST_CMD'] = str(host_command)
    script = (
        "$pkg=Get-AppxPackage OpenAI.Codex -ErrorAction Stop; $root=$pkg.InstallLocation; "
        "$all=@(Get-CimInstance Win32_Process); "
        "$ids=New-Object 'System.Collections.Generic.HashSet[int]'; "
        "foreach($p in $all){if($p.Name -eq 'ChatGPT.exe' -and $p.ExecutablePath -and "
        "$p.ExecutablePath.StartsWith($root,[StringComparison]::OrdinalIgnoreCase)){[void]$ids.Add([int]$p.ProcessId)}}; "
        "$changed=$true; while($changed){$changed=$false; foreach($p in $all){ "
        "if($ids.Contains([int]$p.ProcessId) -and $p.ParentProcessId -and -not $ids.Contains([int]$p.ParentProcessId)){"
        "$parent=$all|Where-Object ProcessId -eq $p.ParentProcessId|Select-Object -First 1; "
        "if($parent -and $parent.Name -eq 'ChatGPT.exe'){[void]$ids.Add([int]$parent.ProcessId);$changed=$true}}; "
        "if($ids.Contains([int]$p.ParentProcessId) -and -not $ids.Contains([int]$p.ProcessId)){[void]$ids.Add([int]$p.ProcessId);$changed=$true} "
        "} }; "
        "$running=($ids.Count -gt 0); "
        "$oldShim=@($all|Where-Object{$ids.Contains([int]$_.ProcessId) -and $_.Name -eq 'codexhost-shim.exe'}|ForEach-Object{[int]$_.ProcessId}); "
        "$managed=($oldShim.Count -gt 0); "
        "$useHost=if($env:CFR_CODEX_LAUNCHER -eq 'codexhost'){$true}elseif($env:CFR_CODEX_LAUNCHER -eq 'stock'){$false}elseif($running){$managed}else{[bool]$env:CFR_CODEXHOST_CMD}; "
        "if($useHost -and -not $env:CFR_CODEXHOST_CMD){Write-Error 'CodexHost launcher was not found';exit 42}; "
        + (
            "foreach($oldId in @($ids)){Stop-Process -Id $oldId -Force -ErrorAction SilentlyContinue}; "
            "$stopDeadline=(Get-Date).AddSeconds(15); $alive=@(); do{ "
            "$alive=@(); foreach($oldId in @($ids)){if(Get-Process -Id $oldId -ErrorAction SilentlyContinue){$alive += $oldId}}; "
            "if($alive.Count -eq 0){break}; Start-Sleep -Milliseconds 150 "
            "}while((Get-Date) -lt $stopDeadline); "
            "if($alive.Count -gt 0){Write-Error 'Previous Codex Desktop process tree did not exit';exit 44}; "
            if restart else ""
        )
        + "if($useHost -and (-not $running -or " + ("$true" if restart else "$false") + ")){ "
        "$hostExt=[IO.Path]::GetExtension($env:CFR_CODEXHOST_CMD); "
        "if($hostExt -in @('.cmd','.bat')){ "
        "$psi=New-Object System.Diagnostics.ProcessStartInfo; $psi.FileName=$env:COMSPEC; "
        "$psi.Arguments='/d /s /c \"\"'+$env:CFR_CODEXHOST_CMD+'\"\"'; "
        "$psi.UseShellExecute=$false; $psi.CreateNoWindow=$true; [void][System.Diagnostics.Process]::Start($psi) "
        "}else{Start-Process -FilePath $env:CFR_CODEXHOST_CMD -WindowStyle Hidden}; "
        "$deadline=(Get-Date).AddSeconds(30); $ready=$false; while((Get-Date) -lt $deadline){ "
        "$now=@(Get-CimInstance Win32_Process); $newIds=New-Object 'System.Collections.Generic.HashSet[int]'; "
        "foreach($p in $now){if($p.Name -eq 'ChatGPT.exe' -and $p.ExecutablePath -and $p.ExecutablePath.StartsWith($root,[StringComparison]::OrdinalIgnoreCase)){[void]$newIds.Add([int]$p.ProcessId)}}; "
        "$treeChanged=$true; while($treeChanged){$treeChanged=$false; foreach($p in $now){if($newIds.Contains([int]$p.ParentProcessId) -and -not $newIds.Contains([int]$p.ProcessId)){[void]$newIds.Add([int]$p.ProcessId);$treeChanged=$true}}}; "
        "$probe=@($now|Where-Object{$newIds.Contains([int]$_.ProcessId) -and $_.Name -eq 'codexhost-shim.exe' -and $oldShim -notcontains [int]$_.ProcessId}); "
        "if($probe.Count -gt 0){$ready=$true;break}; Start-Sleep -Milliseconds 250 }; "
        "if(-not $ready){Write-Error 'CodexHost did not become ready';exit 43}; Start-Sleep -Milliseconds 500 }; "
        "Start-Process -FilePath $env:CFR_CODEX_URI"
    )
    try:
        result = run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
            env=env,
            timeout=45 if restart or host_command else 10,
            check=False,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError('Codex Desktop could not open the thread') from error
    if int(getattr(result, 'returncode', 1)) != 0:
        detail = str(getattr(result, 'stderr', '') or '').strip()
        raise RuntimeError(f'Codex Desktop handoff failed{": " + detail if detail else ""}')
    if restart:
        _invalidate_desktop_runtime_cache()
    return uri


def os_family(system=None):
    value = (system or platform_module.system()).lower()
    if value.startswith('win'):
        return 'windows'
    if value == 'darwin':
        return 'darwin'
    if value == 'linux':
        return 'linux'
    return 'unknown'


def open_codex_desktop_thread(thread_id: str, *, launcher='auto', system=None, opener=None, runner=None, codexhost_command=None):
    """Ask Windows to open one existing Codex Desktop thread."""
    return _desktop_handoff(
        thread_id,
        restart=False,
        launcher=launcher,
        system=system,
        opener=opener,
        runner=runner,
        codexhost_command=codexhost_command,
    )


def restart_codex_desktop_thread(thread_id: str, *, launcher='auto', system=None, runner=None, codexhost_command=None):
    """Restart Codex Desktop while preserving CodexHost ownership when present."""
    return _desktop_handoff(
        thread_id,
        restart=True,
        launcher=launcher,
        system=system,
        runner=runner,
        codexhost_command=codexhost_command,
    )


def detect_platform_capabilities(desktop_continuity_live_validated=None, home_directory=None):
    family = os_family()
    home = Path(home_directory).expanduser() if home_directory else Path.home()
    supported = family in {'windows', 'darwin', 'linux'}
    return PlatformCapabilities(
        os_family=family,
        platform_name=platform_module.platform(),
        python_version=platform_module.python_version(),
        home_directory=home,
        default_cfr_data_directory=Path.cwd(),
        codex_launcher_supported=supported,
        app_server_stdio_supported=supported,
        rollout_watch_supported=supported,
        desktop_continuity_live_validated=desktop_continuity_live_validated,
    )


def codex_home_permissions(path: Path):
    path = Path(path)
    exists = path.exists()
    readable = os.access(path, os.R_OK) if exists else False
    writable = os.access(path, os.W_OK) if exists else False
    return {'Path': str(path), 'Exists': exists, 'Readable': readable, 'Writable': writable}


def hidden_subprocess_kwargs(*, new_process_group=False):
    """Keep CFR-owned background subprocesses invisible on Windows."""
    if os.name != 'nt':
        return {'start_new_session': True} if new_process_group else {}
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    if new_process_group:
        flags |= getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    startupinfo_factory = getattr(subprocess, 'STARTUPINFO', None)
    startupinfo = startupinfo_factory() if startupinfo_factory is not None else SimpleNamespace(dwFlags=0, wShowWindow=0)
    startupinfo.dwFlags |= getattr(subprocess, 'STARTF_USESHOWWINDOW', 0)
    startupinfo.wShowWindow = getattr(subprocess, 'SW_HIDE', 0)
    return {'creationflags': flags, 'startupinfo': startupinfo}
