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
Kafka client code and Spring    ``PRODUCES`` / ``CONSUMES`` ``KafkaTopic``  candidate
Cloud Stream bindings           (literals, Spring ``${...}`` placeholders   (heuristic)
                                and ``static final`` / ``const val``
                                constants are resolved; the rest become
                                ``unresolved:*`` topics)
``ncp-ontology.{toml,json,      hand-written facts, merged in; they win     as written
yaml,yml}`` in any module       over scanned entities with the same key
==============================  ==========================================  ==========

``schema_version=2`` (default) needs ``infrastructure`` v2 (``Repo``,
``Module``). ``schema_version=1`` drops modules and repos, lifts module
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
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from ncp.ontology.facade import OntologyError
from ncp.ontology.models import Entity
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
                             ".go", ".properties", ".yml", ".yaml"})
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
    service_rank: int = 0  # 3 Chart.yaml, 2 named k8s workload, 1 Dockerfile / unnamed

    @property
    def key(self) -> str:
        return self.path if self.path != "." else "(root)"

    @property
    def display(self) -> str:
        for ecosystem in ("npm", "python", "cargo", "maven", "go", "gradle"):
            if ecosystem in self.names:
                return self.names[ecosystem]
        return PurePosixPath(self.path).name or self.key


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


def _parse_gradle(text: str, rel: str, module: _Module, gradle_path: str) -> None:
    module.names["gradle"] = gradle_path
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


# (direction, compiled pattern); group 1 is the topic list/literal.
_KAFKA_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (direction, re.compile(pattern, re.DOTALL)) for direction, pattern in (
        # Spring / Java / Kotlin: see _jvm_hits (resolves placeholders and constants)
        # Python (kafka-python, confluent-kafka, aiokafka)
        ("CONSUMES", r"(?:KafkaConsumer|AIOKafkaConsumer)\s*\(\s*((?:[\"'][^\"']+[\"']\s*,\s*)*[\"'][^\"']+[\"'])"),
        ("CONSUMES", r"\.subscribe\s*\(\s*(\[[^\]]*\])"),
        ("PRODUCES", r"\b(?:producer|kafka_producer)\s*\.\s*(?:send|produce|send_and_wait)\s*\(\s*([\"'][^\"']+[\"'])"),
        # kafkajs
        ("CONSUMES", r"\.subscribe\s*\(\s*\{[^}]*?\btopics?\s*:\s*(\[[^\]]*\]|[\"'][^\"']+[\"'])"),
        ("PRODUCES", r"\.send\s*\(\s*\{[^}]*?\btopic\s*:\s*([\"'][^\"']+[\"'])"),
        # Go (segmentio/kafka-go, sarama)
        ("CONSUMES", r"kafka\.(?:NewReader\s*\(\s*kafka\.)?ReaderConfig\s*\{[^}]*?Topic\s*:\s*(\"[^\"]+\")"),
        ("PRODUCES", r"kafka\.Writer\s*\{[^}]*?Topic\s*:\s*(\"[^\"]+\")"),
        ("CONSUMES", r"ConsumePartition\s*\(\s*(\"[^\"]+\")"),
        ("PRODUCES", r"ProducerMessage\s*\{[^}]*?Topic\s*:\s*(\"[^\"]+\")"),
        # Spring Cloud Stream bindings (properties)
        ("CONSUMES", r"spring\.cloud\.stream\.bindings\.[A-Za-z0-9_.-]*-in-\d+\.destination\s*[=:]\s*([A-Za-z0-9._-]+)"),
        ("PRODUCES", r"spring\.cloud\.stream\.bindings\.[A-Za-z0-9_.-]*-out-\d+\.destination\s*[=:]\s*([A-Za-z0-9._-]+)"),
    )
)
_YAML_BINDING_KEY = re.compile(r"([ \t]*)([A-Za-z0-9_.-]+)-(in|out)-\d+[ \t]*:[ \t]*(?:#.*)?")
_YAML_DESTINATION = re.compile(r"destination[ \t]*:[ \t]*[\"']?([A-Za-z0-9._-]+)")
_TOPIC_NAME = re.compile(r"^[A-Za-z0-9._-]{1,249}$")


def _topics(literal: str) -> list[str]:
    names = re.findall(r"[\"']([^\"']+)[\"']", literal) or [literal.strip()]
    return [name for name in names if _TOPIC_NAME.match(name)]


def _kafka_usages(rel: str, text: str) -> Iterator[tuple[str, str, int]]:
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
                if dest and _TOPIC_NAME.match(dest.group(1)):
                    direction = "CONSUMES" if key.group(3) == "in" else "PRODUCES"
                    yield direction, dest.group(1), offset + 1
                    break
        return
    for direction, pattern in _KAFKA_PATTERNS:
        if suffix == ".properties" and "spring.cloud" not in pattern.pattern:
            continue
        for match in pattern.finditer(text):
            for topic in _topics(match.group(1)):
                yield direction, topic, _line(text, match.start(1))


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
_MAX_LINE = 2000  # longer lines are minified / generated; never worth parsing
_MAX_NESTING = 4  # ${a} -> ${b:${c}} -> ... levels followed
_RECEIVER_EXCLUDE = ("rabbit", "jms", "amqp", "rest", "mail", "redis", "jdbc")


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


@dataclass
class _ConstIndex:
    """String constants keyed by ``NAME`` and ``Class.NAME``."""

    entries: dict[str, list[_Const]] = field(default_factory=dict)

    def add(self, owner: str | None, name: str, value: str, rel: str, module: str) -> None:
        const = _Const(name, value, rel, module)
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


_ESCAPE = re.compile(r"\\(.)", re.DOTALL)  # one fixed-width token per match: linear


def _unescape(value: str) -> str:
    """Resolve ``\\$``, ``\\"`` and ``\\\\``; other escapes are left as written."""
    if "\\" not in value:
        return value
    return _ESCAPE.sub(lambda m: m.group(1) if m.group(1) in '\\"$' else m.group(0), value)


def _matching(text: str, open_index: int, limit: int = 8000) -> int:
    """Index of the bracket closing ``text[open_index]`` (string aware), or -1."""
    depth = 0
    quote = ""
    index = open_index
    stop = min(len(text), open_index + limit)
    while index < stop:
        char = text[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _split_top(text: str, sep: str, base: int = 0) -> list[tuple[int, str]]:
    """Split on ``sep`` outside strings and brackets; offsets are ``base``-relative."""
    pieces: list[tuple[int, str]] = []
    depth = 0
    quote = ""
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == sep and depth == 0:
            pieces.append((base + start, text[start:index]))
            start = index + 1
        index += 1
    pieces.append((base + start, text[start:]))
    return pieces


def _literal(expr: str) -> str | None:
    """The content of a plain ``"..."`` literal, else None."""
    if len(expr) < 2 or expr[0] != '"' or expr[-1] != '"':
        return None
    inner = expr[1:-1]
    if '"' in inner.replace("\\\\", "").replace('\\"', ""):
        return None
    return _unescape(inner)


def _collapse(text: str, limit: int = 200) -> str:
    return " ".join(text.split())[:limit]


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


@dataclass
class _Outcome:
    names: list[str]
    source: str
    profile: str | None
    unresolved: str | None
    expression: str


class _Resolver:
    def __init__(self, props: _PropIndex, consts: _ConstIndex) -> None:
        self.props = props
        self.consts = consts

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

    def _text(self, text: str, source: str | None, module: str) -> _Term:
        if "#{" in text:
            return _Term(None, unresolved="unresolved:" + _collapse(text))
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

    def term(self, expr: str, rel: str, module: str, fields: dict[str, str]) -> _Term | None:
        expr = expr.strip()
        literal = _literal(expr)
        if literal is not None:
            return self._text(literal, None, module)
        if not _IDENT_CHAIN.fullmatch(expr):
            return None  # method call, cast, ... : not understood
        bare = expr[5:] if expr.startswith("this.") else expr
        if "." not in bare and bare in fields:
            return self._text(fields[bare], None, module)
        const = self.consts.find(bare, rel, module)
        if const is not None:
            return self._text(const.value, f"constant:{const.name}", module)
        return _Term(None, unresolved=f"unresolved:{expr}")

    def outcome(self, expr: str, rel: str, module: str, fields: dict[str, str]) -> _Outcome | None:
        raw = _collapse(_literal(expr.strip()) or expr, 300)
        terms: list[_Term] = []
        for _offset, part in _split_top(expr, "+"):
            term = self.term(part, rel, module, fields)
            if term is None:
                return None
            terms.append(term)
        failed = next((term for term in terms if term.value is None), None)
        if failed is not None:
            return _Outcome([], "", None, failed.unresolved, raw)
        source = next((t.source for t in terms if t.source and t.source != "literal"), "literal")
        profile = next((t.profile for t in terms if t.profile), None)
        value = "".join(term.value or "" for term in terms)
        names = [name.strip() for name in value.split(",") if name.strip()]
        return _Outcome(names, source or "literal", profile, None, raw)


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
    direction: str
    topic: str
    line: int  # attestation line
    site_line: int  # line of the listener / send / ProducerRecord keyword
    attributes: dict[str, object]
    unresolved: bool = False


def _is_kafka_receiver(name: str) -> bool:
    low = name.lower()
    if "kafka" in low:
        return True
    return low.endswith("template") and not any(word in low for word in _RECEIVER_EXCLUDE)


def _line_starts(text: str) -> list[int]:
    starts = [0]
    index = text.find("\n")
    while index >= 0:
        starts.append(index + 1)
        index = text.find("\n", index + 1)
    return starts


def _topic_hits(
    outcome: _Outcome, direction: str, line: int, site_line: int, *, pattern: bool = False,
) -> list[_Hit]:
    if outcome.unresolved is not None:
        return [_Hit(direction, outcome.unresolved, line, site_line,
                     {"unresolved": True, "expression": outcome.expression}, unresolved=True)]
    hits: list[_Hit] = []
    for name in outcome.names:
        attributes: dict[str, object] = {"resolved_from": outcome.source}
        if outcome.profile:
            attributes["profile"] = outcome.profile
        if _TOPIC_NAME.match(name):
            hits.append(_Hit(direction, name, line, site_line, attributes))
        elif pattern:
            attributes["pattern"] = name[:240]
            hits.append(_Hit(direction, "pattern:" + name[:240], line, site_line, attributes))
        else:
            hits.append(_Hit(direction, "unresolved:" + _collapse(name), line, site_line, {
                "unresolved": True, "expression": outcome.expression}, unresolved=True))
    return hits


def _jvm_hits(
    rel: str, text: str, module: str, resolver: _Resolver,
) -> tuple[list[_Hit], list[tuple[int, list[_Hit]]]]:
    """Spring / Java / Kotlin Kafka sites: ``@KafkaListener(topics|topicPattern)``,
    ``<kafka template>.send(<expr>, ...)`` and ``ProducerRecord(<expr>, ...)``.

    Returns every hit and, per call site (one per annotation / send / constructor
    occurrence), ``(line, hits of that site)``; a site with no hits was not understood."""
    hits: list[_Hit] = []
    sites: list[tuple[int, list[_Hit]]] = []
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

    position = 0
    while True:  # @KafkaListener(...)
        at = text.find("@KafkaListener", position)
        if at < 0:
            break
        position = at + 14
        site_hits: list[_Hit] = []
        sites.append((line_at(at), site_hits))
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
            value = value.strip()
            if value.startswith(("arrayOf(", "listOf(")) and value.endswith(")"):
                value_offset += value.index("(") + 1
                value = value[value.index("(") + 1:-1]
            elif value[:1] in ("{", "[") and value[-1:] in ("}", "]"):
                value_offset += 1
                value = value[1:-1]
            for item_offset, item in _split_top(value, ",", value_offset):
                if item.strip():
                    add(item, "CONSUMES", item_offset + len(item) - len(item.lstrip()),
                        line_at(at), pattern=name == "topicPattern", into=site_hits)

    position = 0
    while True:  # <receiver>.send(<expr>, ...)
        at = text.find(".send(", position)
        if at < 0:
            break
        position = at + 6
        begin = at
        while begin > 0 and (text[begin - 1].isalnum() or text[begin - 1] == "_"):
            begin -= 1
        if begin == at or not _is_kafka_receiver(text[begin:at]):
            continue
        end = _matching(text, at + 5)
        if end < 0:
            continue
        first_offset, first = _split_top(text[at + 6:end], ",", at + 6)[0]
        if first.strip() and "ProducerRecord" not in first:  # counted as a ProducerRecord site
            sites.append((line_at(at), add(
                first, "PRODUCES", first_offset + len(first) - len(first.lstrip()), line_at(at))))

    position = 0
    while True:  # ProducerRecord<...>(<expr>, ...)
        at = text.find("ProducerRecord", position)
        if at < 0:
            break
        position = at + 14
        if at > 0 and (text[at - 1].isalnum() or text[at - 1] == "_"):
            continue
        index = position
        while index < len(text) and text[index] in " \t":
            index += 1
        if index < len(text) and text[index] == "<":
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
        site_hits = []
        sites.append((line_at(at), site_hits))
        if first.strip():
            add(first, "PRODUCES", first_offset + len(first) - len(first.lstrip()),
                line_at(at), into=site_hits)
    return hits, sites


def _kafka_like(line: str) -> bool:
    """Cheap substring test: does this line look like a Kafka produce/consume site?"""
    text = line.strip()
    if not text or len(text) > _MAX_LINE or text.startswith(
            ("import ", "package ", "from ", "//", "/*", "*", "#")):
        return False
    if "@KafkaListener" in text or "producer.send" in text or "producer.produce" in text:
        return True
    if ".send(" in text and ("emplate" in text or "afka" in text):
        return True
    if ".subscribe(" in text:
        return True
    if "ProducerRecord" in text:
        tail = text.split("ProducerRecord", 1)[1]
        return tail.lstrip().startswith("(") or (tail.startswith("<") and ">(" in tail)
    return ("KafkaConsumer(" in text or "AIOKafkaConsumer(" in text) and "new " not in text


def _source_kind(hits: list[_Hit]) -> str:
    kinds = {("property" if str(h.attributes.get("resolved_from", "")).startswith(
        ("property:", "default:")) else "constant" if str(h.attributes.get(
            "resolved_from", "")).startswith("constant:") else "literal") for h in hits}
    return "property" if "property" in kinds else "constant" if "constant" in kinds else "literal"


def _line_coverage(
    rel: str, text: str, hits: list[_Hit],
) -> tuple[list[tuple[int, str]], dict[int, str]]:
    """Kafka-like lines of a file, and for each its best outcome: ``property`` /
    ``constant`` / ``literal`` (matched), ``unresolved``, or absent (unmatched). A hit
    belongs to the nearest kafka-like line at or above its site line (within 8 lines)."""
    like = [(number, line.strip()) for number, line in enumerate(text.split("\n"), 1)
            if _kafka_like(line)]
    numbers = [number for number, _line_text in like]
    status: dict[int, str] = {}
    rank = {"unresolved": 1, "literal": 2, "constant": 3, "property": 4}
    for hit in hits:
        index = bisect_right(numbers, hit.site_line) - 1
        if index < 0 or hit.site_line - numbers[index] > 8:
            continue
        if hit.unresolved:
            outcome = "unresolved"
        else:
            source = str(hit.attributes.get("resolved_from", "literal"))
            outcome = ("property" if source.startswith(("property:", "default:"))
                       else "constant" if source.startswith("constant:") else "literal")
        line = numbers[index]
        if rank[outcome] > rank.get(status.get(line, ""), 0):
            status[line] = outcome
    return like, status


# -- scan --------------------------------------------------------------------------------


def scan_repository(
    root: str | Path,
    *,
    pipeline: str | None = None,
    source: str | None = None,
    repo_name: str | None = None,
    environment: str = "prod",
    kafka_cluster: str = "main",
    schema_version: int = 2,
    heuristics: HeuristicMode = "candidate",
    include_tests: bool = False,
) -> ScanResult:
    """Scan ``root`` and return a seed manifest plus a summary of what was found."""
    base = Path(root).resolve()
    if not base.is_dir():
        raise OntologyError("invalid", f"{root} is not a directory")
    if schema_version not in (1, 2):
        raise OntologyError("invalid", "schema_version must be 1 or 2")
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
        module = modules.get(directory) or _Module(path=directory)
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
        if module.names:
            module.build_files.append(rel)
            modules[directory] = module

    # 2. Services from container / deployment files.
    for rel in files:
        name = PurePosixPath(rel).name
        if not (name == "Chart.yaml" or "Dockerfile" in name or name.endswith((".yaml", ".yml"))):
            continue
        owner = _owning_module(rel, modules)
        if owner is None or _is_test_path(rel):
            continue
        text = _read(base, rel) or ""
        found = _service_name(rel, text)
        if found is None:
            continue
        service_name, line = found
        rank = 3 if name == "Chart.yaml" and service_name else 2 if service_name else 1
        if rank > owner.service_rank:
            owner.service_rank = rank
            owner.service = service_name or PurePosixPath(owner.path).name or repo
            owner.service_evidence = (rel, line)

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
               heuristic: bool = False, candidate_only: bool = False) -> None:
        """``candidate_only`` edges (unresolved topics) stay candidates even with
        ``heuristics="verified"``."""
        if heuristic and heuristics == "off":
            return
        verified = not heuristic or (heuristics == "verified" and not candidate_only)
        relations.setdefault((src, relation_type, dst), {
            "src": src, "type": relation_type, "dst": dst,
            "status": "verified" if verified else "candidate",
            "confidence": (UNRESOLVED_CONFIDENCE if candidate_only
                           else HEURISTIC_CONFIDENCE if heuristic else STRUCTURAL_CONFIDENCE),
            "attestation": f"{evidence[0]}#L{evidence[1]}",
        })

    v2 = schema_version == 2
    repo_ref = entity("Repo", repo, attributes={"name": repo}) if v2 else None
    module_ref: dict[str, str] = {}
    service_ref: dict[str, str] = {}
    by_identity: dict[tuple[str, str], _Module] = {}
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
                   (module.build_files[0], 1))
        if module.service:
            service_ref[module.path] = entity(
                "Service", module.service,
                attributes={"name": module.service, "environment": environment,
                            "module": module.path},
            )
            if v2:
                relate(module_ref[module.path], "BUILDS", service_ref[module.path],
                       module.service_evidence or (module.build_files[0], 1))

    # Module dependencies inside the repo.
    for module in modules.values():
        for ecosystem, name, rel, line in module.deps:
            target = by_identity.get((ecosystem, name))
            if ecosystem == "maven" and target is None and ":" in name:
                target = by_identity.get(("maven", name.split(":", 1)[1]))
            if target is None or target is module:
                continue
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

    # Kafka usage (heuristic). Pass 1 reads every source file once and builds the
    # property / constant indexes; pass 2 resolves the Kafka sites of the files kept.
    kafka: dict[str, object] = {
        "files_examined": 0, "kafka_like_sites": 0, "matched_sites": 0,
        "resolved_via_property": 0, "resolved_via_constant": 0, "unresolved_placeholders": 0,
        "unmatched_sites": 0, "skipped_no_owner": 0, "unmatched_examples": [],
    }
    skipped_sites = 0
    if heuristics != "off":
        props = _PropIndex()
        consts = _ConstIndex()
        cache: dict[str, str] = {}
        for rel in files:
            suffix = PurePosixPath(rel).suffix
            if suffix not in SOURCE_SUFFIXES or (not include_tests and _is_test_path(rel)):
                continue
            text = _read(base, rel)
            if not text:
                continue
            kafka["files_examined"] += 1  # type: ignore[operator]
            home = _owning_module(rel, modules)
            module_path = home.path if home else ""
            config_name = _CONFIG_FILE.fullmatch(PurePosixPath(rel).name)
            if config_name:
                parse = _parse_properties if suffix == ".properties" else _parse_yaml_props
                props.add(module_path, config_name.group(2), rel, parse(text))
            elif suffix in _JVM_SUFFIXES:
                for owner, name, value in _extract_constants(text, suffix):
                    consts.add(owner, name, value, rel, module_path)
            if "afka" in text or "AFKA" in text or "destination" in text:
                cache[rel] = text
        resolver = _Resolver(props, consts)
        examples: list[str] = kafka["unmatched_examples"]  # type: ignore[assignment]
        for rel, text in cache.items():
            home = _owning_module(rel, modules)
            producer = None
            if home is not None:
                producer = service_ref.get(home.path) or (module_ref.get(home.path) if v2 else None)
            legacy = [_Hit(direction, topic, line, line, {"resolved_from": "literal"})
                      for direction, topic, line in _kafka_usages(rel, text)]
            jvm: list[tuple[int, list[_Hit]]] = []
            jvm_hits: list[_Hit] = []
            if PurePosixPath(rel).suffix in _JVM_SUFFIXES:
                jvm_hits, jvm = _jvm_hits(rel, text, home.path if home else "", resolver)
            hits = legacy + jvm_hits
            lines = text.split("\n")
            # (line, resolved source or None, unresolved topic count) per site
            results: list[tuple[int, str | None, int]] = []
            for number, site_hits in jvm:
                good = [h for h in site_hits if not h.unresolved]
                results.append((number, _source_kind(good) if good else None,
                                len(site_hits) - len(good)))
            site_lines = {number for number, _hits in jvm}
            like, status = _line_coverage(rel, text, legacy)
            for number, _snippet in like:  # lines no JVM site accounts for (other clients)
                if number in site_lines:
                    continue
                outcome = status.get(number)
                results.append((number, None if outcome in (None, "unresolved") else outcome,
                                1 if outcome == "unresolved" else 0))
            kafka["kafka_like_sites"] += len(results)  # type: ignore[operator]
            if producer is None:
                if results:
                    kafka["skipped_no_owner"] += 1  # type: ignore[operator]
                    kafka["unmatched_sites"] += len(results)  # type: ignore[operator]
                    skipped_sites += len(results)
                    examples.extend(f"{rel}:{n}: {lines[n - 1].strip()[:120]}"
                                    for n, _src, _unres in sorted(results))
                continue
            for number, via, unresolved in sorted(results, key=lambda item: item[0]):
                kafka["unresolved_placeholders"] += unresolved  # type: ignore[operator]
                if via is not None:
                    kafka["matched_sites"] += 1  # type: ignore[operator]
                    if via == "property":
                        kafka["resolved_via_property"] += 1  # type: ignore[operator]
                    elif via == "constant":
                        kafka["resolved_via_constant"] += 1  # type: ignore[operator]
                elif not unresolved:
                    kafka["unmatched_sites"] += 1  # type: ignore[operator]
                    examples.append(f"{rel}:{number}: {lines[number - 1].strip()[:120]}")
            for hit in hits:
                attributes = {"name": hit.topic, "cluster": kafka_cluster, **hit.attributes}
                topic_ref = entity("KafkaTopic", hit.topic, attributes=attributes)
                relate(producer, hit.direction, topic_ref, (rel, hit.line), heuristic=True,
                       candidate_only=hit.unresolved)
        del examples[5:]

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
        declared = {(item.type, Entity.canonicalize(item.key))
                    for _path, extra in loaded for item in extra.entities}
        scanned = manifest.model_copy(update={"entities": tuple(
            item for item in manifest.entities
            if (item.type, Entity.canonicalize(item.key)) not in declared
        )})
        manifest = merge_manifests(
            [("scan", scanned), *loaded], source=manifest.source, pipeline=pipeline,
        ).manifest

    by_type: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for relation in manifest.relations:
        by_type[relation.type] = by_type.get(relation.type, 0) + 1
        statuses[relation.status] = statuses.get(relation.status, 0) + 1
    summary = {
        "root": str(base), "files": len(files), "modules": len(modules),
        "services": len(service_ref), "teams": teams,
        "topics": sum(1 for item in manifest.entities if item.type == "KafkaTopic"),
        "entities": len(manifest.entities), "relations": by_type, "statuses": statuses,
        "codeowners": codeowners_file, "hand_manifests": hand, "warnings": warnings,
        "schema_version": schema_version, "kafka": kafka,
    }
    warnings.extend(_kafka_warnings(kafka, int(summary["topics"]), skipped_sites))  # type: ignore[call-overload]
    return ScanResult(manifest=manifest, summary=summary)


def _kafka_warnings(kafka: dict[str, object], topics: int, skipped_sites: int) -> list[str]:
    like = int(kafka["kafka_like_sites"])  # type: ignore[call-overload]
    unmatched = int(kafka["unmatched_sites"])  # type: ignore[call-overload]
    skipped = int(kafka["skipped_no_owner"])  # type: ignore[call-overload]
    unresolved = int(kafka["unresolved_placeholders"])  # type: ignore[call-overload]
    messages: list[str] = []
    if like and (topics == 0 or unmatched * 2 > like):
        if topics == 0:
            head = f"found {like} Kafka-like site(s) but extracted 0 topics"
        else:
            head = f"{unmatched} of {like} Kafka-like site(s) were not recognised"
        if skipped:
            cause = (f"{skipped} file(s) with Kafka usage ({skipped_sites} site(s)) were skipped "
                     "because no module (or, with schema v1, no service) owns them")
            if skipped_sites == like:
                cause += "; add a build file / Dockerfile above them"
        else:
            cause = ("no site matched a supported pattern (topic built by a method call or "
                     "expression, or an unsupported client)")
        examples = kafka["unmatched_examples"]
        messages.append(f"{head}: {cause}. Examples: {'; '.join(examples[:3])}"  # type: ignore[index]
                        if examples else f"{head}: {cause}.")
    if unresolved:
        messages.append(
            f"{unresolved} Kafka site(s) use a topic that could not be resolved "
            "(unknown property, SpEL or local variable); they are recorded as candidate "
            "'unresolved:*' topics",
        )
    return messages


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    return slug or "repo"


__all__ = ["HAND_MANIFEST_NAMES", "ScanResult", "scan_repository"]
