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
Cloud Stream bindings           (literal topic names only)                  (heuristic)
``ncp-ontology.{toml,json,      hand-written facts, merged in; they win     as written
yaml,yml}`` in any module       over scanned entities with the same key
==============================  ==========================================  ==========

``schema_version=2`` (default) needs ``infrastructure`` v2 (``Repo``,
``Module``). ``schema_version=1`` drops modules and repos, lifts module
dependencies to ``Service DEPENDS_ON Service`` where both modules build a
service, and keeps only service-level ownership and Kafka edges.

The scan is static and approximate: it sees literal names only, follows no
code paths, and CODEOWNERS matching is a gitignore-style approximation.
Review the manifest (or seed with ``--status candidate``) before trusting it.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
import xml.etree.ElementTree as ET
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
        # Spring / Java / Kotlin
        ("CONSUMES", r"@KafkaListener\s*\([^)]*?topics\s*=\s*(\{[^}]*\}|\[[^\]]*\]|\"[^\"]+\")"),
        ("PRODUCES", r"(?:kafkaTemplate|KafkaTemplate|template)\s*\.\s*send\s*\(\s*(\"[^\"]+\")"),
        ("PRODUCES", r"new\s+ProducerRecord\s*(?:<[^>]*>)?\s*\(\s*(\"[^\"]+\")"),
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
               heuristic: bool = False) -> None:
        if heuristic and heuristics == "off":
            return
        status = "verified" if not heuristic or heuristics == "verified" else "candidate"
        relations.setdefault((src, relation_type, dst), {
            "src": src, "type": relation_type, "dst": dst, "status": status,
            "confidence": HEURISTIC_CONFIDENCE if heuristic else STRUCTURAL_CONFIDENCE,
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

    # Kafka usage (heuristic).
    if heuristics != "off":
        for rel in files:
            if PurePosixPath(rel).suffix not in SOURCE_SUFFIXES:
                continue
            if not include_tests and _is_test_path(rel):
                continue
            home = _owning_module(rel, modules)
            if home is None:
                continue
            producer = service_ref.get(home.path) or (module_ref.get(home.path) if v2 else None)
            if producer is None:
                continue
            text = _read(base, rel)
            if not text or ("kafka" not in text.lower() and "destination" not in text):
                continue
            for direction, topic, line in _kafka_usages(rel, text):
                topic_ref = entity("KafkaTopic", topic,
                                   attributes={"name": topic, "cluster": kafka_cluster})
                relate(producer, direction, topic_ref, (rel, line), heuristic=True)

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
        "schema_version": schema_version,
    }
    return ScanResult(manifest=manifest, summary=summary)


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    return slug or "repo"


__all__ = ["HAND_MANIFEST_NAMES", "ScanResult", "scan_repository"]
