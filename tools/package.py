#!/usr/bin/env python3
"""Validate and create an exclusive, byte-reproducible DevFlow source archive.

This is a local static validator, not a substitute for `claude plugin validate`.
The archive uses uncompressed entries with fixed metadata, so its bytes do not
depend on the host's zlib release, source mtimes, or executable permission bits.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ("devflow",)
PACKAGE = "plugins/devflow/scripts/devflow_core"
WORKFLOW_RESOURCES = ("defaults.json", "reviewer.md", "review.schema.json")
COORDINATORS = ("router", "cli", "workflows")
SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv", "node_modules", "dist", ".devflow"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".zip", ".log", ".tmp"}
MAX_BYTES = 64 * 1024 * 1024


class PackagingError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise PackagingError(message)


def _no_link(path: Path):
    info = path.lstat()
    _require(not stat.S_ISLNK(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400), f"Refusing symlink/reparse package input: {path}")
    return info


def _json(data: bytes, label: str):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, f"Duplicate JSON key in {label}: {key}")
            result[key] = value
        return result
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=unique,
                          parse_constant=lambda x: (_ for _ in ()).throw(PackagingError(f"Invalid constant in {label}: {x}")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PackagingError(f"Invalid JSON in {label}: {exc}") from exc


def _collect(root: Path) -> dict[str, bytes]:
    _no_link(root)
    _require(root.is_dir(), f"Not a package directory: {root}")
    result = {}
    case_names = {}
    total = 0
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory = Path(directory)
        kept = []
        for name in sorted(dirnames):
            if name in SKIP_DIRS:
                continue
            path = directory / name
            _no_link(path)
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            path = directory / name
            relative = path.relative_to(root).as_posix()
            if path.suffix.lower() in SKIP_SUFFIXES or relative == "SHA256SUMS":
                continue
            if name == ".DS_Store" or name.casefold() == "thumbs.db":
                continue
            _require(not name.startswith(".env") and path.suffix.lower() not in {".pem", ".key", ".p12", ".pfx"}, f"Potential secret must not be included in distribution: {relative}")
            before = _no_link(path)
            _require(stat.S_ISREG(before.st_mode), f"Not a regular package file: {relative}")
            _require(before.st_size <= MAX_BYTES - total, "Package inputs exceed the 64 MiB size limit")
            folded = relative.casefold()
            _require(folded not in case_names, f"Case-colliding package paths: {case_names.get(folded)} / {relative}")
            case_names[folded] = relative
            data = path.read_bytes()
            after = _no_link(path)
            _require((before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns), f"Package input changed during read: {relative}")
            _require(len(data) == before.st_size, f"Package input changed size: {relative}")
            total += len(data)
            result[relative] = data
    return dict(sorted(result.items()))


def _validate_files(files: dict[str, bytes]) -> dict:
    def document(name):
        _require(name in files, f"Missing package file: {name}")
        value = _json(files[name], name)
        _require(isinstance(value, dict), f"Expected JSON object: {name}")
        return value

    marketplace = document(".claude-plugin/marketplace.json")
    _require(marketplace.get("name") == "devflow-marketplace", "Marketplace name must be devflow-marketplace")
    _require(isinstance(marketplace.get("owner"), dict) and marketplace["owner"].get("name"), "Marketplace owner.name is required")
    entries = marketplace.get("plugins")
    _require(isinstance(entries, list) and len(entries) == 1, "Marketplace must list exactly the single devflow plugin")
    names = []
    manifests = {}
    for entry in entries:
        _require(isinstance(entry, dict) and entry.get("name") in PLUGINS, "Unknown marketplace plugin entry")
        name = entry["name"]
        _require(name not in names, f"Duplicate plugin entry: {name}")
        names.append(name)
        _require(entry.get("source") == f"./plugins/{name}", f"Wrong source path for {name}; paths start at marketplace root")
        manifest = document(f"plugins/{name}/.claude-plugin/plugin.json")
        _require(manifest.get("name") == name, f"Plugin/marketplace name mismatch: {name}")
        _require(isinstance(manifest.get("version"), str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", manifest["version"]), f"A semantic version is required for {name}")
        manifests[name] = manifest
    _require(not manifests["devflow"].get("dependencies"), "Unified devflow must not install sibling plugins")
    _require(all(path.startswith("plugins/devflow/") for path in files if path.startswith("plugins/")),
             "Only the single devflow plugin may be distributed")
    plugin = "plugins/devflow"
    runner = plugin + "/scripts/devflow.py"
    _require(runner in files, "Missing unified DevFlow runner")
    metadata = files[runner].decode("utf-8")
    _require('# /// script' in metadata and 'requires-python = ">=3.11"' in metadata,
             "Runner needs a Python minimum, not an exact interpreter pin")
    _require('dependencies = []' in metadata, "Runner must not download Python packages")
    hooks = document(plugin + "/hooks/hooks.json").get("hooks")
    _require(isinstance(hooks, dict), "Missing unified hook map")
    # Hooks only record and add context: they never block, and the tool hooks
    # run only for AskUserQuestion (so other tool calls start no process).
    _require(set(hooks) == {"UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"},
             "Hooks must be exactly UserPromptSubmit, PreToolUse, PostToolUse and Stop")
    for event, matchers in hooks.items():
        _require(isinstance(matchers, list) and matchers, f"Invalid hook event {event}")
        for matcher in matchers:
            _require(isinstance(matcher, dict) and isinstance(matcher.get("hooks"), list), "Invalid hook matcher")
            if event in {"PreToolUse", "PostToolUse"}:
                _require(matcher.get("matcher") == "AskUserQuestion", f"{event} hook must match only AskUserQuestion")
            for hook in matcher["hooks"]:
                _require(isinstance(hook, dict) and hook.get("type") == "command" and hook.get("command") == "uv", "Unexpected unified hook command")
                expected = ["run", "--offline", "--no-project", "--no-config", "--no-python-downloads", "${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py", "hook"]
                _require(hook.get("args") == expected, f"Hook must use exact portable exec argv: {event}")
                _require(hook.get("onFailure") != "block", f"Hook must never block on failure: {event}")
    skill_prefix = plugin + "/skills/"
    skills = [path for path in files if path.startswith(skill_prefix) and path.endswith("/SKILL.md")]
    for required in ("plan", "implement", "resume", "status"):
        _require(skill_prefix + required + "/SKILL.md" in files, f"Missing root skill: {required}")
    for path in skills:
        text = files[path].decode("utf-8")
        _require(text.startswith("---\n") and "\n---" in text[4:], f"Missing skill frontmatter: {path}")
        frontmatter = text[4:text.index("\n---", 4)]
        identifier = Path(path).parent.name
        _require(re.search(rf"(?m)^name:\s*{re.escape(identifier)}\s*$", frontmatter), f"Skill name/directory mismatch: {path}")
        _require(re.search(r"(?m)^description:\s*\S", frontmatter), f"Skill description missing: {path}")
        _require(re.search(r"(?m)^disable-model-invocation:\s*true\s*$", frontmatter), f"Skill must require manual invocation: {path}")
        _require("--python 3.11" not in text, f"Obsolete exact Python pin in {path}")
        _require("${CLAUDE_PLUGIN_ROOT}/scripts/devflow.py" in text, f"Skill bypasses unified runner: {path}")
    _validate_package(files)
    for path, data in files.items():
        if path.endswith(".py"):
            ast.parse(data.decode("utf-8"), filename=path, feature_version=(3, 11))
        elif path.endswith(".json"):
            _json(data, path)
    return {"marketplace": marketplace["name"], "version": manifests["devflow"]["version"], "plugins": {name: manifests[name]["version"] for name in PLUGINS}, "source_files": len(files), "source_bytes": sum(map(len, files.values()))}


def _module_name(path: str) -> str:
    relative = path.removeprefix(PACKAGE.rsplit("/", 1)[0] + "/").removesuffix(".py").split("/")
    return ".".join(relative[:-1] if relative[-1] == "__init__" else relative)


def _imports(path: str, data: bytes) -> set[str]:
    """Absolute module names imported by one package source file."""
    module = _module_name(path)
    package = module if path.endswith("/__init__.py") else module.rsplit(".", 1)[0]
    result = set()
    tree = ast.parse(data.decode("utf-8"), filename=path, feature_version=(3, 11))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                _require(node.level <= len(base), f"Relative import escapes the package: {path}")
                base = base[:len(base) - node.level + 1]
                prefix = ".".join(base + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            result.add(prefix)
            result.update(prefix + "." + alias.name for alias in node.names)
    return result


def _validate_package(files: dict[str, bytes]) -> list[str]:
    """Each workflow ships its resources; imports only flow toward shared code."""
    root = PACKAGE.rsplit("/", 1)[1]
    sources = {path: data for path, data in files.items() if path.startswith(PACKAGE + "/") and path.endswith(".py")}
    workflows = sorted({path.split("/")[4] for path in sources if path.count("/") == 5 and path.endswith("/__init__.py")} - {"common"})
    _require(workflows, "No workflow packages found")
    for name in workflows:
        for resource in WORKFLOW_RESOURCES:
            _require(f"{PACKAGE}/{name}/{resource}" in files, f"Missing {name}/{resource}")
    def area(module: str) -> str | None:
        parts = module.split(".")
        return parts[1] if len(parts) > 1 and parts[0] == root else None
    registry = files.get(f"{PACKAGE}/workflows.py")
    _require(registry is not None, "Missing workflow registry")
    registered = {node.module for node in ast.walk(ast.parse(registry.decode("utf-8")))
                  if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module}
    _require(registered == set(workflows), f"Workflow registry {sorted(registered)} does not match packages {workflows}")
    for path, data in sources.items():
        own = area(_module_name(path))
        for imported in _imports(path, data):
            target = area(imported)
            if target is None:
                continue
            if own in workflows:
                _require(target not in workflows or target == own, f"Workflow imports another workflow: {path} -> {imported}")
                _require(target not in COORDINATORS, f"Workflow imports a coordinator: {path} -> {imported}")
            elif own == "common":
                _require(target not in workflows and target not in COORDINATORS, f"Shared code depends on a workflow or coordinator: {path} -> {imported}")
            elif own != "workflows":
                _require(target not in workflows, f"Only the workflow registry may import a workflow: {path} -> {imported}")
    return workflows


def validate(root: Path = ROOT) -> dict:
    return _validate_files(_collect(Path(root)))


def build(root: Path = ROOT, output: Path | None = None) -> Path:
    root = Path(root)
    files = _collect(root)
    report = _validate_files(files)
    output = Path(output) if output is not None else root / "dist" / f"devflow-{report['version']}.zip"
    _require(output.suffix.lower() == ".zip", "Package output must have a .zip extension")
    for parent in [output, *output.parents]:
        if parent.exists() or parent.is_symlink():
            _no_link(parent)
    _require(not output.exists(), f"Output already exists and will not be overwritten: {output}")
    sums = "".join(f"{hashlib.sha256(data).hexdigest()}  {path}\n" for path, data in files.items()).encode("utf-8")
    entries = {**files, "SHA256SUMS": sums}
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
        for relative, data in sorted(entries.items()):
            info = zipfile.ZipInfo("devflow/" + relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, data)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as destination:
        destination.write(memory.getvalue())
        destination.flush()
        os.fsync(destination.fileno())
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-only", action="store_true", help="Validate source without creating an archive")
    parser.add_argument("--output", type=Path, help="New .zip path; existing output is never overwritten")
    args = parser.parse_args(argv)
    try:
        if args.validate_only:
            _require(args.output is None, "--output cannot be used with --validate-only")
            print(json.dumps(validate(), ensure_ascii=False, indent=2))
        else:
            output = build(output=args.output)
            print(json.dumps({"path": str(output.resolve()), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "bytes": output.stat().st_size}, indent=2))
        return 0
    except (OSError, ValueError, SyntaxError, UnicodeError) as exc:
        print("Package validation failed: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
