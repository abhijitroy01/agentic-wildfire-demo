"""Markdown links, anchors, repository paths and doc numbers resolve (W3.5, DOCS-23)."""
import json
import os
import re
import subprocess  # nosec B404 - runs only a fixed git check-ignore argument list on this checkout, never a shell
import unicodedata
from collections import Counter
from pathlib import PurePosixPath
from urllib.parse import unquote

from deployment import ROOT
from deployment.package_source import GIT_LOCATION

FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
INLINE_CODE = re.compile(r"`([^`\n]+)`")
LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")
# "architecture/29", "doc 29", "docs 29 and 36" or "docs 02, 04 and 29" must name an existing architecture/NN-*.md.
DOC_PATH = re.compile(r"architecture/(\d\d)(?![\d])")
DOC_WORDS = re.compile(r"\b[Dd]ocs? (\d\d(?:(?:, | and |, and )\d\d)*)\b(?!\d)")
TEXT_SUFFIXES = {".md", ".py", ".ts", ".tsx", ".cjs", ".js", ".json", ".toml", ".yaml", ".yml", ".sh"}
SNAPSHOTS = "backend/knowledge/public-sop/snapshots/"
LOCKFILES = {"package-lock.json"}  # npm writes them; nothing names a doc there


def prose(text):
    """Return the text with fenced blocks blanked (line numbers kept) for link and path checks."""
    lines, fenced = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            line = ""
        lines.append("" if fenced else line)
    return lines


def slugs(text):
    """GitHub heading anchors: lowercase, punctuation dropped, spaces to hyphens, -1, -2 for repeats."""
    seen, found = Counter(), set()
    for line in prose(text):
        if match := HEADING.match(line):
            title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", match.group(1).replace("`", "")).lower()
            slug = "".join("-" if ch == " " else ch for ch in title
                           if ch in "-_ " or unicodedata.category(ch)[0] in "LN")
            found.add(f"{slug}-{seen[slug]}" if seen[slug] else slug)
            seen[slug] += 1
    return found


def resolve(base, target):
    """Join a relative target to a folder; None when it leaves the repository."""
    parts = []
    for part in (PurePosixPath(base) / target).parts:
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part != ".":
            parts.append(part)
    return "/".join(parts)


def layout(tracked_files):
    """The tracked files, every folder that holds one, and the Markdown files with their text."""
    folders = {str(parent) for name in tracked_files for parent in PurePosixPath(name).parents} - {"."}
    docs = {name: (ROOT / name).read_text() for name in tracked_files
            if name.endswith(".md") and (ROOT / name).is_file()}
    return set(tracked_files), folders, docs


def ignored(paths):
    """The given repository paths that git ignores, from one git check-ignore call."""
    env = {key: value for key, value in os.environ.items() if key not in GIT_LOCATION}
    # Fixed git arguments on this checkout; no shell.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    done = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "--stdin"],  # nosec B603 B607
                          input="\n".join(paths), capture_output=True, text=True, env=env, timeout=60,
                          check=False)
    assert done.returncode in (0, 1), done.stderr  # 1: nothing ignored
    return set(done.stdout.splitlines())


def test_markdown_links_and_anchors_resolve(tracked_files):
    files, folders, docs = layout(tracked_files)
    anchors = {name: slugs(text) for name, text in docs.items()}
    broken = []
    for doc, text in docs.items():
        for number, line in enumerate(prose(text), 1):
            for target in LINK.findall(INLINE_CODE.sub("", line)):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                path, _, anchor = unquote(target).partition("#")
                dest = resolve(PurePosixPath(doc).parent, path) if path else doc
                if dest is None or (dest not in files and dest.rstrip("/") not in folders):
                    broken.append(f"{doc}:{number}: {target} (no such tracked file or folder)")
                elif anchor and dest.endswith(".md") and anchor not in anchors[dest]:
                    broken.append(f"{doc}:{number}: {target} (no heading #{anchor} in {dest})")
    assert not broken, "Fix these Markdown links:\n" + "\n".join(broken)


def test_backticked_repository_paths_exist(tracked_files):
    files, folders, docs = layout(tracked_files)
    roots = {PurePosixPath(name).parts[0] for name in tracked_files}
    candidates = []
    for doc, text in docs.items():
        base = PurePosixPath(doc).parent
        for number, line in enumerate(prose(text), 1):
            for token in INLINE_CODE.findall(line):
                token = LINE_SUFFIX.sub("", token.strip())
                if "/" not in token or token.startswith("/") or any(ch in token for ch in " <*{$:"):
                    continue
                first = PurePosixPath(token).parts[0]
                local = resolve(base, first)
                where = [p for p in (token, resolve(base, token)) if p]
                if first in roots or local in folders:
                    candidates.append((f"{doc}:{number}: `{token}`", [p.rstrip("/") for p in where]))
    unknown = [(name, paths) for name, paths in candidates
               if not any(path in files or path in folders for path in paths)]
    # A folder-only ignore pattern (".runtime/") matches a path inside the folder, not a folder that is absent.
    probes = {name: paths + [f"{path}/_" for path in paths] for name, paths in unknown}
    ignored_paths = ignored([path for paths in probes.values() for path in paths])
    missing = [name for name, paths in probes.items() if not ignored_paths.intersection(paths)]
    assert not missing, "These backticked paths are neither tracked nor git-ignored:\n" + "\n".join(missing)


def test_doc_numbers_name_existing_docs(tracked_files):
    numbers = {PurePosixPath(name).name[:2] for name in tracked_files
               if re.fullmatch(r"architecture/\d\d-[^/]+\.md", name)}
    unknown = []
    for name in tracked_files:
        path = ROOT / name
        skipped = name.startswith(SNAPSHOTS) or PurePosixPath(name).name in LOCKFILES
        if PurePosixPath(name).suffix not in TEXT_SUFFIXES or skipped or not path.is_file():
            continue
        for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            cited = DOC_PATH.findall(line) + [n for group in DOC_WORDS.findall(line) for n in re.findall(r"\d\d", group)]
            unknown += [f"{name}:{number}: doc {cited_number}" for cited_number in cited if cited_number not in numbers]
    assert not unknown, "These name an architecture doc that does not exist:\n" + "\n".join(unknown)


def test_image_scan_policy_names_the_exception_re_review_doc(tracked_files):
    review = json.loads((ROOT / "deployment/image-scan-policy.json").read_text())["review"]
    assert review in tracked_files and review.endswith(".md"), review
    assert "exception-re-review" in slugs((ROOT / review).read_text()), f"{review} has no Exception re-review heading"
