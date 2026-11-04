"""Dockerfile and .dockerignore readers for the packaging tests: instructions, stages, COPY sources, the build-context
rules and the file layout a stage builds.

Not a test module: pytest imports it as a sibling of the tests in this folder.
"""
import posixpath
import re
import shlex


def instructions(text):
    """Dockerfile instructions with comment lines dropped and backslash continuations joined."""
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    return [line.strip() for line in re.sub(r"\\\n", " ", "\n".join(lines)).splitlines() if line.strip()]


def stage(lines, name=None):
    """A Dockerfile stage's instructions after its FROM line: the stage called name, or the last stage (the image)."""
    starts = [index for index, line in enumerate(lines) if line.split()[0].upper() == "FROM"]
    chosen = [index for index in starts
              if name is None or [word.upper() for word in lines[index].split()[-2:]] == ["AS", name.upper()]]
    if not chosen:
        raise ValueError(f"The Dockerfile has no stage named {name}")
    end = next((index for index in starts if index > chosen[-1]), len(lines))
    return lines[chosen[-1] + 1:end]


def copy_sources(text):
    """Build-context sources of every COPY/ADD that reads the context (not --from stages or URLs)."""
    sources = []
    for line in instructions(text):
        keyword, _, rest = line.partition(" ")
        if keyword.upper() not in {"COPY", "ADD"} or "--from=" in rest:
            continue
        paths = [arg for arg in shlex.split(rest) if not arg.startswith("--")][:-1]
        sources.extend(path for path in paths if "://" not in path)
    return sources


def source_root(path, name):
    """The file or parent folder a COPY source matches; Docker's * and ? never cross a path separator."""
    if path == ".":
        return "."
    regex = re.escape(path).replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    parents = ["/".join(name.split("/")[:i]) for i in range(1, name.count("/") + 2)]
    return next((parent for parent in parents if re.fullmatch(regex, parent)), None)


def under(members, path):
    """The members a literal or glob COPY source includes; "." is all of them."""
    return sorted(name for name in members if source_root(path, name) is not None)


def dockerignore_admits(patterns, path):
    """Apply .dockerignore rules to a context path: the last matching rule wins and parent directories match."""
    admitted = True
    parents = ["/".join(path.split("/")[:i]) for i in range(1, path.count("/") + 2)]
    for rule in patterns:
        pattern = rule.removeprefix("!").strip("/")
        regex = ""
        for token in re.split(r"(\*\*/|\*\*|\*|\?)", pattern):
            regex += {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}.get(token, re.escape(token))
        if any(re.fullmatch(regex, parent) for parent in parents):
            admitted = rule.startswith("!")
    return admitted


def dockerignore_rules(text):
    """The rules of a .dockerignore file: its non-blank, non-comment lines, stripped."""
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def image_layout(lines, rules, members):
    """Map each context file that one stage's COPY lines copy to its path in the image, and return the stage's final
    WORKDIR with the map.

    lines are the stage's instructions (stage()); the context is members filtered by the .dockerignore rules. As in
    Docker, a relative target starts at the WORKDIR, a copied folder's files keep their paths below it, a copied file
    goes into the target folder when the target ends in "/", and options such as --chown= are not sources.
    """
    layout, workdir = {}, "/"
    for line in lines:
        keyword, _, rest = line.partition(" ")
        keyword = keyword.upper()
        if keyword == "WORKDIR":
            workdir = posixpath.join(workdir, rest.strip())
        elif keyword == "COPY" and "--from=" not in rest:
            *sources, target = [arg for arg in shlex.split(rest) if not arg.startswith("--")]
            target = posixpath.join(workdir, target)
            for source in sources:
                folder = posixpath.normpath(source)
                for name in under(members, folder):
                    if not dockerignore_admits(rules, name):
                        continue
                    matched = source_root(folder, name)
                    if name == matched:
                        path = posixpath.join(target, posixpath.basename(name)) if target.endswith("/") else target
                    else:
                        path = posixpath.join(target, name.removeprefix("" if matched == "." else matched + "/"))
                    layout[name] = posixpath.normpath(path)
    return workdir, layout
