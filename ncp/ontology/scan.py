"""Reference repository exporter: scan a (mono)repo and write a seed manifest.

``ncp ontology scan`` reads files only; it never touches the NCP store. Its
output is a manifest for ``ncp ontology seed`` that a person can review
first. Every relation cites the file and line it came from
(``attestation = "path#Lnn"``).

What it reads, and how much each fact is trusted:

==============================  ==========================================  ==========
Source                          Becomes                                     Status
==============================  ==========================================  ==========
build files: ``package.json``,  ``Module`` per directory (key = path,       verified
``pyproject.toml``, ``go.mod``, alias = package name); ``Repo CONTAINS
``Cargo.toml``, ``pom.xml``,    Module``; in-repo dependencies as
``build.gradle(.kts)``          ``Module DEPENDS_ON Module``
``Dockerfile``, ``Chart.yaml``, ``Service`` (Chart / Deployment name, else  verified
k8s ``Deployment`` /            the directory name);
``StatefulSet`` manifests       ``Module BUILDS Service``
``CODEOWNERS``                  ``Team OWNS Module`` / ``Team OWNS Service``  verified
Messaging client code (Kafka,   ``PRODUCES`` / ``CONSUMES`` ``KafkaTopic`` /   candidate
SQS / SNS, RabbitMQ / AMQP,     ``Queue`` / ``Exchange``, ``Exchange ROUTES_TO (heuristic)
JMS) in JS/TS, Python, Go and   Queue``; destinations are literals, constants,
JVM sources, Spring Cloud       Spring ``${...}`` placeholders, simple SpEL and
Stream bindings                 environment defaults; the rest become
                                ``unresolved:*`` destinations. Edges start at
                                the deployable ``Service`` (see below)
``ncp-ontology.{toml,json,      hand-written facts, merged in; they win     as written
yaml,yml}`` in any module       over scanned entities with the same key
==============================  ==========================================  ==========

A module is also a service when it is deployable without a Dockerfile (Spring Boot
plugin, ``main``, ``scripts.start``, ``package main``, ``[project.scripts]``...); a
messaging site in a library module is attributed to every service that depends on it.

``schema_version=3`` (default) needs ``infrastructure`` v3 (``Queue``, ``Exchange``,
``ROUTES_TO``); ``2`` needs v2 (``Repo``, ``Module``) and skips Queue / Exchange sites
(counted as ``skipped_schema``); ``1`` drops modules and repos, lifts module
dependencies to ``Service DEPENDS_ON Service`` where both modules build a
service, and keeps only service-level ownership and Kafka edges.

The scan is static and approximate: it resolves literals, Spring properties
and string constants but follows no code paths, and CODEOWNERS matching is a gitignore-style approximation.
Review the manifest (or seed with ``--status candidate``) before trusting it.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
import xml.etree.ElementTree as ET
from bisect import bisect_right
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from ncp.ontology import scan_messaging as _msg
from ncp.ontology.facade import OntologyError
from ncp.ontology.models import Entity
from ncp.ontology.scan_messaging import MAX_LINE as _MAX_LINE
from ncp.ontology.scan_messaging import collapse as _collapse
from ncp.ontology.scan_messaging import matching as _matching
from ncp.ontology.scan_messaging import split_top as _split_top
from ncp.ontology.scan_messaging import unescape as _unescape
from ncp.ontology.seed import SeedManifest, load_manifest, merge_manifests, parse_manifest

HeuristicMode = Literal["candidate", "verified", "off"]

SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "vendor", "build", "dist", "target", "out",
    ".venv", "venv", "env", "__pycache__", ".tox", ".nox", ".gradle", ".idea", ".vscode",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next", ".terraform", "bower_components",
})
TEST_PARTS = frozenset({"test", "tests", "__tests__", "spec", "specs", "testdata", "fixtures",
                        "e2e", "it", "integration-test", "integration-tests"})
MAX_FILE_BYTES = 1_000_000
HAND_MANIFEST_NAMES = frozenset({
    "ncp-ontology.toml", "ncp-ontology.json", "ncp-ontology.yaml", "ncp-ontology.yml",
})
CODEOWNERS_PATHS = ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS", ".gitlab/CODEOWNERS")
SOURCE_SUFFIXES = frozenset({".java", ".kt", ".scala", ".py", ".js", ".mjs", ".cjs", ".ts",
                             ".tsx", ".jsx", ".mts", ".cts", ".go", ".properties", ".yml",
                             ".yaml"})
HEURISTIC_CONFIDENCE = 0.6
UNRESOLVED_CONFIDENCE = 0.4
STRUCTURAL_CONFIDENCE = 0.95


@dataclass
class _Module:
    path: str  # repo-relative directory, "." for the root
    names: dict[str, str] = field(default_factory=dict)  # ecosystem -> package identity
    build_files: list[str] = field(default_factory=list)
    deps: list[tuple[str, str, str, int]] = field(default_factory=list)  # (eco, name, file, line)
    service: str | None = None
    service_evidence: tuple[str, int] | None = None
    # 6 Chart.yaml, 4 named k8s workload, 2 Dockerfile / unnamed workload, 1 deployable
    service_rank: int = 0
    service_detected_by: str | None = None
    markers: list[tuple[str, str, int]] = field(default_factory=list)  # (rule, file, line)
    anonymous: bool = False  # a build file that names no package (package.json without "name")
    aggregator: bool = False  # ... and only lists workspaces: not a module
    fallback: str = ""  # display name when no package identity exists
    evidence: str | None = None  # file cited for a module without a build file

    @property
    def key(self) -> str:
        return self.path if self.path != "." else "(root)"

    @property
    def display(self) -> str:
        for ecosystem in ("npm", "python", "cargo", "maven", "go", "gradle"):
            if ecosystem in self.names:
                return self.names[ecosystem]
        return self.fallback or PurePosixPath(self.path).name or self.key


@dataclass
class ScanResult:
    manifest: SeedManifest
    summary: dict[str, object]

    def to_json(self) -> str:
        return json.dumps(self.manifest.model_dump(mode="json", exclude_none=True,
                                                   exclude_defaults=False),
                          indent=2, sort_keys=False) + "\n"


# -- file discovery ----------------------------------------------------------------


def _list_files(root: Path) -> list[str]:
    """Repo-relative POSIX paths: ``git ls-files`` (tracked + untracked, not
    ignored) when ``root`` is a git work tree, else a filtered walk."""
    try:
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
             "--exclude-standard"],
            capture_output=True, check=True, timeout=60,
        ).stdout.decode("utf-8", "replace")
        files = [item for item in listed.split("\0") if item]
        if files:
            return sorted(path for path in files
                          if not SKIP_DIRS.intersection(PurePosixPath(path).parts[:-1]))
    except (OSError, subprocess.SubprocessError):
        pass
    found: list[str] = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if SKIP_DIRS.intersection(rel.parts[:-1]) or not path.is_file():
            continue
        found.append(rel.as_posix())
    return sorted(found)


def _read(root: Path, rel: str) -> str | None:
    path = root / rel
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, max(offset, 0)) + 1


def _line_of(text: str, needle: str) -> int:
    position = text.find(needle)
    return _line(text, position) if position >= 0 else 1


def _dir(rel: str) -> str:
    parent = PurePosixPath(rel).parent.as_posix()
    return parent if parent else "."


_TEST_FILE = re.compile(
    r"(_test\.go|\.(spec|test)\.[cm]?[jt]sx?|Tests?\.(java|kt|scala)|_test\.py)$|^test_.*\.py$",
)


def _is_test_path(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return bool(TEST_PARTS.intersection(part.lower() for part in parts[:-1])) or bool(
        _TEST_FILE.search(parts[-1]),
    )


# -- build files -----------------------------------------------------------------------


def _pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_package_json(text: str, rel: str, module: _Module) -> None:
    data = json.loads(text)
    if not isinstance(data, dict):
        return
    name = data.get("name")
    if isinstance(name, str) and name.strip():
        module.names["npm"] = name.strip()
    else:
        module.anonymous = True
    scripts = data.get("scripts")
    if isinstance(scripts, dict) and isinstance(scripts.get("start"), str):
        module.markers.append(("package_json_start", rel, _line_of(text, '"start"')))
    elif data.get("bin"):
        module.markers.append(("package_json_bin", rel, _line_of(text, '"bin"')))
    if module.anonymous and data.get("workspaces") and not module.markers:
        module.aggregator = True  # a nameless workspace root only aggregates its packages
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        deps = data.get(section)
        if isinstance(deps, dict):
            for dep in deps:
                module.deps.append(("npm", dep, rel, _line_of(text, f'"{dep}"')))


def _parse_pyproject(text: str, rel: str, module: _Module) -> None:
    data = tomllib.loads(text)
    project = data.get("project") or {}
    poetry = (data.get("tool") or {}).get("poetry") or {}
    name = project.get("name") or poetry.get("name")
    if isinstance(name, str) and name.strip():
        module.names["python"] = _pep503(name)
    requirements: list[str] = list(project.get("dependencies") or [])
    for extra in (project.get("optional-dependencies") or {}).values():
        requirements.extend(extra or [])
    for group in (data.get("dependency-groups") or {}).values():
        requirements.extend(item for item in group or [] if isinstance(item, str))
    names = []
    for requirement in requirements:
        if isinstance(requirement, str):
            match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
            if match:
                names.append(match.group(1))
    names.extend(dep for dep in (poetry.get("dependencies") or {}) if dep.lower() != "python")
    for group in (poetry.get("group") or {}).values():
        names.extend((group or {}).get("dependencies") or {})
    for dep in names:
        module.deps.append(("python", _pep503(dep), rel, _line_of(text, dep)))
    if project.get("scripts") or poetry.get("scripts"):
        module.markers.append(("python_scripts", rel, _line_of(text, "scripts")))


def _parse_go_mod(text: str, rel: str, module: _Module) -> None:
    match = re.search(r"^\s*module\s+(\S+)", text, re.MULTILINE)
    if match:
        module.names["go"] = match.group(1)
    for found in re.finditer(r"^\s*(?:require\s+)?([^\s()]+)\s+v\d\S*", text, re.MULTILINE):
        module.deps.append(("go", found.group(1), rel, _line(text, found.start(1))))


def _parse_cargo(text: str, rel: str, module: _Module) -> None:
    data = tomllib.loads(text)
    package = data.get("package") or {}
    if isinstance(package.get("name"), str):
        module.names["cargo"] = package["name"]
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        for dep, spec in (data.get(section) or {}).items():
            real = spec.get("package", dep) if isinstance(spec, dict) else dep
            module.deps.append(("cargo", real, rel, _line_of(text, dep)))


def _xml_children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if child.tag.rsplit("}", 1)[-1] == name]


def _xml_text(element: ET.Element | None, name: str) -> str | None:
    if element is None:
        return None
    for child in _xml_children(element, name):
        return (child.text or "").strip() or None
    return None


def _parse_pom(text: str, rel: str, module: _Module) -> None:
    root = ET.fromstring(text)
    if _xml_children(root, "modules") and (_xml_text(root, "packaging") == "pom"):
        return  # an aggregator, not a module
    artifact = _xml_text(root, "artifactId")
    parents = _xml_children(root, "parent")
    group = _xml_text(root, "groupId") or _xml_text(parents[0] if parents else None, "groupId")
    if artifact:
        module.names["maven"] = f"{group}:{artifact}" if group else artifact
    plugin = text.rfind("spring-boot-maven-plugin")
    if plugin > text.rfind("</pluginManagement>") and _xml_text(root, "packaging") != "pom":
        module.markers.append(("maven_spring_boot_plugin", rel, _line(text, plugin)))
    for deps in _xml_children(root, "dependencies"):
        for dep in _xml_children(deps, "dependency"):
            dep_artifact = _xml_text(dep, "artifactId")
            if not dep_artifact:
                continue
            dep_group = _xml_text(dep, "groupId")
            identity = f"{dep_group}:{dep_artifact}" if dep_group else dep_artifact
            line = _line_of(text, f"<artifactId>{dep_artifact}</artifactId>")
            module.deps.append(("maven", identity, rel, line))


_GRADLE_PROJECT = re.compile(r"""project\(\s*(?:path\s*[:=]\s*)?["'](:[^"']*)["']""")


_BOOT_PLUGIN = re.compile(r"""["']org\.springframework\.boot["']""")
_APPLICATION_PLUGIN = re.compile(
    r"""(?:\b(?:id|plugin)\s*[:(]?\s*["']application["']|^\s*application\s*$)""")
_MAIN_CLASS = re.compile(r"\b(?:mainClass(?:Name)?|bootJar)\b")


def _gradle_markers(text: str, rel: str, module: _Module) -> None:
    """Deployable signals in a Gradle build file (not for ``bootJar { enabled = false }``)."""
    compact = text.replace(" ", "")
    boot_disabled = "bootJar" in text and "enabled=false" in compact
    for number, line in enumerate(text.split("\n"), 1):
        if len(line) > _MAX_LINE:
            continue
        if _BOOT_PLUGIN.search(line) and "apply false" not in line and not boot_disabled:
            module.markers.append(("spring_boot_plugin", rel, number))
        elif _APPLICATION_PLUGIN.search(line):
            module.markers.append(("gradle_application_plugin", rel, number))
        elif _MAIN_CLASS.search(line) and not boot_disabled:
            module.markers.append(("gradle_main_class", rel, number))


def _parse_gradle(text: str, rel: str, module: _Module, gradle_path: str) -> None:
    module.names["gradle"] = gradle_path
    _gradle_markers(text, rel, module)
    for found in _GRADLE_PROJECT.finditer(text):
        module.deps.append(("gradle", found.group(1), rel, _line(text, found.start(1))))


def _gradle_roots(files: Iterable[str]) -> list[str]:
    return sorted({_dir(path) for path in files
                   if PurePosixPath(path).name in ("settings.gradle", "settings.gradle.kts")},
                  key=lambda item: -len(item))


def _gradle_path(directory: str, roots: list[str]) -> str:
    for root in roots:
        if root == "." or directory == root or directory.startswith(root + "/"):
            rel = directory if root == "." else directory[len(root):].lstrip("/")
            return ":" + rel.replace("/", ":") if rel not in ("", ".") else ":"
    return ":" + directory.replace("/", ":")


# -- services, owners, kafka --------------------------------------------------------------


def _owning_module(rel: str, modules: dict[str, _Module]) -> _Module | None:
    directory = _dir(rel)
    while True:
        if directory in modules:
            return modules[directory]
        if directory == ".":
            return None
        directory = _dir(directory)


_K8S_KIND = re.compile(r"^kind:\s*(Deployment|StatefulSet|DaemonSet)\s*$", re.MULTILINE)
_K8S_META = re.compile(r"^metadata:[ \t]*(?:#.*)?$")
_K8S_NAME_VALUE = re.compile(r"name[ \t]*:[ \t]*[\"']?([A-Za-z0-9.\-_]+)")
_K8S_NAME_KEY = re.compile(r"name[ \t]*:")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _k8s_metadata_name(text: str, start: int = 0) -> str | None:
    """Return ``metadata.name`` (line-based, linear time); None if absent or templated."""
    lines = text.split("\n")
    first = text.count("\n", 0, start)
    for begin in (first, 0):
        for index in range(begin, len(lines)):
            if not _K8S_META.match(lines[index].rstrip("\r")):
                continue
            block_indent = 0
            for line in lines[index + 1:]:
                line = line.rstrip("\r")
                if not line.strip():
                    continue
                indent = _indent(line)
                if indent == 0:
                    break
                if block_indent == 0:
                    block_indent = indent
                if indent == block_indent:
                    stripped = line.strip()
                    if _K8S_NAME_KEY.match(stripped) and "{{" in stripped:
                        return None
                    match = _K8S_NAME_VALUE.match(stripped)
                    if match:
                        return match.group(1)
            return None
        if begin == 0:
            break
    return None


def _service_name(rel: str, text: str) -> tuple[str | None, int] | None:
    name = PurePosixPath(rel).name
    if name == "Chart.yaml":
        match = re.search(r"^name:\s*[\"']?([A-Za-z0-9.\-_]+)", text, re.MULTILINE)
        return (match.group(1), _line(text, match.start())) if match else (None, 1)
    if name == "Dockerfile" or name.startswith("Dockerfile.") or name.endswith(".Dockerfile"):
        return (None, 1)
    if name.endswith((".yaml", ".yml")):
        kind = _K8S_KIND.search(text)
        if kind is None:
            return None
        return _k8s_metadata_name(text, kind.end()), _line(text, kind.start())
    return None


def _codeowners_regex(pattern: str) -> re.Pattern[str]:
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    body = pattern.strip("/")
    out = []
    index = 0
    while index < len(body):
        char = body[index]
        if body.startswith("**", index):
            out.append(".*")
            index += 2
            continue
        out.append({"*": "[^/]*", "?": "[^/]"}.get(char, re.escape(char)))
        index += 1
    prefix = "^" if anchored else "^(?:.*/)?"
    return re.compile(prefix + "".join(out) + "(?:/.*)?$")


def _parse_codeowners(text: str) -> list[tuple[re.Pattern[str], list[str], int]]:
    rules = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("["):  # gitlab sections
            continue
        parts = line.split()
        owners = [owner for owner in parts[1:] if owner.startswith("@") or "@" in owner]
        rules.append((_codeowners_regex(parts[0]), owners, number))
    return rules


def _owners_for(path: str, rules: list[tuple[re.Pattern[str], list[str], int]]) -> tuple[list[str], int]:
    owners: list[str] = []
    line = 0
    for regex, rule_owners, number in rules:
        if regex.match(path):
            owners, line = rule_owners, number
    return owners, line


_YAML_BINDING_KEY = re.compile(
    r"([ \t]*)([A-Za-z0-9_.-]+-(in|out)-\d+)[ \t]*:[ \t]*(?:#.*)?")
_YAML_DESTINATION = re.compile(r"destination[ \t]*:[ \t]*[\"']?([^\s\"'#]+)")
_PROP_BINDING = re.compile(
    r"spring\.cloud\.stream\.bindings\.([A-Za-z0-9_.-]{1,200}-(?:in|out)-\d{1,6})\.destination")
_TOPIC_NAME = re.compile(r"^[A-Za-z0-9._-]{1,249}$")
# Queue / exchange names: AMQP allows more punctuation than Kafka topics.
_DEST_NAME = re.compile(r"^[A-Za-z0-9._:@#-]{1,249}$")


def _binding_entries(rel: str, text: str) -> Iterator[tuple[str, str, str, int]]:
    """``(direction, binding name, raw destination, line)`` of Spring Cloud Stream
    ``<name>-in-N`` / ``<name>-out-N`` bindings in YAML / properties (destinations are
    returned unresolved: they may hold ``${...}`` placeholders or comma lists)."""
    suffix = PurePosixPath(rel).suffix
    if suffix in (".yml", ".yaml"):
        lines = text.split("\n")
        for number, raw in enumerate(lines):
            key = _YAML_BINDING_KEY.fullmatch(raw.rstrip("\r"))
            if key is None:
                continue
            key_indent = len(key.group(1))
            for offset in range(number + 1, len(lines)):
                line = lines[offset].rstrip("\r")
                if not line.strip():
                    continue
                if _indent(line) <= key_indent:
                    break
                dest = _YAML_DESTINATION.match(line.lstrip(" \t"))
                if dest:
                    direction = "CONSUMES" if key.group(3) == "in" else "PRODUCES"
                    yield direction, key.group(2), dest.group(1), offset + 1
                    break
    elif suffix == ".properties":
        for key_text, (value, number) in _parse_properties(text).items():
            found = _PROP_BINDING.fullmatch(key_text)
            if found and value:
                direction = "CONSUMES" if "-in-" in found.group(1) else "PRODUCES"
                yield direction, found.group(1), value, number


def _kafka_usages(rel: str, text: str) -> Iterator[tuple[str, str, int]]:
    """``(direction, destination, line)`` of Spring Cloud Stream bindings (see
    :func:`_binding_entries`)."""
    for direction, _binding, raw, line in _binding_entries(rel, text):
        yield direction, raw, line


# -- Spring topic resolution: properties, constants, @Value fields ---------------------
#
# Everything below is line- or index-based and linear in the input; the few regexes
# have no nested quantifiers and no overlapping alternations (see each comment).

_JVM_SUFFIXES = (".java", ".kt", ".scala")
# application[-profile].{properties,yml,yaml} / bootstrap[-profile].*; `[^.]+` cannot cross
# the following literal dot, so the match is linear in the (short) file name.
_CONFIG_FILE = re.compile(r"(application|bootstrap)(?:-([^.]+))?\.(properties|ya?ml)")
# `String NAME = "x"`: every step is deterministic (\s+ then \w+ then \s*=\s*").
_JAVA_CONST = re.compile(r"\bString\s+([A-Za-z_]\w*)\s*=\s*\"([^\"\n]*)\"")
# `val NAME[: String] = "x"`: the optional group starts with ':' while the \s* before it
# cannot, so there is no ambiguity.
_KT_CONST = re.compile(r"\bval\s+([A-Za-z_]\w*)\s*(?::\s*String\s*)?=\s*\"([^\"\n]*)\"")
_TYPE_DECL = re.compile(r"\b(class|interface|object|enum)\s+([A-Za-z_]\w*)")
_UPPER_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_IDENT_CHAIN = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")  # a dot is mandatory per repeat
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
_MAX_NESTING = 4  # ${a} -> ${b:${c}} -> ... levels followed
_RECEIVER_EXCLUDE = ("rabbit", "jms", "amqp", "rest", "mail", "redis", "jdbc", "sqs", "sns")


@dataclass(frozen=True)
class _Prop:
    profile: str | None
    value: str
    rel: str
    line: int


@dataclass
class _PropIndex:
    """``key -> [_Prop]`` per owning module and repo-wide (exact and normalised keys)."""

    by_module: dict[str, dict[str, list[_Prop]]] = field(default_factory=dict)
    repo: dict[str, list[_Prop]] = field(default_factory=dict)

    @staticmethod
    def _norm(key: str) -> str:
        return "\0" + key.lower().replace("-", "").replace("_", "")

    def add(self, module: str, profile: str | None, rel: str,
            items: dict[str, tuple[str, int]]) -> None:
        local = self.by_module.setdefault(module, {})
        for key, (value, line) in items.items():
            prop = _Prop(profile, value, rel, line)
            for name in (key, self._norm(key)):
                local.setdefault(name, []).append(prop)
                self.repo.setdefault(name, []).append(prop)

    @staticmethod
    def _pick(entries: list[_Prop], *, strict: bool) -> _Prop | None:
        """Base ``application.*`` wins; else the alphabetically first profile.
        ``strict`` (repo-wide fallback) refuses when the candidates disagree."""
        pool = [item for item in entries if item.profile is None]
        if not pool:
            first = min(item.profile for item in entries)  # type: ignore[type-var]
            pool = [item for item in entries if item.profile == first]
        pool.sort(key=lambda item: (item.rel, item.line))
        if strict and len({item.value for item in pool}) > 1:
            return None
        return pool[0]

    def lookup(self, key: str, module: str) -> _Prop | None:
        for name in (key, self._norm(key)):
            local = self.by_module.get(module, {}).get(name)
            if local:
                return self._pick(local, strict=False)
            wide = self.repo.get(name)
            if wide:
                return self._pick(wide, strict=True)
        return None


@dataclass(frozen=True)
class _Const:
    name: str
    value: str
    rel: str
    module: str
    env: str | None = None  # the value is the default of this environment variable


@dataclass
class _ConstIndex:
    """String constants keyed by ``NAME`` and ``Class.NAME``."""

    entries: dict[str, list[_Const]] = field(default_factory=dict)

    def add(self, owner: str | None, name: str, value: str, rel: str, module: str,
            env: str | None = None) -> None:
        const = _Const(name, value, rel, module, env)
        self.entries.setdefault(name, []).append(const)
        if owner:
            self.entries.setdefault(f"{owner}.{name}", []).append(const)

    def find(self, expr: str, rel: str, module: str) -> _Const | None:
        parts = [part for part in expr.split(".") if part not in ("this", "Companion")]
        if not parts:
            return None
        keys = [".".join(parts)]
        if len(parts) > 2:
            keys.append(".".join(parts[-2:]))
        if len(parts) > 1 and _UPPER_NAME.fullmatch(parts[-1]):
            keys.append(parts[-1])
        for key in keys:
            entries = self.entries.get(key)
            if not entries:
                continue
            for scope in (lambda c: c.rel == rel, lambda c: c.module == module, lambda c: True):
                pool = [const for const in entries if scope(const)]
                if pool:
                    return pool[0] if len({c.value for c in pool}) == 1 else None
        return None


def _parse_properties(text: str) -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    for number, raw in enumerate(text.split("\n"), 1):
        line = raw.strip()
        if not line or line[0] in "#!" or len(line) > _MAX_LINE:
            continue
        cuts = [position for position in (line.find("="), line.find(":")) if position > 0]
        if not cuts:
            continue
        cut = min(cuts)
        key = line[:cut].strip()
        if key:
            out.setdefault(key, (line[cut + 1:].strip(), number))
    return out


def _yaml_key_value(content: str) -> tuple[str, str] | None:
    position = 0
    while True:
        colon = content.find(":", position)
        if colon < 0:
            return None
        if colon + 1 == len(content) or content[colon + 1] in " \t":
            return content[:colon].strip().strip("\"'"), content[colon + 1:].strip()
        position = colon + 1


def _yaml_scalar(value: str) -> str | None:
    """The scalar text, or None for empty / block / flow values (not flattened)."""
    if not value or value[0] in "|>{[&*!":
        return None
    if value[0] in "\"'":
        end = value.find(value[0], 1)
        return value[1:end] if end > 0 else value[1:]
    cut = value.find(" #")
    return (value[:cut] if cut >= 0 else value).strip()


def _parse_yaml_props(text: str) -> dict[str, tuple[str, int]]:
    """Flatten scalar ``key: value`` pairs to dotted keys (indentation based, one pass).
    Lists, flow collections and block scalars are skipped; ``---`` is ignored."""
    out: dict[str, tuple[str, int]] = {}
    stack: list[tuple[int, str]] = []
    skip_deeper: int | None = None
    for number, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip()
        content = line.lstrip(" ")
        if not content or content[0] == "#" or len(line) > _MAX_LINE:
            continue
        indent = len(line) - len(content)
        if skip_deeper is not None:
            if indent > skip_deeper:
                continue
            skip_deeper = None
        if content.startswith("---"):
            continue
        if content[0] == "-":
            skip_deeper = indent
            continue
        pair = _yaml_key_value(content)
        if pair is None or not pair[0]:
            continue
        key, raw_value = pair
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if not raw_value:
            stack.append((indent, key))
            continue
        if raw_value[0] in "|>":
            skip_deeper = indent
            continue
        scalar = _yaml_scalar(raw_value)
        if scalar is not None:
            out.setdefault(".".join([name for _, name in stack] + [key]), (scalar, number))
    return out


def _extract_constants(text: str, suffix: str) -> list[tuple[str | None, str, str]]:
    """``(owner class, NAME, value)`` for ``static final String`` (Java) and
    ``const val`` / ``val`` (Kotlin, Scala) string constants."""
    kotlin = suffix != ".java"
    owner: str | None = None
    kind = "class"
    found: list[tuple[str | None, str, str]] = []
    for raw in text.split("\n"):
        if len(raw) > _MAX_LINE:
            continue
        stripped = raw.strip()
        if stripped.startswith(("//", "/*", "*")):
            continue
        if "class " in raw or "interface " in raw or "object " in raw or "enum " in raw:
            for decl in _TYPE_DECL.finditer(raw):
                if decl.group(1) == "object" and raw[:decl.start()].rstrip().endswith("companion"):
                    kind = "object"  # the owner stays the enclosing class
                    continue
                owner, kind = decl.group(2), decl.group(1)
        if not kotlin:
            if "static" not in raw or "String" not in raw:
                continue
            match = _JAVA_CONST.search(raw)
            if match and "static" in raw[:match.start()]:
                found.append((owner, match.group(1), _unescape(match.group(2))))
        elif "val " in raw:
            match = _KT_CONST.search(raw)
            if match is None:
                continue
            name = match.group(1)
            if ("const" in raw[:match.start()].split() or _UPPER_NAME.fullmatch(name)
                    or kind == "object"):
                found.append((owner, name, _unescape(match.group(2))))
    return found


def _literal(expr: str) -> str | None:
    """The content of a plain ``"..."`` literal, else None."""
    if len(expr) < 2 or expr[0] != '"' or expr[-1] != '"':
        return None
    inner = expr[1:-1]
    if '"' in inner.replace("\\\\", "").replace('\\"', ""):
        return None
    return _unescape(inner)


def _first_placeholder_key(text: str) -> str | None:
    start = text.find("${")
    if start < 0:
        return None
    end = _brace_end(text, start + 1)
    inner = text[start + 2:end if end > 0 else len(text)]
    key, _default = _split_default(inner)
    return _collapse(key) or None


def _brace_end(text: str, open_index: int) -> int:
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _split_default(inner: str) -> tuple[str, str | None]:
    """Split ``key:default`` at the first ':' outside nested ``${...}``."""
    depth = 0
    for index, char in enumerate(inner):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == ":" and depth == 0:
            return inner[:index], inner[index + 1:]
    return inner, None


@dataclass
class _Term:
    value: str | None
    source: str | None = None
    profile: str | None = None
    unresolved: str | None = None  # entity key of the placeholder when value is None
    extra: dict[str, object] = field(default_factory=dict)  # spel / bean / env attributes


@dataclass
class _Outcome:
    names: list[str]
    source: str
    profile: str | None
    unresolved: str | None
    expression: str
    extra: dict[str, object] = field(default_factory=dict)


@dataclass
class _Fail:
    """A SpEL expression the evaluator does not understand (``bean`` names a looked-up bean)."""

    bean: str | None = None


# SpEL forms (all patterns are anchored, bounded and free of nested quantifiers).
_SPEL_SPLIT = re.compile(r"\.\s*split\s*\(\s*(['\"])([^'\"]{1,10})\1\s*\)")
_SPEL_TYPE = re.compile(r"T\s*\(\s*([A-Za-z_][\w.$]{0,200})\s*\)\s*\.\s*([A-Za-z_]\w*)")
_SPEL_INDEX = re.compile(
    r"(?:environment|systemProperties|systemEnvironment)\s*\[\s*(['\"])([^'\"]{1,200})\1\s*\]")
_SPEL_GETPROP = re.compile(
    r"environment\s*\.\s*getProperty\s*\(\s*(['\"])([^'\"]{1,200})\1\s*"
    r"(?:,\s*(['\"])([^'\"]{0,200})\3\s*)?\)")
_SPEL_MEMBER = re.compile(r"(@?)([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*(\(\s*\))?")


def _decapitalize(name: str) -> str:
    return name[:1].lower() + name[1:]


class _Resolver:
    def __init__(self, props: _PropIndex, consts: _ConstIndex) -> None:
        self.props = props
        self.consts = consts
        # bean name (decapitalised class name) -> (class name, field/const -> text)
        self.beans: dict[str, tuple[str, dict[str, str]]] = {}
        # module path -> (file, expression, @Value fields) of ``setDefaultTopic(...)``
        self.default_topics: dict[str, tuple[str, str, dict[str, str]]] = {}

    def expand(self, text: str, module: str, used: list[tuple[str, str, str | None]],
               depth: int = 0, trail: tuple[str, ...] = ()) -> str | None:
        """Expand ``${key}`` / ``${key:default}`` (nested, cycle-guarded); None if any
        placeholder cannot be resolved."""
        if "${" not in text:
            return text
        if depth > _MAX_NESTING:
            return None
        out: list[str] = []
        position = 0
        while True:
            start = text.find("${", position)
            if start < 0:
                out.append(text[position:])
                return "".join(out)
            out.append(text[position:start])
            end = _brace_end(text, start + 1)
            if end < 0:
                return None
            value = self._placeholder(text[start + 2:end], module, used, depth, trail)
            if value is None:
                return None
            out.append(value)
            position = end + 1

    def expand_partial(self, text: str, module: str,
                       used: list[tuple[str, str, str | None]]) -> str:
        """Like :meth:`expand`, but a placeholder that cannot be resolved stays as written."""
        out: list[str] = []
        position = 0
        while True:
            start = text.find("${", position)
            if start < 0:
                out.append(text[position:])
                return "".join(out)
            out.append(text[position:start])
            end = _brace_end(text, start + 1)
            if end < 0:
                out.append(text[start:])
                return "".join(out)
            value = self._placeholder(text[start + 2:end], module, used, 0, ())
            out.append(text[start:end + 1] if value is None else value)
            position = end + 1

    def _placeholder(self, inner: str, module: str, used: list[tuple[str, str, str | None]],
                     depth: int, trail: tuple[str, ...]) -> str | None:
        key, default = _split_default(inner)
        if "${" in key:
            expanded = self.expand(key, module, used, depth + 1, trail)
            if expanded is None:
                return None
            key = expanded
        key = key.strip()
        if key in trail:
            return None
        prop = self.props.lookup(key, module)
        if prop is not None:
            used.append(("property", key, prop.profile))
            return self.expand(prop.value, module, used, depth + 1, (*trail, key))
        if default is not None:
            used.append(("default", key, None))
            return self.expand(default, module, used, depth + 1, trail)
        return None

    def _text(self, text: str, source: str | None, module: str, rel: str = "",
              fields: dict[str, str] | None = None, spring: bool = True) -> _Term:
        if not spring:
            return _Term(text, source or "literal")
        if "#{" in text:
            return self._spel(text, module, rel, fields or {})
        if "${" not in text:
            return _Term(text, source or "literal")
        used: list[tuple[str, str, str | None]] = []
        value = self.expand(text, module, used)
        if value is None:
            key = _first_placeholder_key(text) or _collapse(text)
            return _Term(None, unresolved=f"unresolved:{key}")
        kind, key, profile = used[0] if used else ("property", "", None)
        return _Term(value, source or f"{kind}:{key}",
                     next((item[2] for item in used if item[2]), profile))

    # -- SpEL: bounded, no eval ------------------------------------------------------

    def _spel(self, text: str, module: str, rel: str, fields: dict[str, str]) -> _Term:
        """Evaluate ``#{...}`` segments (string literals, ``+``, ``.split(',')``, ``T(X).C``,
        ``environment[...]``, ``@bean.field``, ``__listener.x``). ``${...}`` placeholders are
        expanded first (Spring does the same); anything else is ``unresolved:#{...}`` with
        the placeholders expanded in the stored text."""
        used: list[tuple[str, str, str | None]] = []
        expanded = self.expand_partial(text, module, used)
        extra: dict[str, object] = {"spel": _collapse(text, 300)}
        out: list[str] = []
        position = 0
        while True:
            start = expanded.find("#{", position)
            if start < 0:
                out.append(expanded[position:])
                break
            out.append(expanded[position:start])
            end = _matching(expanded, start + 1)
            if end < 0:
                return _Term(None, unresolved="unresolved:" + _collapse(expanded), extra=extra)
            value = self._spel_expr(expanded[start + 2:end], module, rel, fields, used)
            if isinstance(value, _Fail):
                if value.bean:
                    extra["bean"] = value.bean
                return _Term(None, unresolved="unresolved:" + _collapse(expanded), extra=extra)
            out.append(value)
            position = end + 1
        value = "".join(out)
        if "${" in value or "#{" in value:
            return _Term(None, unresolved="unresolved:" + _collapse(expanded), extra=extra)
        return _Term(value, "spel", next((item[2] for item in used if item[2]), None),
                     extra=extra)

    def _spel_expr(self, inner: str, module: str, rel: str, fields: dict[str, str],
                   used: list[tuple[str, str, str | None]]) -> str | _Fail:
        parts: list[str] = []
        for _offset, operand in _split_top(inner, "+"):
            value = self._spel_operand(operand.strip(), module, rel, fields, used)
            if isinstance(value, _Fail):
                return value
            parts.append(value)
        return "".join(parts)

    def _spel_operand(self, operand: str, module: str, rel: str, fields: dict[str, str],
                      used: list[tuple[str, str, str | None]]) -> str | _Fail:
        if not operand:
            return _Fail()
        if operand[0] in "'\"":
            quote = operand[0]
            index = 1
            while index < len(operand):
                if operand[index] == quote:
                    if operand[index + 1:index + 2] == quote:  # '' is an escaped quote
                        index += 2
                        continue
                    break
                index += 1
            else:
                return _Fail()
            return self._spel_methods(
                operand[1:index].replace(quote * 2, quote), operand[index + 1:].strip())
        for pattern in (_SPEL_INDEX, _SPEL_GETPROP):
            match = pattern.match(operand)
            if match:
                key = match.group(2)
                prop = self.props.lookup(key, module)
                value: str | None
                if prop is not None:
                    used.append(("property", key, prop.profile))
                    value = self.expand(prop.value, module, used)
                elif pattern is _SPEL_GETPROP and match.group(3) is not None:
                    value = match.group(4)
                else:
                    return _Fail()
                if value is None:
                    return _Fail()
                return self._spel_methods(value, operand[match.end():].strip())
        match = _SPEL_TYPE.match(operand)
        if match:
            owner = match.group(1).rsplit(".", 1)[-1]
            const = self.consts.find(f"{owner}.{match.group(2)}", rel, module)
            if const is None:
                return _Fail()
            expanded = self.expand(const.value, module, used)
            if expanded is None:
                return _Fail()
            return self._spel_methods(expanded, operand[match.end():].strip())
        match = _SPEL_MEMBER.match(operand)
        if match and (match.group(1) or match.group(2) == "__listener"):
            return self._spel_member(match, operand[match.end():].strip(), module, rel,
                                     fields, used)
        return _Fail()

    def _spel_methods(self, value: str, rest: str) -> str | _Fail:
        """Apply a trailing ``.split(',')`` chain: the pieces are kept comma-joined (the
        caller splits on commas)."""
        while rest:
            match = _SPEL_SPLIT.match(rest)
            if match is None:
                return _Fail()
            value = ",".join(part.strip() for part in value.split(match.group(2)))
            rest = rest[match.end():].strip()
        return value

    def _spel_member(self, match: re.Match[str], rest: str, module: str, rel: str,
                     fields: dict[str, str], used: list[tuple[str, str, str | None]],
                     ) -> str | _Fail:
        """``@bean.field`` / ``@bean.getX()`` / ``__listener.field``: an ``@Value("${...}")``
        field or a string constant of that name in the bean's class (or, for
        ``__listener``, the enclosing class)."""
        owner, member, call = match.group(2), match.group(3), bool(match.group(4))
        name = _decapitalize(member[3:]) if call and member.startswith("get") and len(
            member) > 3 else member
        text: str | None = None
        if match.group(1):
            bean = self.beans.get(owner)
            if bean is not None:
                text = bean[1].get(name)
        else:
            text = fields.get(name)
            if text is None:
                const = self.consts.find(name, rel, "")
                text = const.value if const is not None and const.rel == rel else None
        if text is None:
            return _Fail(owner)
        expanded = self.expand(text, module, used)
        if expanded is None:
            return _Fail(owner)
        return self._spel_methods(expanded, rest)

    # -- terms and outcomes ----------------------------------------------------------

    def _template(self, expr: str, rel: str, module: str) -> _Term:
        """A JS template literal: each ``${name}`` must be a known constant."""
        inner = expr[1:-1]
        out: list[str] = []
        position = 0
        while True:
            start = inner.find("${", position)
            if start < 0:
                out.append(inner[position:])
                return _Term("".join(out), "literal")
            end = inner.find("}", start)
            const = (self.consts.find(inner[start + 2:end].strip(), rel, module)
                     if end > 0 else None)
            if const is None:
                return _Term(None, unresolved="unresolved:" + _collapse(expr))
            out.append(inner[position:start] + const.value)
            position = end + 1

    def term(self, expr: str, rel: str, module: str, fields: dict[str, str],
             spring: bool = True) -> _Term | None:
        expr = expr.strip()
        if not spring:
            expr = _msg.unwrap(expr)
        literal = _literal(expr) if spring else _msg.unquote(expr)
        if literal is not None:
            return self._text(literal, None, module, rel, fields, spring)
        if not spring:
            if len(expr) > 1 and expr[0] == "`" and expr[-1] == "`":
                return self._template(expr, rel, module)
            env = _msg.parse_env(expr)
            if env is not None:
                name, default = env
                if default is None:
                    return _Term(None, unresolved=f"unresolved:env:{name}")
                return _Term(default, "env_default", extra={"env": name})
        if not _IDENT_CHAIN.fullmatch(expr):
            return None  # method call, cast, ... : not understood
        bare = expr[5:] if expr.startswith(("this.", "self.")) else expr
        if "." not in bare and bare in fields:
            return self._text(fields[bare], None, module, rel, fields, spring)
        const = self.consts.find(bare, rel, module)
        if const is not None:
            if const.env is not None:
                return _Term(const.value, "env_default", extra={"env": const.env})
            return self._text(const.value, f"constant:{const.name}", module, rel, fields,
                              spring)
        return _Term(None, unresolved=f"unresolved:{expr}")

    def outcome(self, expr: str, rel: str, module: str, fields: dict[str, str],
                spring: bool = True) -> _Outcome | None:
        raw = _collapse((_literal(expr.strip()) if spring else _msg.unquote(expr)) or expr, 300)
        terms: list[_Term] = []
        for _offset, part in _split_top(expr, "+"):
            term = self.term(part, rel, module, fields, spring)
            if term is None:
                return None
            terms.append(term)
        return self._finish(terms, raw)

    def outcome_text(self, text: str, module: str, rel: str = "", source: str | None = None,
                     ) -> _Outcome:
        """Outcome of a configuration value (already literal text: placeholders, SpEL and
        comma lists apply, quotes do not)."""
        return self._finish([self._text(text, source, module, rel)], _collapse(text, 300))

    @staticmethod
    def _finish(terms: list[_Term], raw: str) -> _Outcome:
        extra: dict[str, object] = {}
        for term in terms:
            extra.update(term.extra)
        failed = next((term for term in terms if term.value is None), None)
        if failed is not None:
            return _Outcome([], "", None, failed.unresolved, raw, extra)
        source = next((t.source for t in terms if t.source and t.source != "literal"), "literal")
        profile = next((t.profile for t in terms if t.profile), None)
        value = "".join(term.value or "" for term in terms)
        names = [name.strip() for name in value.split(",") if name.strip()]
        return _Outcome(names, source or "literal", profile, None, raw, extra)


def _value_fields(text: str) -> dict[str, str]:
    """``@Value("${key}") String field`` / Kotlin ``@Value("\\${key}") lateinit var field``."""
    fields: dict[str, str] = {}
    position = 0
    while True:
        index = text.find("Value(", position)
        if index < 0:
            return fields
        position = index + 6
        if index == 0 or text[index - 1] not in "@:":
            continue
        open_index = index + 5
        end = _matching(text, open_index, 600)
        if end < 0:
            continue
        argument = text[open_index + 1:end].strip()
        if argument.startswith("value") and argument[5:].lstrip().startswith("="):
            argument = argument[5:].lstrip()[1:].strip()
        literal = _literal(argument)
        if literal is None:
            continue
        tail = text[end + 1:end + 250].lstrip()  # the declaration is on this or the next line
        cut = min((tail.find(char) for char in ";=,){\n" if char in tail), default=len(tail))
        segment = tail[:cut]
        if "(" in segment:
            continue
        if ":" in segment:
            segment = segment.split(":", 1)[0]
        names = _IDENTIFIER.findall(segment)
        if names:
            fields[names[-1]] = literal
    return fields


@dataclass
class _Hit:
    direction: str  # PRODUCES / CONSUMES / ROUTES_TO
    topic: str  # destination name (a queue / exchange name for non-Kafka brokers)
    line: int  # attestation line
    site_line: int  # line of the listener / send / ProducerRecord keyword
    attributes: dict[str, object]
    unresolved: bool = False
    entity: str = "KafkaTopic"  # KafkaTopic | Queue | Exchange
    broker: str = "kafka"
    partner: _Hit | None = None  # ROUTES_TO: the queue the exchange routes to


@dataclass
class _Res:
    """One messaging call site: what it was, and whether a destination was understood."""

    broker: str
    entity: str
    line: int
    direction: str
    via: str | None  # property / constant / literal when a destination resolved
    unresolved: int = 0  # destinations that could not be resolved


_SEND_METHODS = (".send(", ".sendDefault(", ".produce(")
_JVM_CALL = re.compile(r"\.(sendDefault|send|produce)\(")
_DEFAULT_TOPIC_KEY = "spring.kafka.template.default-topic"


def _is_kafka_receiver(name: str) -> bool:
    low = name.lower()
    if "kafka" in low:
        return True
    return (any(word in low for word in ("producer", "template", "sender"))
            and not any(word in low for word in _RECEIVER_EXCLUDE))


def _line_starts(text: str) -> list[int]:
    return _msg.line_starts(text)


def _topic_hits(
    outcome: _Outcome, direction: str, line: int, site_line: int, *, pattern: bool = False,
    entity: str = "KafkaTopic", broker: str = "kafka",
) -> list[_Hit]:
    def hit(topic: str, attributes: dict[str, object], unresolved: bool = False) -> _Hit:
        return _Hit(direction, topic, line, site_line, attributes, unresolved, entity, broker)

    if outcome.unresolved is not None:
        return [hit(outcome.unresolved, {"unresolved": True, "expression": outcome.expression,
                                         **outcome.extra}, True)]
    hits: list[_Hit] = []
    valid = _TOPIC_NAME if entity == "KafkaTopic" else _DEST_NAME
    for raw_name in outcome.names:
        attributes: dict[str, object] = {"resolved_from": outcome.source, **outcome.extra}
        if outcome.profile:
            attributes["profile"] = outcome.profile
        name = raw_name
        if entity != "KafkaTopic":
            name, found = _msg.dest_name(raw_name)
            attributes.update(found)
        if valid.match(name):
            hits.append(hit(name, attributes))
        elif pattern:
            attributes["pattern"] = name[:240]
            hits.append(hit("pattern:" + name[:240], attributes))
        else:
            hits.append(hit("unresolved:" + _collapse(name), {
                "unresolved": True, "expression": outcome.expression, **outcome.extra}, True))
    return hits


def _binder_is_rabbit(resolver: _Resolver, binding: str, module: str) -> bool:
    for key in (f"spring.cloud.stream.bindings.{binding}.binder",
                "spring.cloud.stream.default-binder"):
        prop = resolver.props.lookup(key, module)
        if prop is not None:
            return "rabbit" in prop.value.lower()
    return False


def _binding_hits(
    outcome: _Outcome, binding: str, direction: str, line: int, site_line: int,
    resolver: _Resolver, module: str,
) -> list[_Hit]:
    """Hits of a Spring Cloud Stream binding: Kafka topics, or exchanges for the Rabbit binder."""
    rabbit = _binder_is_rabbit(resolver, binding, module)
    hits = _topic_hits(outcome, direction, line, site_line,
                       entity="Exchange" if rabbit else "KafkaTopic",
                       broker="rabbitmq" if rabbit else "kafka")
    for hit in hits:
        hit.attributes["binding"] = binding
    return hits


def _jvm_hits(
    rel: str, text: str, module: str, resolver: _Resolver,
) -> tuple[list[_Hit], list[tuple[int, list[_Hit], str]]]:
    """Spring / Java / Kotlin Kafka sites: ``@KafkaListener(topics|topicPattern)`` (plus
    ``@SendTo``), ``<producer>.send(<expr>, ...)`` / ``sendDefault`` / ``produce``,
    ``streamBridge.send(<binding>, ...)``, ``ProducerRecord(<expr>, ...)`` and
    ``SenderRecord.create(<expr>, ...)``.

    Returns every hit and, per call site (one per annotation / send / constructor
    occurrence), ``(line, hits of that site, direction)``; a site with no hits was not
    understood."""
    hits: list[_Hit] = []
    sites: list[tuple[int, list[_Hit], str]] = []
    starts = _line_starts(text)
    fields = _value_fields(text) if "Value(" in text else {}

    def line_at(offset: int) -> int:
        return bisect_right(starts, offset)

    def add(expr: str, direction: str, offset: int, site: int, *, pattern: bool = False,
            into: list[_Hit] | None = None) -> list[_Hit]:
        outcome = resolver.outcome(expr, rel, module, fields)
        found = ([] if outcome is None else
                 _topic_hits(outcome, direction, line_at(offset), site, pattern=pattern))
        hits.extend(found)
        if into is not None:
            into.extend(found)
        return found

    def listener_value(value: str, value_offset: int, name: str, site: int,
                       into: list[_Hit]) -> None:
        value = value.strip()
        if value.startswith(("arrayOf(", "listOf(")) and value.endswith(")"):
            value_offset += value.index("(") + 1
            value = value[value.index("(") + 1:-1]
        elif value[:1] in ("{", "[") and value[-1:] in ("}", "]"):
            value_offset += 1
            value = value[1:-1]
        for item_offset, item in _split_top(value, ",", value_offset):
            if item.strip():
                add(item, "CONSUMES", item_offset + len(item) - len(item.lstrip()), site,
                    pattern=name == "topicPattern", into=into)

    position = 0
    while True:  # @KafkaListener(...)
        at = text.find("@KafkaListener", position)
        if at < 0:
            break
        position = at + 14
        site_hits: list[_Hit] = []
        sites.append((line_at(at), site_hits, "CONSUMES"))
        open_index = position
        while open_index < len(text) and text[open_index] in " \t\r\n":
            open_index += 1
        if open_index >= len(text) or text[open_index] != "(":
            continue
        end = _matching(text, open_index)
        if end < 0:
            continue
        for offset, part in _split_top(text[open_index + 1:end], ",", open_index + 1):
            name, equals, value = part.partition("=")
            name = name.strip()
            if not equals or name not in ("topics", "topicPattern"):
                continue
            lead = len(value) - len(value.lstrip())
            value_offset = offset + len(part.partition("=")[0]) + 1 + lead
            listener_value(value, value_offset, name, line_at(at), site_hits)

    position = 0
    while True:  # @SendTo("topic") on a method that is also a @KafkaListener
        at = text.find("@SendTo", position)
        if at < 0:
            break
        position = at + 7
        open_index = position
        while open_index < len(text) and text[open_index] in " \t\r\n":
            open_index += 1
        if open_index >= len(text) or text[open_index] != "(":
            continue
        end = _matching(text, open_index)
        if end < 0 or not _on_kafka_listener(text, at, end):
            continue
        site_hits = []
        sites.append((line_at(at), site_hits, "PRODUCES"))
        for offset, part in _split_top(text[open_index + 1:end], ",", open_index + 1):
            name, equals, value = part.partition("=")
            if equals and name.strip() != "value":
                continue
            value = value if equals else part
            lead = len(value) - len(value.lstrip())
            shift = offset + (len(name) + 1 if equals else 0) + lead
            value = value.strip()
            if value[:1] in ("{", "[") and value[-1:] in ("}", "]"):
                shift, value = shift + 1, value[1:-1]
            for item_offset, item in _split_top(value, ",", shift):
                if item.strip():
                    add(item, "PRODUCES", item_offset + len(item) - len(item.lstrip()),
                        line_at(at), into=site_hits)

    for found in _JVM_CALL.finditer(text):  # <producer>.send(<expr>, ...) and friends
        at, method, paren = found.start(), found.group(1), found.end() - 1
        begin = at
        while begin > 0 and (text[begin - 1].isalnum() or text[begin - 1] == "_"):
            begin -= 1
        receiver = text[begin:at]
        bridge = method == "send" and "streambridge" in receiver.lower()
        if begin == at or not (bridge or _is_kafka_receiver(receiver)):
            continue
        end = _matching(text, paren)
        if end < 0:
            continue
        items = _split_top(text[paren + 1:end], ",", paren + 1)
        first_offset, first = items[0]
        site = line_at(at)
        site_hits = []
        if method == "sendDefault":
            outcome = _default_topic(resolver, rel, module, fields)
            if outcome is not None:
                site_hits = _topic_hits(outcome, "PRODUCES", site, site)
                hits.extend(site_hits)
            sites.append((site, site_hits, "PRODUCES"))
        elif first.strip() and "ProducerRecord" not in first:  # a ProducerRecord site otherwise
            offset = first_offset + len(first) - len(first.lstrip())
            if bridge:
                outcome = resolver.outcome(first, rel, module, fields)
                if outcome is not None:
                    site_hits = _bridge_hits(outcome, site, line_at(offset), resolver, module)
                    hits.extend(site_hits)
                sites.append((site, site_hits, "PRODUCES"))
            else:
                sites.append((site, add(first, "PRODUCES", offset, site), "PRODUCES"))

    for keyword in ("ProducerRecord", "SenderRecord.create"):
        position = 0
        while True:  # ProducerRecord<...>(<expr>, ...) / SenderRecord.create(<expr>, ...)
            at = text.find(keyword, position)
            if at < 0:
                break
            position = at + len(keyword)
            if at > 0 and (text[at - 1].isalnum() or text[at - 1] == "_"):
                continue
            index = position
            while index < len(text) and text[index] in " \t":
                index += 1
            if keyword == "ProducerRecord" and index < len(text) and text[index] == "<":
                depth = 0
                for cursor in range(index, min(len(text), index + 300)):
                    depth += text[cursor] == "<"
                    depth -= text[cursor] == ">"
                    if depth == 0:
                        index = cursor + 1
                        break
                else:
                    continue
                while index < len(text) and text[index] in " \t":
                    index += 1
            if index >= len(text) or text[index] != "(":
                continue
            end = _matching(text, index)
            if end < 0:
                continue
            first_offset, first = _split_top(text[index + 1:end], ",", index + 1)[0]
            if keyword != "ProducerRecord" and "ProducerRecord" in first:
                continue  # counted as a ProducerRecord site
            site_hits = []
            sites.append((line_at(at), site_hits, "PRODUCES"))
            if first.strip():
                add(first, "PRODUCES", first_offset + len(first) - len(first.lstrip()),
                    line_at(at), into=site_hits)
    return hits, sites


_METHOD_OPEN = re.compile(r"\)\s*\{")


def _on_kafka_listener(text: str, at: int, end: int) -> bool:
    """Does the method annotated by the ``@SendTo`` at ``at`` also carry ``@KafkaListener``?"""
    before = text[max(0, at - 600):at]
    listener = before.rfind("@KafkaListener")
    if listener >= 0 and not _METHOD_OPEN.search(before, listener):
        return True
    after = text[end + 1:end + 600]
    stop = _METHOD_OPEN.search(after)
    head = after[:stop.start()] if stop else after
    return "@KafkaListener" in head.split(";", 1)[0]


def _default_topic(
    resolver: _Resolver, rel: str, module: str, fields: dict[str, str],
) -> _Outcome | None:
    """Topic of ``sendDefault(...)``: ``spring.kafka.template.default-topic`` or a
    ``setDefaultTopic(X)`` call in the same module; else an unresolved placeholder."""
    prop = resolver.props.lookup(_DEFAULT_TOPIC_KEY, module)
    if prop is not None:
        return resolver.outcome_text(prop.value, module, rel, source=f"property:{_DEFAULT_TOPIC_KEY}")
    declared = resolver.default_topics.get(module)
    if declared is not None:
        decl_rel, expr, decl_fields = declared
        return resolver.outcome(expr, decl_rel, module, decl_fields)
    return _Outcome([], "", None, "unresolved:sendDefault", "sendDefault")


def _bridge_hits(
    outcome: _Outcome, site: int, line: int, resolver: _Resolver, module: str,
) -> list[_Hit]:
    """``streamBridge.send(<binding or destination>, ...)``: the binding's
    ``spring.cloud.stream.bindings.<name>.destination`` if configured, else the name itself."""
    if outcome.unresolved is not None:
        return _topic_hits(outcome, "PRODUCES", line, site)
    found: list[_Hit] = []
    for name in outcome.names:
        key = f"spring.cloud.stream.bindings.{name}.destination"
        prop = resolver.props.lookup(key, module)
        resolved = (resolver.outcome_text(prop.value, module, source=f"property:{key}")
                    if prop is not None else
                    _Outcome([name], outcome.source, outcome.profile, None, outcome.expression,
                             outcome.extra))
        found.extend(_binding_hits(resolved, name, "PRODUCES", line, site, resolver, module))
    return found


def _default_topic_decl(text: str, rel: str) -> tuple[str, str, dict[str, str]] | None:
    """``(file, expression, @Value fields)`` of a ``setDefaultTopic(<expr>)`` call."""
    at = text.find("setDefaultTopic(")
    if at < 0:
        return None
    end = _matching(text, at + 15)
    if end < 0:
        return None
    expr = _split_top(text[at + 16:end], ",")[0][1].strip()
    if not expr:
        return None
    return rel, expr, _value_fields(text) if "Value(" in text else {}


def _kafka_like(line: str) -> bool:
    """Cheap substring test: does this line look like a Kafka produce/consume site?"""
    text = line.strip()
    if not text or len(text) > _MAX_LINE or text.startswith(
            ("import ", "package ", "from ", "//", "/*", "*", "#")):
        return False
    if "@KafkaListener" in text or "producer.send" in text or "producer.produce" in text:
        return True
    if any(call in text for call in _SEND_METHODS) and any(
            word in text for word in ("emplate", "afka", "roducer", "ender", "treamBridge")):
        return True
    if ".subscribe(" in text:
        return True
    if "ProducerRecord" in text:
        tail = text.split("ProducerRecord", 1)[1]
        return tail.lstrip().startswith("(") or (tail.startswith("<") and ">(" in tail)
    return ("KafkaConsumer(" in text or "AIOKafkaConsumer(" in text) and "new " not in text


def _guess_direction(line: str) -> str:
    low = line.lower()
    return "CONSUMES" if any(word in low for word in ("subscribe", "consumer", "listener")) \
        else "PRODUCES"


def _source_kind(hits: list[_Hit]) -> str:
    kinds = set()
    for hit in hits:
        source = str(hit.attributes.get("resolved_from", ""))
        kinds.add("property" if source.startswith(("property:", "default:", "spel", "env_default"))
                  else "constant" if source.startswith("constant:") else "literal")
    return "property" if "property" in kinds else "constant" if "constant" in kinds else "literal"


def _site_result(rule_broker: str, rule_entity: str, line: int, direction: str,
                 site_hits: list[_Hit]) -> _Res:
    good = [hit for hit in site_hits if not hit.unresolved]
    return _Res(site_hits[0].broker if site_hits else rule_broker,
                site_hits[0].entity if site_hits else rule_entity, line, direction,
                _source_kind(good) if good else None, len(site_hits) - len(good))


def _file_results(
    rel: str, text: str, lowered: str, module: str, resolver: _Resolver,
) -> tuple[list[_Hit], list[_Res]]:
    """Every messaging hit of one file and one :class:`_Res` per call site."""
    suffix = PurePosixPath(rel).suffix
    jvm = suffix in _JVM_SUFFIXES
    fields = _value_fields(text) if jvm and "Value(" in text else {}
    hits: list[_Hit] = []
    results: list[_Res] = []
    accounted: set[int] = set()
    for direction, binding, raw, line in _binding_entries(rel, text):
        outcome = resolver.outcome_text(raw, module, rel)
        hits.extend(_binding_hits(outcome, binding, direction, line, line, resolver, module))
    if jvm:
        jvm_hits, jvm_sites = _jvm_hits(rel, text, module, resolver)
        hits.extend(jvm_hits)
        for number, jvm_site_hits, direction in jvm_sites:
            results.append(_site_result("kafka", "KafkaTopic", number, direction, jvm_site_hits))
            accounted.add(number)
    for site in _msg.find_sites(suffix, text, lowered):
        accounted.add(site.line)
        rule = site.rule
        site_hits: list[_Hit] = []
        if site.direction == "ROUTES_TO":
            site_hits, result = _route_hits(site, rel, module, resolver, fields, jvm)
            results.append(result)
        else:
            for dest in site.dests:
                resolved = resolver.outcome(dest.expr, rel, module, fields, spring=jvm)
                if resolved is not None:
                    site_hits.extend(_topic_hits(
                        resolved, site.direction, dest.line, site.line, entity=dest.entity,
                        broker=rule.broker))
            results.append(_site_result(rule.broker, site.dests[0].entity if site.dests
                                        else rule.entity, site.line, site.direction, site_hits))
        hits.extend(site_hits)
    if "afka" in text or "AFKA" in text:  # kafka-like lines no site above accounts for
        for number, line_text in enumerate(text.split("\n"), 1):
            if number not in accounted and _kafka_like(line_text):
                results.append(_Res("kafka", "KafkaTopic", number, _guess_direction(line_text),
                                    None))
    return hits, results


def _route_hits(
    site: _msg.Site, rel: str, module: str, resolver: _Resolver, fields: dict[str, str],
    jvm: bool,
) -> tuple[list[_Hit], _Res]:
    """``Exchange ROUTES_TO Queue`` hits of a bind call and its site result. An edge between
    placeholders would be noise, so an unresolved end yields no hit (counted as unresolved)."""
    rule = site.rule
    outcomes = [resolver.outcome(dest.expr, rel, module, fields, spring=jvm)
                for dest in site.dests]
    if len(outcomes) != 2 or outcomes[0] is None or outcomes[1] is None:
        return [], _Res(rule.broker, "Exchange", site.line, "ROUTES_TO", None)
    exchanges = _topic_hits(outcomes[0], "ROUTES_TO", site.dests[0].line, site.line,
                            entity="Exchange", broker=rule.broker)
    queues = _topic_hits(outcomes[1], "ROUTES_TO", site.dests[1].line, site.line,
                         entity="Queue", broker=rule.broker)
    unresolved = sum(hit.unresolved for hit in exchanges + queues)
    if unresolved:
        return [], _Res(rule.broker, "Exchange", site.line, "ROUTES_TO", None, unresolved)
    routes = [_Hit("ROUTES_TO", ex.topic, ex.line, site.line, ex.attributes, False, "Exchange",
                   ex.broker, partner=queue)
              for ex in exchanges for queue in queues]
    return routes, _Res(rule.broker, "Exchange", site.line, "ROUTES_TO",
                        _source_kind(exchanges + queues))


# -- scan --------------------------------------------------------------------------------


_MARKER_PRIORITY = (
    "spring_boot_plugin", "maven_spring_boot_plugin", "gradle_application_plugin",
    "gradle_main_class", "spring_boot_application", "main_function", "package_json_start",
    "package_json_bin", "go_package_main", "python_scripts", "python_main_module", "procfile",
)
_GO_PACKAGE_MAIN = re.compile(r"^package\s+main\b", re.MULTILINE)
_PLAIN_NAME = re.compile(r"[A-Za-z0-9._-]{1,200}")
_NAME_DEFAULT = re.compile(r"\$\{[^:{}]{1,200}:([^{}]{1,200})\}")


def _marker_rank(marker: tuple[str, str, int]) -> int:
    rule = marker[0]
    return _MARKER_PRIORITY.index(rule) if rule in _MARKER_PRIORITY else len(_MARKER_PRIORITY)


def _spring_app_name(value: str) -> str | None:
    """``spring.application.name`` as a service name (``${X:default}`` -> ``default``)."""
    text = value.strip()
    found = _NAME_DEFAULT.fullmatch(text)
    if found:
        text = found.group(1).strip()
    return text if _PLAIN_NAME.fullmatch(text) else None


def _default_service_name(module: _Module, repo: str) -> str:
    """Package / module name of a deployable that no chart, workload or Spring app names."""
    for ecosystem in ("npm", "python", "cargo"):
        if ecosystem in module.names:
            return module.names[ecosystem]
    if "maven" in module.names:
        return module.names["maven"].rsplit(":", 1)[-1]
    if "go" in module.names:
        return module.names["go"].rstrip("/").rsplit("/", 1)[-1]
    return PurePosixPath(module.path).name or module.fallback or repo


def _primary_class(text: str) -> str | None:
    """Name of the first class / object / interface declared in a JVM source file."""
    for raw in text.split("\n"):
        stripped = raw.strip()
        if len(raw) > _MAX_LINE or stripped.startswith(("//", "/*", "*", "import ", "package ")):
            continue
        found = _TYPE_DECL.search(raw)
        if found:
            return found.group(2)
    return None


@dataclass
class _Stats:
    """Per-broker call-site counters (the shape of ``summary["messaging"][broker]``)."""

    sites: int = 0
    producer_sites: int = 0
    consumer_sites: int = 0
    matched_sites: int = 0
    unmatched_sites: int = 0
    unresolved: int = 0
    skipped_no_owner: int = 0
    skipped_schema: int = 0
    via_property: int = 0
    via_constant: int = 0
    skipped_files: int = 0
    examples: list[str] = field(default_factory=list)

    def public(self) -> dict[str, object]:
        return {
            "sites": self.sites, "producer_sites": self.producer_sites,
            "consumer_sites": self.consumer_sites, "matched_sites": self.matched_sites,
            "unmatched_sites": self.unmatched_sites, "unresolved": self.unresolved,
            "skipped_no_owner": self.skipped_no_owner, "skipped_schema": self.skipped_schema,
            "unmatched_examples": self.examples[:5],
        }


def scan_repository(
    root: str | Path,
    *,
    pipeline: str | None = None,
    source: str | None = None,
    repo_name: str | None = None,
    environment: str = "prod",
    kafka_cluster: str = "main",
    schema_version: int = 3,
    heuristics: HeuristicMode = "candidate",
    include_tests: bool = False,
) -> ScanResult:
    """Scan ``root`` and return a seed manifest plus a summary of what was found."""
    base = Path(root).resolve()
    if not base.is_dir():
        raise OntologyError("invalid", f"{root} is not a directory")
    if schema_version not in (1, 2, 3):
        raise OntologyError("invalid", "schema_version must be 1, 2 or 3")
    repo = repo_name or base.name or "repo"
    files = _list_files(base)
    warnings: list[str] = []

    # 1. Modules from build files.
    modules: dict[str, _Module] = {}
    gradle_roots = _gradle_roots(files)
    gradle_dirs = {_dir(path) for path in files
                   if PurePosixPath(path).name in ("build.gradle", "build.gradle.kts")}
    parsers = {
        "package.json": _parse_package_json, "pyproject.toml": _parse_pyproject,
        "go.mod": _parse_go_mod, "Cargo.toml": _parse_cargo, "pom.xml": _parse_pom,
    }
    for rel in files:
        name = PurePosixPath(rel).name
        directory = _dir(rel)
        is_gradle = name in ("build.gradle", "build.gradle.kts")
        if name not in parsers and not is_gradle:
            continue
        text = _read(base, rel)
        if text is None:
            continue
        module = modules.get(directory) or _Module(
            path=directory,
            fallback=repo if directory == "." else PurePosixPath(directory).name)
        try:
            if is_gradle:
                # A root project that only aggregates subprojects is not a module.
                if directory in gradle_roots and any(
                    other != directory and other.startswith("" if directory == "." else directory + "/")
                    for other in gradle_dirs
                ):
                    continue
                _parse_gradle(text, rel, module, _gradle_path(directory, gradle_roots))
            else:
                parsers[name](text, rel, module)
        except (ValueError, ET.ParseError, tomllib.TOMLDecodeError) as exc:
            warnings.append(f"skipped {rel}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        if module.names or (module.anonymous and not module.aggregator):
            module.build_files.append(rel)
            modules[directory] = module
    # A JS/TS tree with no build file above some source: one synthetic module named after the repo.
    if "." not in modules:
        orphan = next((rel for rel in files if PurePosixPath(rel).suffix in _msg.JS
                       and not _is_test_path(rel) and _owning_module(rel, modules) is None), None)
        if orphan is not None:
            modules["."] = _Module(path=".", fallback=repo, evidence=orphan)

    # 2. Services from container / deployment files, then from deployable signals.
    spring_names: dict[str, tuple[str, str | None]] = {}  # module -> (name, profile)
    for rel in files:
        name = PurePosixPath(rel).name
        suffix = PurePosixPath(rel).suffix
        owner = _owning_module(rel, modules)
        if owner is None or _is_test_path(rel):
            continue
        if name == "Chart.yaml" or "Dockerfile" in name or suffix in (".yaml", ".yml"):
            config = _CONFIG_FILE.fullmatch(name) if suffix in (".yaml", ".yml") else None
            text = _read(base, rel) or ""
            if config:
                app = _parse_yaml_props(text).get("spring.application.name")
                _note_spring_name(spring_names, owner.path, app, config.group(2))
            found = _service_name(rel, text)
            if found is None:
                continue
            service_name, line = found
            rank = 6 if name == "Chart.yaml" and service_name else 4 if service_name else 2
            if rank > owner.service_rank:
                owner.service_rank = rank
                owner.service = service_name or PurePosixPath(owner.path).name or repo
                owner.service_evidence = (rel, line)
                owner.service_detected_by = (
                    "chart" if rank == 6 else "k8s" if rank == 4 else "dockerfile")
        elif suffix == ".properties" and _CONFIG_FILE.fullmatch(name):
            config = _CONFIG_FILE.fullmatch(name)
            app = _parse_properties(_read(base, rel) or "").get("spring.application.name")
            _note_spring_name(spring_names, owner.path, app, config.group(2))  # type: ignore[union-attr]
        elif suffix in _JVM_SUFFIXES or suffix == ".go":
            text = _read(base, rel) or ""
            if suffix == ".go":
                found_main = _GO_PACKAGE_MAIN.search(text)
                if found_main:
                    owner.markers.append(("go_package_main", rel, _line(text, found_main.start())))
            elif "@SpringBootApplication" in text:
                owner.markers.append(
                    ("spring_boot_application", rel, _line_of(text, "@SpringBootApplication")))
            elif "fun main(" in text or "static void main(" in text:
                needle = "fun main(" if "fun main(" in text else "static void main("
                owner.markers.append(("main_function", rel, _line_of(text, needle)))
        elif name == "__main__.py":
            owner.markers.append(("python_main_module", rel, 1))
        elif name == "Procfile":
            owner.markers.append(("procfile", rel, 1))
    for module in modules.values():
        spring_name = spring_names.get(module.path, (None, None))[0]
        if module.service_rank == 0 and module.markers:
            rule, marker_rel, marker_line = min(module.markers, key=_marker_rank)
            module.service_rank = 1
            module.service_detected_by = rule
            module.service_evidence = (marker_rel, marker_line)
            module.service = spring_name or _default_service_name(module, repo)
        elif module.service and module.service_rank <= 2 and spring_name:
            module.service = spring_name  # Chart > k8s > spring.application.name > module name

    # 3. CODEOWNERS (first location found wins, as on GitHub).
    rules: list[tuple[re.Pattern[str], list[str], int]] = []
    codeowners_file = next((path for path in CODEOWNERS_PATHS if path in set(files)), None)
    if codeowners_file:
        rules = _parse_codeowners(_read(base, codeowners_file) or "")

    entities: dict[tuple[str, str], dict[str, object]] = {}
    relations: dict[tuple[str, str, str], dict[str, object]] = {}

    def entity(entity_type: str, key: str, **extra: object) -> str:
        identity = (entity_type, Entity.canonicalize(key))
        entities.setdefault(identity, {"type": entity_type, "key": key, **extra})
        return f"{entity_type}:{key}"

    def relate(src: str, relation_type: str, dst: str, evidence: tuple[str, int], *,
               heuristic: bool = False, candidate_only: bool = False,
               attributes: dict[str, object] | None = None) -> bool:
        """``candidate_only`` edges (unresolved topics) stay candidates even with
        ``heuristics="verified"``. True when the relation is new."""
        if heuristic and heuristics == "off":
            return False
        identity = (src, relation_type, dst)
        if identity in relations:
            return False
        verified = not heuristic or (heuristics == "verified" and not candidate_only)
        record: dict[str, object] = {
            "src": src, "type": relation_type, "dst": dst,
            "status": "verified" if verified else "candidate",
            "confidence": (UNRESOLVED_CONFIDENCE if candidate_only
                           else HEURISTIC_CONFIDENCE if heuristic else STRUCTURAL_CONFIDENCE),
            "attestation": f"{evidence[0]}#L{evidence[1]}",
        }
        if attributes:
            record["attributes"] = attributes
        relations[identity] = record
        return True

    v2 = schema_version >= 2
    repo_ref = entity("Repo", repo, attributes={"name": repo}) if v2 else None
    module_ref: dict[str, str] = {}
    service_ref: dict[str, str] = {}
    by_identity: dict[tuple[str, str], _Module] = {}

    def module_evidence(module: _Module) -> tuple[str, int]:
        return (module.build_files[0] if module.build_files else module.evidence or ".", 1)

    for module in sorted(modules.values(), key=lambda item: item.path):
        for ecosystem, identity in module.names.items():
            by_identity[(ecosystem, identity)] = module
            if ecosystem == "maven" and ":" in identity:
                by_identity.setdefault(("maven", identity.split(":", 1)[1]), module)
        if v2:
            aliases = sorted({identity for identity in module.names.values()
                              if identity != module.key})
            module_ref[module.path] = entity(
                "Module", module.key, display_name=module.display,
                aliases=aliases, attributes={
                    "name": module.display, "path": module.path,
                    "build": sorted({PurePosixPath(rel).name for rel in module.build_files}),
                },
            )
            relate(repo_ref, "CONTAINS", module_ref[module.path],  # type: ignore[arg-type]
                   module_evidence(module))
        if module.service:
            service_ref[module.path] = entity(
                "Service", module.service,
                attributes={"name": module.service, "environment": environment,
                            "module": module.path,
                            "detected_by": module.service_detected_by or "unknown"},
            )
            if v2:
                relate(module_ref[module.path], "BUILDS", service_ref[module.path],
                       module.service_evidence or module_evidence(module))

    # Module dependencies inside the repo.
    module_deps: dict[str, set[str]] = {}
    for module in modules.values():
        for ecosystem, name, rel, line in module.deps:
            target = by_identity.get((ecosystem, name))
            if ecosystem == "maven" and target is None and ":" in name:
                target = by_identity.get(("maven", name.split(":", 1)[1]))
            if target is None or target is module:
                continue
            module_deps.setdefault(module.path, set()).add(target.path)
            if v2:
                relate(module_ref[module.path], "DEPENDS_ON", module_ref[target.path], (rel, line))
            elif module.path in service_ref and target.path in service_ref:
                relate(service_ref[module.path], "DEPENDS_ON", service_ref[target.path], (rel, line))

    # Ownership.
    teams = 0
    if rules:
        for module in modules.values():
            owners, line = _owners_for(module.path, rules)
            for handle in owners:
                team_key = handle.lstrip("@")
                team = entity("Team", team_key, attributes={
                    "name": team_key, "kind": "team" if "/" in team_key else (
                        "email" if "@" in team_key else "user")})
                if v2:
                    relate(team, "OWNS", module_ref[module.path], (codeowners_file, line))  # type: ignore[arg-type]
                if module.path in service_ref:
                    relate(team, "OWNS", service_ref[module.path], (codeowners_file, line))  # type: ignore[arg-type]
        teams = sum(1 for (entity_type, _key) in entities if entity_type == "Team")

    # Which node an edge from a messaging site in module M starts at: M's service; else every
    # service that (transitively) depends on M; else the Module (schema 2+); else nothing.
    dependents: dict[str, set[str]] = {}
    for dependent, targets in module_deps.items():
        for target_path in targets:
            dependents.setdefault(target_path, set()).add(dependent)
    source_cache: dict[str, list[tuple[str, str | None, str]]] = {}

    def sources_for(home: _Module) -> list[tuple[str, str | None, str]]:
        if home.path in source_cache:
            return source_cache[home.path]
        found: list[tuple[str, str | None, str]]
        if home.path in service_ref:
            found = [(service_ref[home.path], None, "service")]
        else:
            seen = {home.path}
            queue = [home.path]
            while queue:
                for parent in dependents.get(queue.pop(), ()):
                    if parent not in seen:
                        seen.add(parent)
                        queue.append(parent)
            found = [(service_ref[path], home.key, "dependency")
                     for path in sorted(seen - {home.path}) if path in service_ref]
            if not found and v2:
                found = [(module_ref[home.path], None, "module")]
        source_cache[home.path] = found
        return found

    # Messaging usage (heuristic). Pass 1 reads every source file once and builds the
    # property / constant indexes; pass 2 resolves the sites of the files that mention a broker.
    stats: dict[str, _Stats] = {}
    attribution = {"edges_from_service": 0, "edges_via_dependency": 0, "edges_from_module": 0}
    files_examined = 0
    if heuristics != "off":
        props = _PropIndex()
        consts = _ConstIndex()
        beans: dict[str, tuple[str, dict[str, str]]] = {}
        default_topics: dict[str, tuple[str, str, dict[str, str]]] = {}
        cache: dict[str, str] = {}
        for rel in files:
            suffix = PurePosixPath(rel).suffix
            if suffix not in SOURCE_SUFFIXES or (not include_tests and _is_test_path(rel)):
                continue
            text = _read(base, rel)
            if not text:
                continue
            files_examined += 1
            home = _owning_module(rel, modules)
            module_path = home.path if home else ""
            config_name = _CONFIG_FILE.fullmatch(PurePosixPath(rel).name)
            if config_name:
                parse = _parse_properties if suffix == ".properties" else _parse_yaml_props
                props.add(module_path, config_name.group(2), rel, parse(text))
            elif suffix in _JVM_SUFFIXES:
                found_consts = _extract_constants(text, suffix)
                for const_owner, name, value in found_consts:
                    consts.add(const_owner, name, value, rel, module_path)
                primary = _primary_class(text)
                if primary:
                    members = _value_fields(text) if "Value(" in text else {}
                    for const_owner, name, value in found_consts:
                        if const_owner == primary:
                            members.setdefault(name, value)
                    if members:
                        existing = beans.get(_decapitalize(primary), (primary, {}))[1]
                        beans[_decapitalize(primary)] = (primary, {**existing, **members})
                declared = _default_topic_decl(text, rel)
                if declared is not None:
                    default_topics.setdefault(module_path, declared)
            else:
                for const_owner, name, value, env in _msg.extract_constants(text, suffix):
                    consts.add(const_owner, name, value, rel, module_path, env)
            if _msg.mentions_messaging(text) is not None:
                cache[rel] = text
        resolver = _Resolver(props, consts)
        resolver.beans = beans
        resolver.default_topics = default_topics
        for rel, text in cache.items():
            home = _owning_module(rel, modules)
            sources = sources_for(home) if home is not None else []
            hits, results = _file_results(rel, text, text.lower(), home.path if home else "",
                                          resolver)
            lines = text.split("\n")
            kafka_skipped = False
            for res in sorted(results, key=lambda item: item.line):
                st = stats.setdefault(res.broker, _Stats())
                st.sites += 1
                st.producer_sites += res.direction == "PRODUCES"
                st.consumer_sites += res.direction == "CONSUMES"
                if res.entity != "KafkaTopic" and schema_version < 3:
                    st.skipped_schema += 1
                    continue
                if not sources:
                    st.skipped_no_owner += 1
                    st.unmatched_sites += 1
                    kafka_skipped = kafka_skipped or res.broker == "kafka"
                    st.examples.append(f"{rel}:{res.line}: {lines[res.line - 1].strip()[:120]}")
                    continue
                st.unresolved += res.unresolved
                if res.via is not None:
                    st.matched_sites += 1
                    st.via_property += res.via == "property"
                    st.via_constant += res.via == "constant"
                elif not res.unresolved:
                    st.unmatched_sites += 1
                    st.examples.append(f"{rel}:{res.line}: {lines[res.line - 1].strip()[:120]}")
            if kafka_skipped:
                stats["kafka"].skipped_files += 1
            if not sources:
                continue
            for hit in hits:
                if hit.entity != "KafkaTopic" and schema_version < 3:
                    continue
                dest_ref = _destination_ref(hit, entity, kafka_cluster)
                if hit.direction == "ROUTES_TO" and hit.partner is not None:
                    relate(dest_ref, "ROUTES_TO", _destination_ref(hit.partner, entity, kafka_cluster),
                           (rel, hit.line), heuristic=True)
                    continue
                for src, via, kind in sources:
                    if relate(src, hit.direction, dest_ref, (rel, hit.line), heuristic=True,
                              candidate_only=hit.unresolved,
                              attributes={"via_module": via} if via else None):
                        attribution[{"service": "edges_from_service",
                                     "dependency": "edges_via_dependency",
                                     "module": "edges_from_module"}[kind]] += 1

    raw = {
        "pipeline": pipeline, "source": source or _slug(repo), "schema_version": schema_version,
        "entities": [entities[key] for key in sorted(entities)],
        "relations": [relations[key] for key in sorted(relations)],
    }
    manifest = parse_manifest({key: value for key, value in raw.items() if value is not None})

    # Hand-written module manifests win over scanned entities with the same identity.
    hand = [path for path in files if PurePosixPath(path).name in HAND_MANIFEST_NAMES]
    if hand:
        loaded = [(path, load_manifest(base / path)) for path in hand]
        declared_keys = {(item.type, Entity.canonicalize(item.key))
                         for _path, extra in loaded for item in extra.entities}
        scanned = manifest.model_copy(update={"entities": tuple(
            item for item in manifest.entities
            if (item.type, Entity.canonicalize(item.key)) not in declared_keys
        )})
        manifest = merge_manifests(
            [("scan", scanned), *loaded], source=manifest.source, pipeline=pipeline,
        ).manifest

    by_type: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for relation in manifest.relations:
        by_type[relation.type] = by_type.get(relation.type, 0) + 1
        statuses[relation.status] = statuses.get(relation.status, 0) + 1
    destinations = {name: sum(1 for item in manifest.entities if item.type == name)
                    for name in ("KafkaTopic", "Queue", "Exchange")}
    kafka_stats = stats.get("kafka", _Stats())
    kafka: dict[str, object] = {
        "files_examined": files_examined, "kafka_like_sites": kafka_stats.sites,
        "matched_sites": kafka_stats.matched_sites,
        "resolved_via_property": kafka_stats.via_property,
        "resolved_via_constant": kafka_stats.via_constant,
        "unresolved_placeholders": kafka_stats.unresolved,
        "unmatched_sites": kafka_stats.unmatched_sites,
        "skipped_no_owner": kafka_stats.skipped_files,
        "unmatched_examples": kafka_stats.examples[:5],
        "producer_sites": kafka_stats.producer_sites,
        "consumer_sites": kafka_stats.consumer_sites,
    }
    messaging = {broker: item.public() for broker, item in sorted(stats.items()) if item.sites}
    summary = {
        "root": str(base), "files": len(files), "modules": len(modules),
        "services": len(service_ref), "teams": teams,
        "topics": destinations["KafkaTopic"],
        "entities": len(manifest.entities), "relations": by_type, "statuses": statuses,
        "codeowners": codeowners_file, "hand_manifests": hand, "warnings": warnings,
        "schema_version": schema_version, "kafka": kafka, "messaging": messaging,
        "attribution": attribution, "destinations": destinations,
    }
    warnings.extend(_messaging_warnings(stats, manifest, schema_version))
    return ScanResult(manifest=manifest, summary=summary)


def _note_spring_name(
    names: dict[str, tuple[str, str | None]], module: str,
    entry: tuple[str, int] | None, profile: str | None,
) -> None:
    """Remember ``spring.application.name`` (base config beats a profile; first file wins)."""
    name = _spring_app_name(entry[0]) if entry else None
    if name is None:
        return
    current = names.get(module)
    if current is None or (profile is None and current[1] is not None):
        names[module] = (name, profile)


def _destination_ref(hit: _Hit, add: Callable[..., str], kafka_cluster: str) -> str:
    """Register the Kafka topic / queue / exchange entity of a hit; returns ``Type:key``
    (``add`` is the scan's entity registrar)."""
    if hit.entity == "KafkaTopic":
        return add("KafkaTopic", hit.topic, attributes={
            "name": hit.topic, "cluster": kafka_cluster, **hit.attributes})
    return add(hit.entity, f"{hit.broker}:{hit.topic}", attributes={
        "broker": hit.broker, "name": hit.topic, **hit.attributes})


def _messaging_warnings(
    stats: dict[str, _Stats], manifest: SeedManifest, schema_version: int,
) -> list[str]:
    """Coverage warnings per broker: zero destinations despite sites, mostly unmatched sites,
    unresolved destinations, and Queue / Exchange sites dropped by an old schema."""
    found: dict[str, int] = {}
    for entry in manifest.entities:
        broker = ("kafka" if entry.type == "KafkaTopic"
                  else str(entry.attributes.get("broker", "")) if entry.type in (
                      "Queue", "Exchange") else None)
        if broker:
            found[broker] = found.get(broker, 0) + 1
    messages: list[str] = []
    skipped_schema = sum(item.skipped_schema for item in stats.values())
    if skipped_schema:
        brokers = ", ".join(sorted(name for name, item in stats.items() if item.skipped_schema))
        messages.append(
            f"{skipped_schema} Queue/Exchange site(s) ({brokers}) were not emitted because "
            f"schema version {schema_version} has no Queue / Exchange types; use "
            "--schema-version 3 and pin [ontology] pack_versions = { infrastructure = 3 }")
    for broker, item in sorted(stats.items()):
        like = item.sites - item.skipped_schema
        label = "Kafka" if broker == "kafka" else broker
        noun, nouns = ("topic", "topics") if broker == "kafka" else ("destination", "destinations")
        if like and (found.get(broker, 0) == 0 or item.unmatched_sites * 2 > like):
            if found.get(broker, 0) == 0:
                head = f"found {like} {label}-like site(s) but extracted 0 {nouns}"
            else:
                head = f"{item.unmatched_sites} of {like} {label}-like site(s) were not recognised"
            if item.skipped_no_owner:
                owned = item.skipped_files if broker == "kafka" else item.skipped_no_owner
                what = "file(s)" if broker == "kafka" else "site(s)"
                cause = (f"{owned} {what} with {label} usage ({item.skipped_no_owner} site(s)) "
                         "were skipped because no module (or, with schema v1, no service) "
                         "owns them")
                if item.skipped_no_owner == like:
                    cause += "; add a build file / Dockerfile above them"
            else:
                cause = ("no site matched a supported pattern (destination built by a method "
                         "call or expression, or an unsupported client)")
            examples = item.examples[:3]
            messages.append(f"{head}: {cause}. Examples: {'; '.join(examples)}"
                            if examples else f"{head}: {cause}.")
        if item.unresolved:
            messages.append(
                f"{item.unresolved} {label} site(s) use a {noun} that could not be resolved "
                "(unknown property, SpEL or local variable); they are recorded as candidate "
                f"'unresolved:*' {nouns}",
            )
    return messages


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    return slug or "repo"


__all__ = ["HAND_MANIFEST_NAMES", "ScanResult", "scan_repository"]
