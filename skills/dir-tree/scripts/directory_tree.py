#!/usr/bin/env python3
"""Print a directory tree. Python 3.10+; pip install pathspec==1.1.1.

skip_gitignore=False applies .gitignore rules; True bypasses those rules.
The filesystem is read-only unless the CLI is given --output.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

try:
    from pathspec import GitIgnoreSpec
except ImportError:
    GitIgnoreSpec = None  # type: ignore[assignment,misc]


@dataclass(frozen=True)
class IgnoreRules:
    base: Path
    spec: GitIgnoreSpec


@dataclass(frozen=True)
class Entry:
    path: Path
    is_dir: bool
    is_link: bool


def display_name(value: str) -> str:
    """Quote unusual names so newlines/control characters cannot corrupt the tree."""
    if (value and value == value.strip()
            and all(c.isprintable() and c not in '\\"' for c in value)):
        return value
    return json.dumps(value, ensure_ascii=True)


def compile_rules(base: Path, lines: Iterable[str]) -> IgnoreRules:
    patterns = []
    for line in lines:
        line = line.rstrip('\r\n')
        trimmed = line if line.endswith('\\ ') else line.rstrip()
        # A terminal /** means CONTENTS, not the directory itself. Requiring
        # one child component prevents pathspec from matching `build/` against
        # `build/**` and pruning !build/keep.txt. The /**/ case needs this too.
        directory_only = trimmed.endswith('/')
        body = trimmed[:-1] if directory_only else trimmed
        if body.endswith('/**'):
            line = body + '/*' + ('/' if directory_only else '')
        patterns.append(line)
    return IgnoreRules(base, GitIgnoreSpec.from_lines(patterns))


def load_rules(directory: Path, inherited: tuple[IgnoreRules, ...]) -> tuple[IgnoreRules, ...]:
    """Load this directory's .gitignore, preserving the base for anchored patterns."""
    ignore_file = directory / '.gitignore'
    try:
        mode = ignore_file.lstat().st_mode
    except FileNotFoundError:
        return inherited
    # In particular, never open a symbolic link or a FIFO as .gitignore.
    if not stat.S_ISREG(mode):
        return inherited
    with ignore_file.open(encoding='utf-8-sig', errors='surrogateescape') as stream:
        try:
            rule = compile_rules(directory, stream)
        except ValueError as exc:
            raise ValueError(f'Invalid .gitignore in {directory}: {exc}') from exc
    return inherited + (rule,)


def ignored(path: Path, is_dir: bool, rules: tuple[IgnoreRules, ...]) -> bool:
    """A matching rule in a deeper .gitignore overrides an ancestor's decision."""
    for rule in reversed(rules):
        relative = path.relative_to(rule.base).as_posix() + ('/' if is_dir else '')
        decision = rule.spec.check_file(relative, separators=('/',)).include
        if decision is not None:
            return decision
    return False


def ancestor_rules(root: Path) -> tuple[tuple[IgnoreRules, ...], bool]:
    """Honor ancestor .gitignore files up to the nearest .git directory/file.

    Outside a Git working tree, the selected root is the ignore boundary.
    No Git executable, index, config, or network connection is used.
    """
    boundary = root
    for directory in (root, *root.parents):
        try:
            (directory / '.git').lstat()
        except FileNotFoundError:
            continue
        boundary = directory
        break

    rules: tuple[IgnoreRules, ...] = ()
    directory = boundary
    for part in root.relative_to(boundary).parts:
        rules = load_rules(directory, rules)
        directory = directory / part
        if ignored(directory, True, rules):
            return rules, True  # Do not resurrect descendants of ignored directories.
    return rules, False


def build_tree(
    directory: str | Path = '.',
    *,
    skip_gitignore: bool = False,
    max_depth: int | None = None,
    max_entries: int = 2000,
    exclude: Iterable[str] = (),
    include_git: bool = False,
) -> str:
    """Return a Unicode tree with a trailing newline.

    Root depth is 0. max_entries counts files/directories/links, excluding root;
    0 disables that limit. Depth/entry omissions are explicitly marked.
    Files and links precede directories; names are sorted within each group.
    Nested symlinks and Windows junctions are displayed, not traversed.
    Filesystem errors propagate: an unreadable ignore file is never ignored.
    """
    if not isinstance(skip_gitignore, bool):
        raise TypeError('skip_gitignore must be a bool')
    if max_depth is not None and max_depth < 0:
        raise ValueError('max_depth must be >= 0 or None')
    if max_entries < 0:
        raise ValueError('max_entries must be >= 0')
    patterns = tuple(exclude)
    if GitIgnoreSpec is None and (not skip_gitignore or patterns):
        raise RuntimeError('Missing dependency. Run: python3 -m pip install pathspec==1.1.1')

    root = Path(directory).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(f'Not a directory: {root}')
    label = display_name(root.name) + '/' if root.name else display_name(str(root))
    lines = ['Directory structure:', f'└── {label}']
    if max_depth == 0:
        lines[-1] += ' [not scanned: max-depth=0]'
        return '\n'.join(lines) + '\n'

    inherited, root_ignored = ((), False) if skip_gitignore else ancestor_rules(root)
    if root_ignored:
        lines[-1] += ' [ignored by ancestor .gitignore; use --skip-gitignore]'
        return '\n'.join(lines) + '\n'
    extra = (compile_rules(root, patterns),) if patterns else ()
    is_junction = getattr(os.path, 'isjunction', lambda _path: False)

    def children(parent: Path, rules: tuple[IgnoreRules, ...]) -> tuple[list[Entry], tuple[IgnoreRules, ...]]:
        if not skip_gitignore:
            rules = load_rules(parent, rules)
        entries: list[Entry] = []
        with os.scandir(parent) as scanned:
            for item in scanned:
                if item.name == '.git' and not include_git:
                    continue
                path = parent / item.name
                is_link = item.is_symlink() or is_junction(path)
                is_dir = not is_link and item.is_dir(follow_symlinks=False)
                if ignored(path, is_dir, extra):
                    continue
                if ignored(path, is_dir, rules):
                    continue
                entries.append(Entry(path, is_dir, is_link))
        entries.sort(key=lambda e: (e.is_dir, e.path.name.casefold(), e.path.name))
        return entries, rules

    # Explicit DFS stack avoids Python's recursion limit on deeply nested trees.
    first, rules = children(root, inherited)
    Frame = tuple[Iterator[tuple[int, Entry]], int, str, int, tuple[IgnoreRules, ...]]
    stack: list[Frame] = [(iter(enumerate(first)), len(first), '    ', 1, rules)]
    count = 0
    while stack:
        iterator, total, prefix, depth, rules = stack[-1]
        try:
            index, entry = next(iterator)
        except StopIteration:
            stack.pop()
            continue
        if max_entries and count >= max_entries:
            lines.extend(['', f'[Truncated: max-entries={max_entries}; remaining branches are not shown.]'])
            break

        last = index == total - 1
        name = display_name(entry.path.name)
        at_depth_limit = entry.is_dir and max_depth is not None and depth >= max_depth
        if entry.is_link:
            name += '@ -> ' + display_name(os.readlink(entry.path))
        elif entry.is_dir:
            name += '/'
            if at_depth_limit:
                name += f' [not scanned: max-depth={max_depth}]'
        lines.append(prefix + ('└── ' if last else '├── ') + name)
        count += 1
        if entry.is_dir and not at_depth_limit:
            nested, nested_rules = children(entry.path, rules)
            nested_prefix = prefix + ('    ' if last else '│   ')
            stack.append((iter(enumerate(nested)), len(nested), nested_prefix, depth + 1, nested_rules))
    return '\n'.join(lines) + '\n'


def parse_bool(value: str) -> bool:
    if value.lower() in {'true', '1', 'yes'}:
        return True
    if value.lower() in {'false', '0', 'no'}:
        return False
    raise argparse.ArgumentTypeError('expected true or false')


def nonnegative(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('expected an integer') from None
    if number < 0:
        raise argparse.ArgumentTypeError('must be >= 0')
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Print an AI-friendly directory tree.')
    parser.add_argument('directory', nargs='?', default='.', help='directory to inspect (default: .)')
    parser.add_argument('--skip-gitignore', '--skip_gitignore', nargs='?', const=True,
                        default=False, type=parse_bool, metavar='BOOL',
                        help='bypass .gitignore rules (default: false); bare flag means true')
    parser.add_argument('--max-depth', type=nonnegative, default=None,
                        help='maximum depth: root=0, direct children=1 (default: unlimited)')
    parser.add_argument('--max-entries', type=nonnegative, default=2000,
                        help='maximum entries excluding root; 0=unlimited (default: 2000)')
    parser.add_argument('--exclude', action='append', default=[], metavar='PATTERN',
                        help='additional gitignore-style filter relative to selected root; repeatable')
    parser.add_argument('--include-git', action='store_true',
                        help='allow .git entries, which are otherwise hidden independently of .gitignore')
    parser.add_argument('-o', '--output', type=Path, help='write UTF-8 text to this file instead of stdout')
    args = parser.parse_args(argv)
    try:
        text = build_tree(args.directory, skip_gitignore=args.skip_gitignore,
                          max_depth=args.max_depth, max_entries=args.max_entries,
                          exclude=args.exclude, include_git=args.include_git)
        if args.output is not None:
            args.output.expanduser().write_text(text, encoding='utf-8')
        else:
            # Preserve the Unicode tree even when redirected under a legacy locale.
            if hasattr(sys.stdout, 'reconfigure'):
                sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
            sys.stdout.write(text)
            sys.stdout.flush()
    except BrokenPipeError:
        # Normal when piping to a consumer that stops early (e.g. head).
        sys.stdout = open(os.devnull, 'w')
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'directory-tree: error: {display_name(str(exc))}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
