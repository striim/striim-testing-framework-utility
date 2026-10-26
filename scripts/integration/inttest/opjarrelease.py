"""Release-specific jar filenames without changing Maven's Striim series or OP identity.

Kept byte-identical in both engine packages so either engine can be imported alone;
the root regression suite checks parity.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile
import time
from types import SimpleNamespace
import xml.etree.ElementTree as ET


_VERSION = r'\d+(?:\.\d+)+(?:[A-Za-z]\w*)?'
_POM_PREFIX = '.release-build-'
_POM_STALE_S = 3600   # a leftover temporary POM older than this, of a dead process, is removed


def _install_version(home: Path) -> str | None:
    """The release an install holds, from its one lib/Platform-<version>.jar."""
    try:
        found = [p.name[len('Platform-'):-len('.jar')] for p in (home / 'lib').glob('Platform-*.jar')]
    except OSError:
        return None
    found = [v for v in found if re.fullmatch(_VERSION, v)]
    return found[0] if len(found) == 1 else None


def configured_versions(root: Path, env=None) -> set[str]:
    """Read the existing build matrix and the selected STRIIM_HOME, best effort.

    Use the same matrix file selection and STRIIM_BUILD_<version> grammar as the
    console. An annotation without an install path does not configure a release.
    No configuration is written, imported from a consumer, or executed. Other
    installs beside STRIIM_HOME are not counted, so one matrix gives the same
    names on every host.
    """
    env = os.environ if env is None else env
    matrix = Path(env.get('FTC_PUBLISHER_RELEASES_ENV') or '.env').expanduser()
    if not matrix.is_absolute():
        matrix = root / matrix
    values = {}
    try:
        for line in matrix.read_text().splitlines():
            key, sep, value = line.strip().removeprefix('export ').partition('=')
            if sep and key.strip().startswith('STRIIM_BUILD_'):
                try:
                    values[key.strip()] = ' '.join(shlex.split(value, comments=True))
                except ValueError:
                    continue
    except (OSError, UnicodeError):
        pass
    values.update(env)
    versions = set()
    for key, value in values.items():
        if key.startswith('STRIIM_BUILD_') and value:
            version = key[len('STRIIM_BUILD_'):].replace('_', '.')
            if re.fullmatch(_VERSION, version):
                home = Path(value).expanduser()
                if not home.is_absolute():
                    home = matrix.parent / home
                # Matrix keys are labels (5.4.4 can name a 5.4.4.0 install).
                # Count the install's actual identity once, just as release resolution does.
                versions.add(_install_version(home) or version)
    if env.get('STRIIM_HOME'):
        selected = _install_version(Path(env['STRIIM_HOME']).expanduser())
        if selected:
            versions.add(selected)
    return versions


def jar_suffix(release: dict, versions) -> str:
    """The shortest unique dotted prefix, starting at the series (unknown -> series)."""
    series = release['STRIIM_SERIES']
    version = release.get('STRIIM_VERSION')
    if not version or not versions:
        return series
    peers = {v for v in versions if '.'.join(v.split('.')[:2]) == series}
    peers.add(version)
    parts = version.split('.')
    for size in range(2, len(parts) + 1):
        prefix = '.'.join(parts[:size])
        if all(v == version or '.'.join(v.split('.')[:size]) != prefix for v in peers):
            return prefix
    return version


def release_runner(run, module_dir: Path, release: dict, suffix: str):
    """Isolate Maven output, then atomically publish only this release's named jar.

    A temporary POM beside the original preserves basedir and relative sources/parents.
    Only build.directory changes; finalName, shade outputFile, manifest and filtering
    retain the real STRIIM_SERIES. Clean cannot remove another release's output.
    """
    series = release['STRIIM_SERIES']
    output = module_dir / 'target' / '.releases' / release['STRIIM_VERSION']

    def invoke(argv, cwd, env):
        _remove_abandoned_poms(module_dir)
        try:
            tree = ET.parse(module_dir / 'pom.xml')
        except ET.ParseError as e:
            return SimpleNamespace(returncode=1, stderr=f"cannot read {module_dir / 'pom.xml'}: {e}")
        project = tree.getroot()
        ns = project.tag.removesuffix('project')
        if ns:
            # Maven's model reader expects unprefixed tags in its default namespace.
            ET.register_namespace('', ns[1:-1])
        build = project.find(ns + 'build')
        if build is None:
            build = ET.SubElement(project, ns + 'build')
        directory = build.find(ns + 'directory')
        if directory is None:
            directory = ET.SubElement(build, ns + 'directory')
        directory.text = str(output)
        with tempfile.NamedTemporaryFile(prefix=f'{_POM_PREFIX}{os.getpid()}-', suffix='.xml',
                                         dir=module_dir, delete=False) as f:
            pom = Path(f.name)
        try:
            tree.write(pom, encoding='utf-8', xml_declaration=True)
            command = [*argv, '-f', str(pom)]
            for attempt in range(2):
                before = {p: p.stat() for p in output.glob(f'*-{series}.jar')}
                result = run(command, cwd, env)
                if getattr(result, 'returncode', 1) != 0:
                    return result
                jars = sorted(p for p in output.glob(f'*-{series}.jar')
                              if p.is_file() and not p.name.startswith('original-'))
                # A successful Maven command alone does not prove it produced a jar.
                if len(jars) == 1 and (jars[0] not in before or
                                      jars[0].stat() != before[jars[0]]):
                    break
                if attempt == 1:
                    return SimpleNamespace(
                        returncode=1, stderr=(
                            f"No newly built unambiguous jar artifact in {output} after clean retry "
                            f"(found: {[p.name for p in jars]})"))
                # Incremental packaging can leave V1 beside V2 after a module bump.
                # Discard only this release's cache; a retry cannot reuse a stale jar.
                if output.exists():
                    shutil.rmtree(output)
                command = [*argv, '-f', str(pom)]
                if 'clean' not in command:
                    command.insert(1, 'clean')
            jar = jars[0]
            name = jar.name[:-len(f'-{series}.jar')] + f'-{suffix}.jar'
            target = module_dir / 'target'
            with tempfile.NamedTemporaryFile(dir=target, delete=False) as f:
                staged = Path(f.name)
            try:
                shutil.copy2(jar, staged)
                staged.replace(target / name)
            finally:
                staged.unlink(missing_ok=True)
            # A module version bump may leave a second candidate for THIS release.
            for old in target.glob(f'*-{suffix}.jar'):
                if old.name != name and not old.name.startswith('original-'):
                    old.unlink()
            return result
        finally:
            pom.unlink(missing_ok=True)

    return invoke


def _remove_abandoned_poms(module_dir: Path) -> None:
    """Remove temporary POMs left by a killed build: its process is gone and the file is old.

    A live build in another process keeps its POM.
    """
    for pom in module_dir.glob(f'{_POM_PREFIX}*.xml'):
        pid = pom.name[len(_POM_PREFIX):].partition('-')[0]
        try:
            if time.time() - pom.stat().st_mtime < _POM_STALE_S:
                continue
            if pid.isdigit():
                os.kill(int(pid), 0)
                continue
        except ProcessLookupError:
            pass
        except OSError:
            # PermissionError: another user's live process; anything else: leave it.
            continue
        pom.unlink(missing_ok=True)


def is_private_cache(path: Path) -> bool:
    """Whether a jar belongs to a framework-owned target/.releases build cache."""
    return any(parent.name == '.releases' and parent.parent.name == 'target'
               for parent in path.parents)
