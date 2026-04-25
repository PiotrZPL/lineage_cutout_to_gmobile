#!/usr/bin/env python3
"""
main.py

Generate a gmobile display-panel JSON file from LineageOS Android device overlays.

Typical usage:

    python3 main.py xiaomi sweet \
        --branch lineage-23.2 \
        --workdir ./lineage-device-work \
        --output-dir ./display-panels

For Xiaomi Redmi Note 10 Pro (sweet), this produces:

{
  "name": "Xiaomi Redmi Note 10 Pro/Pro Max",
  "x-res": 1080,
  "y-res": 2400,
  "corner-radii": [90, 90, 90, 90],
  "cutouts": [
    {
      "name": "front-camera",
      "path": "M 515.5,51.75 a 24.5,24.5,0,1,0,49,0 a 24.5,24.5,0,1,0,-49,0 Z"
    }
  ]
}

The generated JSON is a starting point. Always validate visually on the device or
with gmobile/phoc debug tooling before upstreaming.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional


# Android framework resource names commonly used for display cutouts/corners.
CUTOUT_RESOURCE = "config_mainBuiltInDisplayCutout"
CUTOUT_RECT_RESOURCE = "config_mainBuiltInDisplayCutoutRectApproximation"
WIKI_REPO_URL = "https://github.com/LineageOS/lineage_wiki.git"

CORNER_TOP_NAMES = (
    "rounded_corner_radius_top",
    "config_roundedCornerTopRadius",
    "config_roundedCornerRadiusTop",
)

CORNER_BOTTOM_NAMES = (
    "rounded_corner_radius_bottom",
    "config_roundedCornerBottomRadius",
    "config_roundedCornerRadiusBottom",
)

CORNER_DEFAULT_NAMES = (
    "rounded_corner_radius",
    "config_roundedCornerRadius",
)

STATUS_BAR_NAMES = (
    "status_bar_height_default",
    "status_bar_height_portrait",
)

DENSITY_PATTERNS = (
    re.compile(r"^\s*TARGET_SCREEN_DENSITY\s*[:?+]?=\s*(\d+)\s*$", re.MULTILINE),
    re.compile(r"\bro\.sf\.lcd_density\s*[:?+]?=\s*(\d+)\b"),
    re.compile(r"\bqemu\.sf\.lcd_density\s*[:?+]?=\s*(\d+)\b"),
)

SCREEN_SIZE_VAR_RE = re.compile(
    r"^\s*(?P<name>TARGET_SCREEN_(?P<axis>WIDTH|HEIGHT))\s*[:?+]?=\s*(?P<value>\d+)\s*$",
    re.MULTILINE,
)

TOUCH_PANEL_MAX_RE = re.compile(
    r"^\s*(?P<prefix>[\w-]+),(?P<axis>panel-max-[xy])\s*=\s*<\s*(?P<value>\d+)\s*>\s*;",
    re.MULTILINE,
)

TOUCH_DISPLAY_COORDS_RE = re.compile(
    r"^\s*(?P<prefix>[\w-]+),display-coords\s*=\s*<\s*0\s+0\s+(?P<x>\d+)\s+(?P<y>\d+)\s*>\s*;",
    re.MULTILINE,
)

TOUCH_SUPER_RESOLUTION_RE = re.compile(
    r"^\s*(?P<prefix>[\w-]+),super-resolution-factors\s*=\s*<\s*(?P<factor>\d+)\s*>\s*;",
    re.MULTILINE,
)

WIKI_CODENAME_RE = re.compile(r"^\s*codename:\s*['\"]?(?P<codename>[^'\"\s#]+)['\"]?\s*$", re.MULTILINE)
WIKI_INLINE_SCREEN_RE = re.compile(
    r"^\s*screen:\s*\{[^\n}]*\bresolution:\s*['\"]?(?P<resolution>\d+\s*[x×]\s*\d+)['\"]?",
    re.IGNORECASE | re.MULTILINE,
)
WIKI_SCREEN_BLOCK_RE = re.compile(
    r"^\s*screen:\s*\n(?P<body>(?:\s+.*\n?)+)",
    re.MULTILINE,
)
WIKI_BLOCK_RESOLUTION_RE = re.compile(
    r"^\s+resolution:\s*['\"]?(?P<resolution>\d+\s*[x×]\s*\d+)['\"]?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

RESOLUTION_RE = re.compile(
    r"\b(?P<a>[4-9]\d{2,4}|[1-3]\d{3,4})\s*[x×]\s*(?P<b>[4-9]\d{2,4}|[1-3]\d{3,4})\b",
    re.IGNORECASE,
)

DISPLAY_LINE_RE = re.compile(r"\b(display|screen|panel|resolution)\b", re.IGNORECASE)
INCH_RE = re.compile(r"(?P<inches>\d+(?:\.\d+)?)\s*(?:inches|inch|in\.|\"|”)", re.IGNORECASE)


@dataclass(frozen=True)
class ResourceValue:
    name: str
    value: str
    source: Path


@dataclass(frozen=True)
class ResolutionCandidate:
    x: int
    y: int
    source: Path
    line: str
    score: int


@dataclass(frozen=True)
class InchCandidate:
    inches: float
    source: Path
    line: str
    score: int


@dataclass
class Findings:
    name: str
    x_res: int
    y_res: int
    density: Optional[int]
    cutout_path_android: Optional[ResourceValue]
    cutout_rect_android: Optional[ResourceValue]
    corner_top_px: Optional[float]
    corner_bottom_px: Optional[float]
    corner_default_px: Optional[float]
    status_bar_px: Optional[float]
    diagonal_inches: Optional[float]
    notes: list[str]


class ScriptError(RuntimeError):
    pass


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def run(
    cmd: list[str],
    *,
    cwd: Optional[Path] = None,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    if capture:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=check, text=True)


def ensure_git() -> None:
    if shutil.which("git") is None:
        raise ScriptError("git was not found in PATH")


def make_repo_url(org: str, oem: str, codename: str) -> str:
    return f"https://github.com/{org}/android_device_{oem}_{codename}.git"


def repo_name_from_url(url: str) -> str:
    name = url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name


def git_remote_heads(repo_url: str) -> list[str]:
    proc = run(["git", "ls-remote", "--heads", repo_url], capture=True)
    branches: list[str] = []
    for line in proc.stdout.splitlines():
        if "refs/heads/" in line:
            branches.append(line.rsplit("refs/heads/", 1)[1].strip())
    return branches


def git_default_branch(repo_url: str) -> Optional[str]:
    proc = run(["git", "ls-remote", "--symref", repo_url, "HEAD"], capture=True, check=False)
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        m = re.match(r"ref:\s+refs/heads/(\S+)\s+HEAD", line)
        if m:
            return m.group(1)
    return None


def git_current_branch(path: Path) -> Optional[str]:
    proc = run(["git", "branch", "--show-current"], cwd=path, capture=True, check=False)
    if proc.returncode != 0:
        return None

    branch = proc.stdout.strip()
    return branch or None


def lineage_branch_key(branch: str) -> tuple[int, int, str]:
    """
    Sort key for LineageOS branches. Larger Android versions first.
    Examples:
      lineage-23.2 -> (23, 2, branch)
      lineage-22.1 -> (22, 1, branch)
      lineage-21   -> (21, 0, branch)
    """
    m = re.match(r"lineage-(\d+)(?:\.(\d+))?$", branch)
    if not m:
        return (-1, -1, branch)
    return (int(m.group(1)), int(m.group(2) or 0), branch)


def choose_branch(repo_url: str, requested: Optional[str]) -> str:
    if requested:
        return requested

    heads = git_remote_heads(repo_url)
    lineage_heads = [b for b in heads if b.startswith("lineage-")]
    if lineage_heads:
        return sorted(lineage_heads, key=lineage_branch_key, reverse=True)[0]

    default = git_default_branch(repo_url)
    if default:
        return default

    for fallback in ("main", "master"):
        if fallback in heads:
            return fallback

    raise ScriptError(f"Could not determine a branch for {repo_url}")


def choose_branch_for_target(repo_url: str, requested: Optional[str], target: Path, *, update: bool) -> str:
    if requested:
        return requested

    if (target / ".git").exists() and not update:
        branch = git_current_branch(target)
        if branch:
            return branch

    return choose_branch(repo_url, None)


def clone_or_update_repo(
    repo_url: str,
    branch: str,
    target: Path,
    *,
    update: bool,
    depth: Optional[int],
) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)

    if (target / ".git").exists():
        if update:
            eprint(f"Updating existing checkout: {target}")
            run(["git", "fetch", "origin", branch], cwd=target)
            run(["git", "checkout", branch], cwd=target)
            run(["git", "pull", "--ff-only", "origin", branch], cwd=target)
        else:
            eprint(f"Using existing checkout: {target}")
        return target

    if target.exists() and any(target.iterdir()):
        raise ScriptError(f"Target directory exists and is not a git checkout: {target}")

    cmd = ["git", "clone", "--branch", branch]
    if depth and depth > 0:
        cmd += ["--depth", str(depth)]
    cmd += [repo_url, str(target)]

    eprint(f"Cloning {repo_url} ({branch}) -> {target}")
    run(cmd)
    return target


def clone_or_update_wiki(workdir: Path, *, update: bool, depth: Optional[int]) -> Path:
    target = workdir / "lineage_wiki"
    target.parent.mkdir(parents=True, exist_ok=True)

    if (target / ".git").exists():
        if update:
            eprint(f"Updating existing wiki checkout: {target}")
            run(["git", "fetch", "origin", "main"], cwd=target)
            run(["git", "checkout", "main"], cwd=target)
            run(["git", "pull", "--ff-only", "origin", "main"], cwd=target)
        else:
            eprint(f"Using existing wiki checkout: {target}")
        return target

    if target.exists() and any(target.iterdir()):
        raise ScriptError(f"Wiki target directory exists and is not a git checkout: {target}")

    cmd = ["git", "clone", "--sparse", "--filter=blob:none"]
    if depth and depth > 0:
        cmd += ["--depth", str(depth)]
    cmd += [WIKI_REPO_URL, str(target)]

    eprint(f"Cloning {WIKI_REPO_URL} -> {target}")
    run(cmd)
    run(["git", "sparse-checkout", "set", "_data/devices"], cwd=target)
    return target


def strip_json_comments(text: str) -> str:
    """
    Best-effort cleanup for dependency files that occasionally contain comments.
    This deliberately does not attempt to be a full JSON5 parser.
    """
    text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return text


def dependency_repo_url(dep: dict, default_org: str) -> Optional[str]:
    repo = dep.get("repository")
    remote = dep.get("remote")

    if not isinstance(repo, str) or not repo:
        return None

    if repo.startswith("http://") or repo.startswith("https://") or repo.startswith("git@"):
        return repo if repo.endswith(".git") else repo + ".git"

    if isinstance(remote, str) and remote:
        remote = remote.rstrip("/")
        if remote.startswith("http://") or remote.startswith("https://"):
            return f"{remote}/{repo}.git"
        if remote in ("github", "github.com"):
            return f"https://github.com/{default_org}/{repo}.git"
        if remote.lower() == "lineageos":
            return f"https://github.com/LineageOS/{repo}.git"

    return f"https://github.com/{default_org}/{repo}.git"


def clone_lineage_dependencies(
    root: Path,
    workdir: Path,
    default_org: str,
    *,
    update: bool,
    depth: Optional[int],
    max_deps: int,
) -> list[Path]:
    depfile = root / "lineage.dependencies"
    if not depfile.exists():
        return []

    try:
        deps = json.loads(strip_json_comments(depfile.read_text(encoding="utf-8")))
    except Exception as exc:
        eprint(f"Warning: could not parse {depfile}: {exc}")
        return []

    if not isinstance(deps, list):
        return []

    cloned: list[Path] = []
    dep_base = workdir / "_dependencies"
    for idx, dep in enumerate(deps[:max_deps]):
        if not isinstance(dep, dict):
            continue

        repo_url = dependency_repo_url(dep, default_org)
        if not repo_url:
            continue

        repo_name = repo_name_from_url(repo_url)
        target_path = dep.get("target_path")
        if isinstance(target_path, str) and target_path:
            local_name = target_path.strip("/").replace("/", "__")
        else:
            local_name = repo_name

        target = dep_base / local_name
        try:
            branch = dep.get("branch")
            requested_branch = branch if isinstance(branch, str) and branch else None
            branch = choose_branch_for_target(repo_url, requested_branch, target, update=update)
            clone_or_update_repo(repo_url, branch, target, update=update, depth=depth)
            cloned.append(target)
        except Exception as exc:
            eprint(f"Warning: failed to clone dependency #{idx + 1} {repo_url}: {exc}")

    return cloned


def interesting_text_files(root: Path) -> Iterator[Path]:
    wanted_names = {
        "README",
        "README.md",
        "README.txt",
        "BoardConfig.mk",
        "device.mk",
        "system.prop",
        "vendor.prop",
        "product.prop",
        "lineage.mk",
    }
    wanted_suffixes = {".mk", ".prop", ".txt", ".md", ".xml", ".bp", ".dts", ".dtsi"}

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if ".git" in path.parts:
            continue
        if path.name in wanted_names or path.suffix.lower() in wanted_suffixes:
            yield path


def read_text_lossy(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def score_resolution(path: Path, line: str) -> int:
    score = 0
    name = path.name.lower()
    parts = {p.lower() for p in path.parts}

    if name.startswith("readme"):
        score += 50
    if "overlay" in parts or "overlay-lineage" in parts or "rro_overlays" in parts:
        score += 20
    if DISPLAY_LINE_RE.search(line):
        score += 40
    if "pixels" in line.lower() or "px" in line.lower():
        score += 15
    if "display" in line.lower():
        score += 20
    if "camera" in line.lower() or "video" in line.lower():
        score -= 60
    if "photo" in line.lower() or "sensor" in line.lower():
        score -= 60
    return score


def make_resolution_candidate(
    x: int,
    y: int,
    *,
    source: Path,
    line: str,
    score: int,
    natural_landscape: bool,
) -> Optional[ResolutionCandidate]:
    if not plausible_resolution_pair(x, y):
        return None

    if natural_landscape:
        x, y = max(x, y), min(x, y)

    return ResolutionCandidate(x=x, y=y, source=source, line=line, score=score)


def find_makefile_screen_resolution(
    path: Path,
    text: str,
    *,
    natural_landscape: bool,
) -> Optional[ResolutionCandidate]:
    values: dict[str, tuple[int, str]] = {}
    for m in SCREEN_SIZE_VAR_RE.finditer(text):
        axis = m.group("axis")
        value = int(m.group("value"))
        if 120 <= value <= 10000:
            values[axis] = (value, m.group(0).strip())

    if "WIDTH" not in values or "HEIGHT" not in values:
        return None

    width, width_line = values["WIDTH"]
    height, height_line = values["HEIGHT"]
    candidate = make_resolution_candidate(
        width,
        height,
        source=path,
        line=f"{width_line}; {height_line}",
        score=95,
        natural_landscape=natural_landscape,
    )
    return candidate


def find_touch_panel_resolution(
    path: Path,
    text: str,
    *,
    natural_landscape: bool,
) -> list[ResolutionCandidate]:
    candidates: list[ResolutionCandidate] = []

    panel_values: dict[str, dict[str, tuple[int, str]]] = {}
    for m in TOUCH_PANEL_MAX_RE.finditer(text):
        prefix = m.group("prefix")
        axis = "x" if m.group("axis").endswith("-x") else "y"
        value = int(m.group("value"))
        if 120 <= value <= 10000:
            panel_values.setdefault(prefix, {})[axis] = (value, m.group(0).strip())

    for values in panel_values.values():
        if "x" not in values or "y" not in values:
            continue

        x, x_line = values["x"]
        y, y_line = values["y"]
        candidate = make_resolution_candidate(
            x,
            y,
            source=path,
            line=f"{x_line} {y_line}",
            score=85,
            natural_landscape=natural_landscape,
        )
        if candidate is not None:
            candidates.append(candidate)

    factors: dict[str, int] = {}
    for m in TOUCH_SUPER_RESOLUTION_RE.finditer(text):
        factor = int(m.group("factor"))
        if factor > 1:
            factors[m.group("prefix")] = factor

    for m in TOUCH_DISPLAY_COORDS_RE.finditer(text):
        factor = factors.get(m.group("prefix"))
        if factor is None:
            continue

        raw_x = int(m.group("x"))
        raw_y = int(m.group("y"))
        if raw_x % factor != 0 or raw_y % factor != 0:
            continue

        candidate = make_resolution_candidate(
            raw_x // factor,
            raw_y // factor,
            source=path,
            line=f"{m.group(0).strip()} with {m.group('prefix')},super-resolution-factors = <{factor}>",
            score=75,
            natural_landscape=natural_landscape,
        )
        if candidate is not None:
            candidates.append(candidate)

    return candidates


def wiki_device_files(wiki_root: Path, codename: str) -> Iterator[Path]:
    devices_dir = wiki_root / "_data" / "devices"
    if not devices_dir.is_dir():
        return

    target = codename.casefold()
    for path in devices_dir.glob("*.yml"):
        try:
            text = read_text_lossy(path)
        except OSError:
            continue

        m = WIKI_CODENAME_RE.search(text)
        if m and m.group("codename").casefold() == target:
            yield path


def find_wiki_resolution(
    wiki_root: Optional[Path],
    codename: str,
    *,
    natural_landscape: bool,
) -> list[ResolutionCandidate]:
    if wiki_root is None:
        return []

    candidates: list[ResolutionCandidate] = []
    for path in wiki_device_files(wiki_root, codename):
        try:
            text = read_text_lossy(path)
        except OSError:
            continue

        match = WIKI_INLINE_SCREEN_RE.search(text)
        if match is None:
            block = WIKI_SCREEN_BLOCK_RE.search(text)
            if block is not None:
                match = WIKI_BLOCK_RESOLUTION_RE.search(block.group("body"))
        if match is None:
            continue

        resolution = match.group("resolution")
        res_match = RESOLUTION_RE.search(resolution)
        if res_match is None:
            continue

        candidate = make_resolution_candidate(
            int(res_match.group("a")),
            int(res_match.group("b")),
            source=path,
            line=f"screen resolution: {resolution}",
            score=80,
            natural_landscape=natural_landscape,
        )
        if candidate is not None:
            candidates.append(candidate)

    return candidates


def find_resolution(
    roots: list[Path],
    *,
    natural_landscape: bool,
    wiki_root: Optional[Path],
    codename: str,
) -> Optional[ResolutionCandidate]:
    candidates: list[ResolutionCandidate] = []

    for root in roots:
        for path in interesting_text_files(root):
            try:
                text = read_text_lossy(path)
            except OSError:
                continue

            makefile_candidate = find_makefile_screen_resolution(
                path,
                text,
                natural_landscape=natural_landscape,
            )
            if makefile_candidate is not None:
                candidates.append(makefile_candidate)

            candidates.extend(
                find_touch_panel_resolution(
                    path,
                    text,
                    natural_landscape=natural_landscape,
                )
            )

            for line in text.splitlines():
                for m in RESOLUTION_RE.finditer(line):
                    a = int(m.group("a"))
                    b = int(m.group("b"))

                    if natural_landscape:
                        x, y = max(a, b), min(a, b)
                    else:
                        x, y = min(a, b), max(a, b)

                    candidate = make_resolution_candidate(
                        x,
                        y,
                        source=path,
                        line=line.strip(),
                        score=score_resolution(path, line),
                        natural_landscape=False,
                    )
                    if candidate is not None:
                        candidates.append(candidate)

    candidates.extend(find_wiki_resolution(wiki_root, codename, natural_landscape=natural_landscape))

    if not candidates:
        return None

    # Prefer candidates with higher score. Tie-break by larger pixel area.
    candidates.sort(key=lambda c: (c.score, c.x * c.y), reverse=True)
    return candidates[0]


def plausible_resolution_pair(a: int, b: int) -> bool:
    if min(a, b) < 480:
        return False
    if max(a, b) > 10000:
        return False
    ratio = max(a, b) / min(a, b)
    if ratio > 3.2:
        return False
    return True


def find_diagonal_inches(roots: list[Path]) -> Optional[InchCandidate]:
    candidates: list[InchCandidate] = []

    for root in roots:
        for path in root.glob("README*"):
            if not path.is_file():
                continue

            try:
                text = read_text_lossy(path)
            except OSError:
                continue

            for line in text.splitlines():
                if not DISPLAY_LINE_RE.search(line):
                    continue
                m = INCH_RE.search(line)
                if not m:
                    continue
                inches = float(m.group("inches"))
                if 2.0 <= inches <= 20.0:
                    score = 100 if "display" in line.lower() else 50
                    candidates.append(InchCandidate(inches, path, line.strip(), score))

    if not candidates:
        return None

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[0]


def find_density(roots: list[Path]) -> Optional[int]:
    candidates: list[tuple[int, int, Path]] = []

    for root in roots:
        for path in interesting_text_files(root):
            try:
                text = read_text_lossy(path)
            except OSError:
                continue

            for pattern in DENSITY_PATTERNS:
                for m in pattern.finditer(text):
                    density = int(m.group(1))
                    if 120 <= density <= 800:
                        score = 0
                        if path.name == "BoardConfig.mk":
                            score += 50
                        if path.suffix == ".prop":
                            score += 30
                        candidates.append((score, density, path))

    if not candidates:
        return None

    candidates.sort(reverse=True)
    return candidates[0][1]


def strip_xml_namespace(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def iter_xml_resource_files(roots: list[Path]) -> Iterator[Path]:
    for root in roots:
        for path in root.rglob("*.xml"):
            if ".git" in path.parts:
                continue
            # Most Android resource overlays live below res/values*.
            parts = {p.lower() for p in path.parts}
            if "values" in path.parent.name.lower() or "res" in parts:
                yield path


def parse_resource_xml(path: Path) -> list[ResourceValue]:
    try:
        text = read_text_lossy(path)
    except OSError:
        return []

    if "<resources" not in text:
        return []

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        # Android resource XML can contain oddities. Use a fallback regex for
        # simple <string>/<dimen>/<bool>/<integer> resource values.
        return parse_resource_xml_fallback(path, text)

    if strip_xml_namespace(root.tag) != "resources":
        return []

    values: list[ResourceValue] = []
    for elem in root.iter():
        if elem is root:
            continue

        tag = strip_xml_namespace(elem.tag)
        if tag not in {"string", "dimen", "bool", "integer", "fraction"}:
            continue

        name = elem.attrib.get("name")
        if not name:
            continue

        value = "".join(elem.itertext()).strip()
        if value:
            value = re.sub(r"\s+", " ", value)
        values.append(ResourceValue(name=name, value=value, source=path))

    return values


def parse_resource_xml_fallback(path: Path, text: str) -> list[ResourceValue]:
    values: list[ResourceValue] = []
    pattern = re.compile(
        r"<(?P<tag>string|dimen|bool|integer|fraction)\b[^>]*\bname=[\"'](?P<name>[^\"']+)[\"'][^>]*>"
        r"(?P<value>.*?)"
        r"</(?P=tag)>",
        re.DOTALL,
    )

    for m in pattern.finditer(text):
        raw = re.sub(r"<[^>]+>", "", m.group("value"))
        value = re.sub(r"\s+", " ", raw).strip()
        if value:
            values.append(ResourceValue(m.group("name"), value, path))

    return values


def collect_resources(roots: list[Path]) -> dict[str, list[ResourceValue]]:
    resources: dict[str, list[ResourceValue]] = {}

    for path in iter_xml_resource_files(roots):
        for value in parse_resource_xml(path):
            resources.setdefault(value.name, []).append(value)

    return resources


def is_overlay_preferred(path: Path) -> int:
    parts = [p.lower() for p in path.parts]
    score = 0
    for idx, part in enumerate(parts):
        if part in {"overlay", "overlay-lineage", "rro_overlays"}:
            score += 100
        if part.startswith("values"):
            score += 20
        if part in {"frameworks", "base", "core", "res"}:
            score += 5
    if path.name == "config.xml":
        score += 20
    if path.name == "dimens.xml":
        score += 20
    return score


def pick_resource(resources: dict[str, list[ResourceValue]], names: Iterable[str]) -> Optional[ResourceValue]:
    all_values: list[ResourceValue] = []
    for name in names:
        all_values.extend(resources.get(name, []))

    if not all_values:
        return None

    def key(rv: ResourceValue) -> tuple[int, int]:
        value_score = 0 if rv.value.strip() else -100
        return (is_overlay_preferred(rv.source), value_score)

    all_values.sort(key=key, reverse=True)
    return all_values[0]


def resolve_reference(
    rv: Optional[ResourceValue],
    resources: dict[str, list[ResourceValue]],
    *,
    max_depth: int = 10,
) -> Optional[ResourceValue]:
    if rv is None:
        return None

    current = rv
    seen = {current.name}

    for _ in range(max_depth):
        value = current.value.strip()
        m = re.match(r"@(?:\*android:)?(?P<typ>string|dimen|integer|bool)/(?P<name>[\w.]+)$", value)
        if not m:
            return current

        ref_name = m.group("name")
        if ref_name in seen:
            return current
        seen.add(ref_name)

        next_rv = pick_resource(resources, [ref_name])
        if next_rv is None:
            return current
        current = next_rv

    return current


def parse_dimension_to_px(value: str, density: Optional[int]) -> Optional[float]:
    value = value.strip()
    m = re.match(r"^(-?\d+(?:\.\d+)?)(px|dp|dip|sp)?$", value, re.IGNORECASE)
    if not m:
        return None

    amount = float(m.group(1))
    unit = (m.group(2) or "px").lower()

    if unit == "px":
        return amount

    if unit in {"dp", "dip", "sp"}:
        if density is None:
            return None
        return amount * density / 160.0

    return None


def format_num(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def parse_android_path_suffixes(path: str) -> tuple[str, set[str]]:
    """
    Android cutout strings may end with markers like '@dp', '@left', '@right'.
    Return the path without markers and a set of markers.
    """
    markers: set[str] = set()
    path = re.sub(r"\s+", " ", path.strip())

    while True:
        m = re.search(r"\s@(?P<marker>dp|left|right)\s*$", path)
        if not m:
            # Some overlays omit the whitespace before the marker.
            m = re.search(r"@(?P<marker>dp|left|right)\s*$", path)
        if not m:
            break
        markers.add(m.group("marker"))
        path = path[: m.start()].strip()

    return path, markers


SVG_TOKEN_RE = re.compile(
    r"(?P<cmd>[AaCcHhLlMmQqSsTtVvZz])|"
    r"(?P<num>[+-]?(?:\d+\.\d+|\d+\.|\.\d+|\d+)(?:[eE][+-]?\d+)?)"
)

ARG_COUNTS = {
    "M": 2,
    "L": 2,
    "H": 1,
    "V": 1,
    "C": 6,
    "S": 4,
    "Q": 4,
    "T": 2,
    "A": 7,
    "Z": 0,
}


def tokenize_svg_path(path: str) -> list[str]:
    tokens: list[str] = []
    pos = 0

    for m in SVG_TOKEN_RE.finditer(path):
        skipped = path[pos : m.start()]
        if skipped.strip().strip(","):
            raise ScriptError(f"Could not parse SVG path near: {skipped!r}")
        tokens.append(m.group("cmd") or m.group("num"))
        pos = m.end()

    tail = path[pos:]
    if tail.strip().strip(","):
        raise ScriptError(f"Could not parse SVG path tail: {tail!r}")

    return tokens


def is_cmd(token: str) -> bool:
    return len(token) == 1 and token.isalpha()


def split_path_segments(tokens: list[str]) -> list[tuple[str, list[float]]]:
    """
    Split a path into command segments with one command's worth of numbers each.
    Handles implicit repeated commands such as "M 0 0 10 10".
    """
    segments: list[tuple[str, list[float]]] = []
    i = 0
    cmd: Optional[str] = None

    while i < len(tokens):
        if is_cmd(tokens[i]):
            cmd = tokens[i]
            i += 1
        elif cmd is None:
            raise ScriptError("SVG path starts with numbers instead of a command")

        assert cmd is not None
        upper = cmd.upper()
        argc = ARG_COUNTS.get(upper)
        if argc is None:
            raise ScriptError(f"Unsupported SVG path command: {cmd}")

        if argc == 0:
            segments.append((cmd, []))
            cmd = None
            continue

        first_for_command = True
        while i < len(tokens) and not is_cmd(tokens[i]):
            if i + argc > len(tokens):
                raise ScriptError(f"Not enough arguments for SVG command {cmd}")

            raw_args = tokens[i : i + argc]
            if any(is_cmd(t) for t in raw_args):
                raise ScriptError(f"Not enough numeric arguments for SVG command {cmd}")

            args = [float(t) for t in raw_args]
            out_cmd = cmd

            # SVG: extra coordinate pairs following M/m are treated as L/l.
            if upper == "M" and not first_for_command:
                out_cmd = "L" if cmd == "M" else "l"

            segments.append((out_cmd, args))
            i += argc
            first_for_command = False

            # Most commands can repeat with the same command letter until a new
            # command appears. If a new command appears, leave the loop.
            if i >= len(tokens) or is_cmd(tokens[i]):
                break

    return segments


def scale_xy(value: float, scale: float) -> float:
    return value * scale


def transform_segment(
    cmd: str,
    args: list[float],
    *,
    origin_x: float,
    scale: float,
    is_first_segment: bool,
) -> tuple[str, list[float]]:
    """
    Convert Android display-cutout path coordinates to gmobile coordinates.

    Android default path origin is center-top; gmobile wants top-left. For
    absolute X coordinates we add origin_x. Relative commands are not shifted.

    If the Android path is marked '@dp', scale all length-like values first.
    """
    upper = cmd.upper()
    relative = cmd.islower()

    # Treat an initial relative moveto like an absolute moveto relative to the
    # Android origin. This makes odd-but-seen paths such as "m -10,0 ..." work.
    if is_first_segment and cmd == "m":
        cmd = "M"
        upper = "M"
        relative = False

    out = list(args)

    def transform_x(idx: int) -> None:
        out[idx] = scale_xy(out[idx], scale)
        if not relative:
            out[idx] += origin_x

    def transform_y(idx: int) -> None:
        out[idx] = scale_xy(out[idx], scale)

    if upper in {"M", "L", "T"}:
        transform_x(0)
        transform_y(1)
    elif upper == "H":
        transform_x(0)
    elif upper == "V":
        transform_y(0)
    elif upper == "C":
        transform_x(0)
        transform_y(1)
        transform_x(2)
        transform_y(3)
        transform_x(4)
        transform_y(5)
    elif upper in {"S", "Q"}:
        transform_x(0)
        transform_y(1)
        transform_x(2)
        transform_y(3)
    elif upper == "A":
        # A rx ry x-axis-rotation large-arc-flag sweep-flag x y
        out[0] = scale_xy(out[0], scale)
        out[1] = scale_xy(out[1], scale)
        transform_x(5)
        transform_y(6)
    elif upper == "Z":
        pass
    else:
        raise ScriptError(f"Unsupported SVG path command: {cmd}")

    return cmd, out


def transform_android_cutout_path(
    android_path: str,
    *,
    x_res: int,
    density: Optional[int],
) -> tuple[str, list[str]]:
    raw_path, markers = parse_android_path_suffixes(android_path)
    notes: list[str] = []

    if "left" in markers and "right" in markers:
        raise ScriptError("Cutout path cannot use both @left and @right markers")

    if "dp" in markers:
        if density is None:
            raise ScriptError("Cutout path uses @dp but no screen density was found; pass --density")
        scale = density / 160.0
        notes.append(f"Converted Android @dp cutout path to px using density {density} dpi.")
    else:
        scale = 1.0

    if "left" in markers:
        origin_x = 0.0
        notes.append("Android cutout path uses @left; no horizontal shift applied.")
    elif "right" in markers:
        origin_x = float(x_res)
        notes.append("Android cutout path uses @right; shifted x coordinates by display width.")
    else:
        origin_x = x_res / 2.0
        notes.append("Android cutout path origin is center-top; shifted absolute x coordinates by x-res / 2.")

    tokens = tokenize_svg_path(raw_path)
    segments = split_path_segments(tokens)

    out_parts: list[str] = []
    for idx, (cmd, args) in enumerate(segments):
        out_cmd, out_args = transform_segment(
            cmd,
            args,
            origin_x=origin_x,
            scale=scale,
            is_first_segment=(idx == 0),
        )

        if out_args:
            if out_cmd.upper() == "A":
                # Keep arc flags as integer-looking values.
                formatted = [
                    format_num(out_args[0]),
                    format_num(out_args[1]),
                    format_num(out_args[2]),
                    format_num(out_args[3]),
                    format_num(out_args[4]),
                    format_num(out_args[5]),
                    format_num(out_args[6]),
                ]
            else:
                formatted = [format_num(v) for v in out_args]

            # Use commas between coordinate-like values. SVG accepts spaces too,
            # but this resembles existing gmobile JSON files.
            out_parts.append(out_cmd + " " + ",".join(formatted))
        else:
            out_parts.append(out_cmd)

    return " ".join(out_parts), notes


def compute_physical_mm(x_res: int, y_res: int, diagonal_inches: float) -> tuple[int, int]:
    diag_px = math.hypot(x_res, y_res)
    width_in = diagonal_inches * x_res / diag_px
    height_in = diagonal_inches * y_res / diag_px
    return round(width_in * 25.4), round(height_in * 25.4)


def find_device_name(roots: list[Path], oem: str, codename: str) -> str:
    readme_candidates: list[tuple[int, str]] = []

    for root in roots:
        for path in root.glob("README*"):
            if not path.is_file():
                continue
            try:
                text = read_text_lossy(path)
            except OSError:
                continue

            for line in text.splitlines():
                clean = line.strip().strip("#").strip()
                if not clean:
                    continue

                m = re.search(r"Device configuration for\s+(.+)$", clean, re.IGNORECASE)
                if m:
                    readme_candidates.append((100, m.group(1).strip()))
                    continue

                m = re.search(r"The\s+(.+?)\s+\(codenamed\s+[\"']?" + re.escape(codename) + r"[\"']?\)", clean, re.IGNORECASE)
                if m:
                    readme_candidates.append((80, m.group(1).strip()))

    if readme_candidates:
        readme_candidates.sort(reverse=True)
        return readme_candidates[0][1]

    # PRODUCT_MODEL is also common.
    model_pattern = re.compile(r"^\s*PRODUCT_MODEL\s*[:?+]?=\s*(.+?)\s*$", re.MULTILINE)
    for root in roots:
        for path in interesting_text_files(root):
            try:
                text = read_text_lossy(path)
            except OSError:
                continue
            m = model_pattern.search(text)
            if m:
                model = m.group(1).strip().strip('"')
                if model:
                    return model

    return f"{oem.title()} {codename}"


def build_findings(
    roots: list[Path],
    *,
    oem: str,
    codename: str,
    wiki_root: Optional[Path],
    x_res_override: Optional[int],
    y_res_override: Optional[int],
    density_override: Optional[int],
    natural_landscape: bool,
) -> Findings:
    notes: list[str] = []

    density = density_override if density_override is not None else find_density(roots)
    if density:
        notes.append(f"Detected density: {density} dpi.")
    else:
        notes.append("No screen density found; px values are fine, but dp/dip values cannot be converted automatically.")

    if x_res_override and y_res_override:
        x_res, y_res = x_res_override, y_res_override
        notes.append("Using resolution from command-line overrides.")
    else:
        candidate = find_resolution(
            roots,
            natural_landscape=natural_landscape,
            wiki_root=wiki_root,
            codename=codename,
        )
        if candidate is None:
            raise ScriptError("Could not find display resolution. Pass --x-res and --y-res.")
        x_res, y_res = candidate.x, candidate.y
        notes.append(f"Detected resolution {x_res}x{y_res} from {candidate.source}: {candidate.line}")

    inch_candidate = find_diagonal_inches(roots)
    diagonal_inches = inch_candidate.inches if inch_candidate else None
    if inch_candidate:
        notes.append(f"Detected display diagonal {inch_candidate.inches:g}\" from {inch_candidate.source}: {inch_candidate.line}")

    resources = collect_resources(roots)

    cutout_path = resolve_reference(pick_resource(resources, [CUTOUT_RESOURCE]), resources)
    cutout_rect = resolve_reference(pick_resource(resources, [CUTOUT_RECT_RESOURCE]), resources)

    if cutout_path:
        notes.append(f"Found {CUTOUT_RESOURCE} in {cutout_path.source}.")
    else:
        notes.append(f"No {CUTOUT_RESOURCE} found.")

    if cutout_rect:
        notes.append(f"Found {CUTOUT_RECT_RESOURCE} in {cutout_rect.source}; not used by default.")

    def dim_from_names(names: Iterable[str]) -> Optional[float]:
        rv = resolve_reference(pick_resource(resources, names), resources)
        if not rv:
            return None
        px = parse_dimension_to_px(rv.value, density)
        if px is None:
            notes.append(f"Could not convert dimension {rv.name}={rv.value!r} from {rv.source}.")
        else:
            notes.append(f"Detected {rv.name}={rv.value} -> {format_num(px)} px from {rv.source}.")
        return px

    corner_top = dim_from_names(CORNER_TOP_NAMES)
    corner_bottom = dim_from_names(CORNER_BOTTOM_NAMES)
    corner_default = dim_from_names(CORNER_DEFAULT_NAMES)
    status_bar = dim_from_names(STATUS_BAR_NAMES)

    name = find_device_name(roots, oem, codename)

    return Findings(
        name=name,
        x_res=x_res,
        y_res=y_res,
        density=density,
        cutout_path_android=cutout_path,
        cutout_rect_android=cutout_rect,
        corner_top_px=corner_top,
        corner_bottom_px=corner_bottom,
        corner_default_px=corner_default,
        status_bar_px=status_bar,
        diagonal_inches=diagonal_inches,
        notes=notes,
    )


def rounded_int_or_none(value: Optional[float]) -> Optional[int]:
    if value is None:
        return None
    return int(round(value))


def build_gmobile_json(
    findings: Findings,
    *,
    use_rect_approx: bool,
    corner_format: str,
    include_physical_size: bool,
) -> tuple[dict, list[str]]:
    notes = list(findings.notes)

    obj: dict = {
        "name": findings.name,
        "x-res": findings.x_res,
        "y-res": findings.y_res,
    }

    if include_physical_size and findings.diagonal_inches:
        width_mm, height_mm = compute_physical_mm(findings.x_res, findings.y_res, findings.diagonal_inches)
        obj["width"] = width_mm
        obj["height"] = height_mm
        notes.append(f"Computed physical size approximately {width_mm}x{height_mm} mm from diagonal.")

    top = rounded_int_or_none(findings.corner_top_px)
    bottom = rounded_int_or_none(findings.corner_bottom_px)
    default = rounded_int_or_none(findings.corner_default_px)

    if top is None:
        top = default
    if bottom is None:
        bottom = default

    if top is not None or bottom is not None:
        top = top or 0
        bottom = bottom or 0
        all_equal = top == bottom

        if corner_format == "border-radius" or (corner_format == "auto" and all_equal):
            obj["border-radius"] = top
            notes.append("Using gmobile border-radius because all corner radii are equal.")
        else:
            # Order is clockwise from top-left.
            obj["corner-radii"] = [top, top, bottom, bottom]
            notes.append("Using gmobile corner-radii as [top-left, top-right, bottom-right, bottom-left].")

    chosen_cutout = findings.cutout_rect_android if use_rect_approx else findings.cutout_path_android
    if chosen_cutout and chosen_cutout.value.strip():
        transformed, path_notes = transform_android_cutout_path(
            chosen_cutout.value,
            x_res=findings.x_res,
            density=findings.density,
        )
        notes.extend(path_notes)

        obj["cutouts"] = [
            {
                "name": "front-camera",
                "path": transformed,
            }
        ]
    elif findings.cutout_path_android and not findings.cutout_path_android.value.strip():
        notes.append("Cutout resource is empty; generated JSON has no cutouts.")

    return obj, notes


def write_json(obj: dict, output: Path, *, force: bool) -> None:
    if output.exists() and not force:
        raise ScriptError(f"Output file already exists: {output}. Use --force to overwrite.")

    output.parent.mkdir(parents=True, exist_ok=True)
    text = format_gmobile_json(obj)
    output.write_text(text, encoding="utf-8")


def format_gmobile_json(obj: dict) -> str:
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    text = re.sub(
        r'  "corner-radii": \[\n'
        r"    (?P<tl>-?\d+),\n"
        r"    (?P<tr>-?\d+),\n"
        r"    (?P<br>-?\d+),\n"
        r"    (?P<bl>-?\d+)\n"
        r"  \]",
        r'  "corner-radii": [\g<tl>, \g<tr>, \g<br>, \g<bl>]',
        text,
    )
    return text + "\n"


def validate_with_json_glib(output: Path) -> None:
    if shutil.which("json-glib-validate") is None:
        eprint("json-glib-validate not found; skipping validation.")
        return

    run(["json-glib-validate", str(output)])


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a gmobile display-panel JSON from LineageOS device overlays.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument("oem", help="Device OEM/manufacturer in LineageOS repo name, e.g. xiaomi")
    parser.add_argument("codename", help="Device codename in LineageOS repo name, e.g. sweet")

    parser.add_argument("--org", default="LineageOS", help="GitHub organization/user containing LineageOS device repos")
    parser.add_argument("--repo-url", help="Override repository URL instead of android_device_<oem>_<codename>")
    parser.add_argument("--branch", help="Git branch to clone; auto-detects newest lineage-* branch when omitted")
    parser.add_argument("--workdir", type=Path, default=Path("./lineage-device-work"), help="Directory for cloned repositories")
    parser.add_argument("--output-dir", type=Path, default=Path("."), help="Directory for generated gmobile JSON")
    parser.add_argument("--output-file", type=Path, help="Exact output file path; overrides --output-dir")
    parser.add_argument("--compatible", help="Output compatible/file stem, e.g. xiaomi,sweet. Defaults to <oem>,<codename>")
    parser.add_argument("--force", action="store_true", help="Overwrite existing output JSON")
    parser.add_argument("--update", action="store_true", help="Fetch/pull if the repository already exists")
    parser.add_argument("--depth", type=int, default=1, help="git clone depth; set 0 for full clone")

    parser.add_argument("--clone-dependencies", action="store_true", help="Also clone repositories listed in lineage.dependencies")
    parser.add_argument("--max-deps", type=int, default=8, help="Maximum lineage.dependencies repositories to clone")
    parser.add_argument("--use-wiki", action="store_true", help="Use LineageOS wiki device YAML as a fallback for display resolution")

    parser.add_argument("--x-res", type=int, help="Override detected display x resolution")
    parser.add_argument("--y-res", type=int, help="Override detected display y resolution")
    parser.add_argument("--density", type=int, help="Override detected Android screen density for dp/dip conversion")
    parser.add_argument("--natural-landscape", action="store_true", help="Treat the larger resolution dimension as x-res")

    parser.add_argument(
        "--corner-format",
        choices=("auto", "border-radius", "corner-radii"),
        default="corner-radii",
        help="How to emit rounded corners",
    )
    parser.add_argument("--use-rect-approx", action="store_true", help="Use config_mainBuiltInDisplayCutoutRectApproximation instead of the exact cutout path")
    parser.add_argument("--include-physical-size", action="store_true", help="Try to emit physical width/height in millimeters from README diagonal size")
    parser.add_argument("--no-validate", action="store_true", help="Do not run json-glib-validate when available")
    parser.add_argument("--print-notes", action="store_true", help="Print source/detection notes after generation")

    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    try:
        ensure_git()

        oem = args.oem.strip().lower()
        codename = args.codename.strip().lower()
        repo_url = args.repo_url or make_repo_url(args.org, oem, codename)
        repo_name = repo_name_from_url(repo_url)
        target = args.workdir / repo_name
        branch = choose_branch_for_target(repo_url, args.branch, target, update=args.update)
        root = clone_or_update_repo(
            repo_url,
            branch,
            target,
            update=args.update,
            depth=args.depth,
        )

        search_roots = [root]
        if args.clone_dependencies:
            deps = clone_lineage_dependencies(
                root,
                args.workdir,
                args.org,
                update=args.update,
                depth=args.depth,
                max_deps=args.max_deps,
            )
            search_roots.extend(deps)

        wiki_root: Optional[Path] = None
        if args.use_wiki:
            try:
                wiki_root = clone_or_update_wiki(args.workdir / "_wiki", update=args.update, depth=args.depth)
            except Exception as exc:
                eprint(f"Warning: could not use LineageOS wiki fallback: {exc}")

        findings = build_findings(
            search_roots,
            oem=oem,
            codename=codename,
            wiki_root=wiki_root,
            x_res_override=args.x_res,
            y_res_override=args.y_res,
            density_override=args.density,
            natural_landscape=args.natural_landscape,
        )

        obj, notes = build_gmobile_json(
            findings,
            use_rect_approx=args.use_rect_approx,
            corner_format=args.corner_format,
            include_physical_size=args.include_physical_size,
        )

        if args.output_file:
            output = args.output_file
        else:
            compatible = args.compatible or f"{oem},{codename}"
            output = args.output_dir / f"{compatible}.json"

        write_json(obj, output, force=args.force)

        if not args.no_validate:
            validate_with_json_glib(output)

        print(f"Wrote {output}")
        print(format_gmobile_json(obj), end="")

        if args.print_notes:
            print("\nDetection notes:", file=sys.stderr)
            for note in notes:
                print(f"  - {note}", file=sys.stderr)

        return 0

    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            eprint(exc.stdout)
        if exc.stderr:
            eprint(exc.stderr)
        eprint(f"Command failed with exit status {exc.returncode}: {' '.join(exc.cmd)}")
        return exc.returncode or 1
    except ScriptError as exc:
        eprint(f"Error: {exc}")
        return 1
    except KeyboardInterrupt:
        eprint("Interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
