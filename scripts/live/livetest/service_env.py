"""Consumer service settings discovery and dotenv resolution, shared by both tiers."""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

class RegistryError(Exception):
    pass


_ENV_NAME = re.compile(r'[A-Z][A-Z0-9_]*\Z')


def required_env(raw, path):
    """Environment settings a service requires explicitly, without defaults."""
    if raw is None:
        return []
    if (not isinstance(raw, list)
            or any(not isinstance(key, str) or not _ENV_NAME.fullmatch(key) for key in raw)
            or len(set(raw)) != len(raw)):
        raise RegistryError(f"{path}: required_env must be a list of unique environment variable names")
    return list(raw)


def require_env(name, keys, env):
    """Reject missing or blank settings; never include their values in an error."""
    missing = [key for key in keys if not str(env.get(key) or "").strip()]
    if missing:
        raise RegistryError(f"service {name!r}: set required environment variable(s): {', '.join(missing)}")


def pre_up_env(raw, path):
    """Typed hook inputs: [{name: ENV_KEY, type: string|path}, ...]."""
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise RegistryError(f"{path}: pre_up_env must be a list of name/type mappings")
    out = {}
    for entry in raw:
        if (not isinstance(entry, dict) or set(entry) != {'name', 'type'}
                or not isinstance(entry['name'], str) or not _ENV_NAME.fullmatch(entry['name'])
                or entry['type'] not in ('string', 'path') or entry['name'] in out):
            raise RegistryError(f"{path}: invalid or duplicate pre_up_env entry {entry!r}")
        out[entry['name']] = entry['type']
    return out


def compose_keys(text):
    """The doctor's Compose interpolation parser (includes default/alternative forms)."""
    return set(re.findall(r'\$\{([A-Z][A-Z0-9_]*)', text))


def declarations(env=None):
    """Declared keys across visible profiles, first definition wins within each tier.

    Read path settings without service discovery to avoid recursive dotenv lookups.
    Manifest validation/expansion uses this explicit environment, before CLI dispatch.
    """
    from livetest import paths, layout, project
    from inttest import paths as int_paths
    env = dict(os.environ if env is None else env)
    clone = paths.dotenv_path({k: v for k, v in env.items() if k != 'SLT_PROJECT_ROOT'})
    checkout = paths.dotenv_path(env)
    basic = {**paths.read_dotenv(clone), **paths.read_dotenv(checkout), **env}
    entries = list(layout._cfg_services)
    if basic.get('GOLD_TARGETS'):
        try:
            loaded = project.load_project(basic['GOLD_TARGETS'], env=basic)
        except project.ProjectError as e:
            # A configuration error that names the key, like any path key set to a missing path.
            raise paths.PathConfigError(f"GOLD_TARGETS={basic['GOLD_TARGETS']!r}: {e}") from None
        entries += list(loaded.services_roots)
    else:
        entries += list(layout._cfg_manifest_services)
    live_roots = [layout._services_dir_for(Path(p)) for p in entries]
    roots_by_tier = [live_roots + [paths.services_dir(env, dotenv=basic)],
                     [p / 'integration' for p in live_roots] + [int_paths.services_dir(env, dotenv=basic)]]
    types = {}
    for roots in roots_by_tier:
        seen = set()
        for root in roots:
            for file in sorted(root.glob('*/service.yaml')):
                if file.parent.name in seen:
                    continue
                seen.add(file.parent.name)
                raw = yaml.safe_load(file.read_text()) or {}
                keys = {raw.get('live_override_env'), raw.get('opt_in_env')}
                keys.update(required_env(raw.get('required_env'), file))
                for key in ('docker_env', 'live_env'):
                    keys.update((raw.get(key) or {}).values())
                compose = raw.get('compose')
                if compose and (file.parent / compose).is_file():
                    keys.update(compose_keys((file.parent / compose).read_text()))
                for key in keys:
                    if key:
                        types.setdefault(key, 'string')
                types.update(pre_up_env(raw.get('pre_up_env'), file))
    return types


def values(paths, env=None):
    """Machine < clone .env < project .env; explicit environment is applied by paths.

    Only stock or declared keys are imported. Hook paths from a checkout are relative
    to that checkout; machine hook paths must be absolute. Shell values stay untouched.
    """
    env = os.environ if env is None else env
    types = declarations(env)
    allowed = set(paths.KEYS) | set(paths.SERVICE_KEYS) | set(paths.LICENCE_KEYS) | set(types)
    out = paths.machine_values(env, allowed=allowed)
    for key, value in out.items():
        if types.get(key) == 'path' and not Path(value).expanduser().is_absolute():
            raise paths.PathConfigError(f'{key} in machine.env must be an absolute path')
    clone_env = {k: v for k, v in env.items() if k != 'SLT_PROJECT_ROOT'}
    for file in (paths.dotenv_path(clone_env), paths.dotenv_path(env)):
        checkout = paths.read_dotenv(file, allowed=allowed - set(paths.LICENCE_KEYS))
        for group in paths._ALIASES.values():
            if any((checkout.get(k) or '').strip() for k in group):
                for key in group:
                    out.pop(key, None)
        for key, value in checkout.items():
            if not value.strip():
                continue
            if types.get(key) == 'path':
                path = Path(value).expanduser()
                value = str(path if path.is_absolute() else file.parent / path)
            out[key] = value
    return out
