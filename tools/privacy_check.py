"""Fail closed when the public showcase contains private values or artifacts.

The checker scans the working tree and, when Git metadata is available, every
reachable blob/path, commit identity/message, and annotated-tag identity/message.
Findings identify only a file, object, or metadata field; matched content is
never emitted.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".playwright-cli",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vercel",
    "__pycache__",
}
SKIP_FILES = {".coverage"}
ALLOWED_BINARY_SHA256 = {
    PurePosixPath("site/assets/hero-biometric-workbench.jpg"): (
        "71bd64d3626cd566d7fb545b11b68673e33202479fd2259fa6bccf4a7da8c542"
    )
}
ALLOWED_FRAME_SHA256 = {
    PurePosixPath("evidence/STATIC_TRACE.md"): {
        "2c31d13bf2c23e7f742f63ce3cacd6ad4f38828570c7de2a1cdd2a8066b1eaff"
    }
}
FORBIDDEN_DIR_NAMES = {
    "backups",
    "data",
    "exports",
    "logs",
    "private",
    "secrets",
}
FORBIDDEN_SUFFIXES = {
    ".backup",
    ".bak",
    ".cer",
    ".crt",
    ".csv",
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".der",
    ".env",
    ".gpx",
    ".jks",
    ".key",
    ".keystore",
    ".log",
    ".p12",
    ".pem",
    ".pfx",
    ".sqlite",
    ".sqlite3",
    ".tsv",
    ".xls",
    ".xlsm",
    ".xlsx",
    ".xml",
    ".zip",
}
FORBIDDEN_NAMES = {
    ".mcp.json",
    "athlete_profile.md",
    "credentials.json",
    "openclaw.json",
    "operations.md",
    "production_readiness.md",
    "secrets.json",
}

APPLE_CHARACTERISTIC_PREFIX = "HKCharacteristicTypeIdentifier"
APPLE_DEMOGRAPHIC_FIELDS = "(?:DateOfBirth|BiologicalSex)"
TEXT_PATTERNS = (
    (
        "absolute-home-path",
        re.compile(
            r"(?i)(?:/Users/[A-Za-z0-9._-]+(?:/|$)|"
            r"/home/[A-Za-z0-9._-]+(?:/|$)|"
            r"[A-Z]:[\\/]Users[\\/][^\\/\s]+(?:[\\/]|$))"
        ),
    ),
    (
        "apple-health-demographics",
        re.compile(
            rf"(?:<{re.escape('M' + 'e')}\b[^>]*{APPLE_CHARACTERISTIC_PREFIX}"
            rf"{APPLE_DEMOGRAPHIC_FIELDS}|"
            rf"{APPLE_CHARACTERISTIC_PREFIX}{APPLE_DEMOGRAPHIC_FIELDS}\s*=)",
            re.IGNORECASE,
        ),
    ),
    (
        "date-of-birth-value",
        re.compile(
            r"(?im)^(?!\s*(?:#|//))[^\n]*"
            r"(?<![A-Za-z0-9])(?:date[_ -]?of[_ -]?birth|birth[_ -]?date|dob)"
            r"(?![A-Za-z0-9])"
            r"\s*[:=]\s*[\"']?(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b"
        ),
    ),
    (
        "biological-sex-value",
        re.compile(
            r"(?im)^(?!\s*(?:#|//))[^\n]*\bbiological[_ -]?sex\b"
            r"\s*[:=]\s*[\"']?"
            r"(?:female|male|other|not[_ -]?set|unknown)\b"
        ),
    ),
    (
        "hardware-serial",
        re.compile(r"\bWHOOP\s+(?=[A-Z0-9]{8,}\b)(?=[A-Z0-9]*\d)[A-Z0-9]+\b"),
    ),
    (
        "mac-address",
        re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}:){5}[0-9a-f]{2}(?![0-9a-f])"),
    ),
    (
        "private-network-address",
        re.compile(
            r"(?<!\d)(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
            r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?!\d)"
        ),
    ),
)
SECRET_PATTERNS = (
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("groq-key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}\b")),
    ("google-api-key", re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("supabase-token", re.compile(r"\bsbp_[A-Za-z0-9]{20,}\b")),
    ("vercel-token", re.compile(r"\bvercel_[A-Za-z0-9_-]{20,}\b")),
    (
        "private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "telegram-bot-token",
        re.compile(r"(?<!\d)\d{8,10}:[A-Za-z0-9_-]{30,}\b"),
    ),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
            r"[A-Za-z0-9_-]{10,}\b"
        ),
    ),
    (
        "credentialed-url",
        re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/@\s]+:[^/@\s]+@"),
    ),
    ("bearer-token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    (
        "assigned-secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|bot[_-]?token|"
            r"client[_-]?secret|password|passwd)\b\s*[:=]\s*[\"']?"
            r"(?!\$\{|<|your-|example|placeholder|test|dummy|none|null)"
            r"[A-Za-z0-9_./+=-]{16,}"
        ),
    ),
)
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
GITHUB_NOREPLY_EMAIL = re.compile(
    r"^(?:noreply@github\.com|[^@\s]+@users\.noreply\.github\.com)$", re.IGNORECASE
)
PUBLIC_GIT_IDENTITY_NAME = re.compile(r"^(?:GitHub|[A-Za-z0-9][A-Za-z0-9._-]{0,63}(?:\[bot\])?)$")
LONG_FRAME_LITERAL = re.compile(r"(?i)(?<![0-9a-f])aa[0-9a-f]{38,}(?![0-9a-f])")
EPOCH_LITERAL = re.compile(r"(?<!\d)(?:[1-9]\d{9}|[1-9]\d{12})(?!\d)")
SYNTHETIC_EPOCH_START_SECONDS = 2_051_222_400  # 2035-01-01T00:00:00Z


def _normalized_path(path: str | Path | PurePosixPath) -> PurePosixPath:
    return PurePosixPath(str(path).replace("\\", "/"))


def _path_failure_categories(path: str | Path | PurePosixPath) -> set[str]:
    relative = _normalized_path(path)
    name = relative.name.lower()
    parts = {part.lower() for part in relative.parts}
    failures: set[str] = set()
    if parts & FORBIDDEN_DIR_NAMES:
        failures.add("private-directory")
    if name in FORBIDDEN_NAMES:
        failures.add("forbidden-name")
    if name.startswith(".env") and name != ".env.example":
        failures.add("environment-file")
    if name.endswith("~") or any(name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        failures.add("forbidden-suffix")
    return failures


def _text_failure_categories(text: str, path: PurePosixPath) -> set[str]:
    failures = {category for category, pattern in TEXT_PATTERNS if pattern.search(text)}
    failures.update(category for category, pattern in SECRET_PATTERNS if pattern.search(text))
    if any(
        not GITHUB_NOREPLY_EMAIL.fullmatch(match.group(0)) for match in EMAIL_PATTERN.finditer(text)
    ):
        failures.add("email-address")
    if path.name != "uv.lock":
        for match in EPOCH_LITERAL.finditer(text):
            literal = match.group(0)
            epoch_seconds = int(literal) / (1000 if len(literal) == 13 else 1)
            if 946_684_800 <= epoch_seconds < SYNTHETIC_EPOCH_START_SECONDS:
                failures.add("non-synthetic-epoch-timestamp")
                break
    allowed_frames = ALLOWED_FRAME_SHA256.get(path, set())
    for match in LONG_FRAME_LITERAL.finditer(text):
        candidate = match.group(0).lower()
        if not _looks_like_gen4_frame(candidate):
            continue
        digest = hashlib.sha256(candidate.encode("ascii")).hexdigest()
        if digest not in allowed_frames:
            failures.add("captured-frame-literal")
    return failures


def _looks_like_gen4_frame(candidate: str) -> bool:
    """Distinguish framed packets from lockfile hashes that happen to start ``aa``."""
    if len(candidate) % 2:
        return False
    try:
        raw = bytes.fromhex(candidate)
    except ValueError:
        return False
    if len(raw) < 12 or raw[0] != 0xAA:
        return False
    declared_length = int.from_bytes(raw[1:3], "little")
    frame_length = declared_length + 4
    return 12 <= frame_length <= len(raw)


def _content_failure_categories(data: bytes, path: PurePosixPath) -> set[str]:
    if expected_hash := ALLOWED_BINARY_SHA256.get(path):
        actual_hash = hashlib.sha256(data).hexdigest()
        return set() if actual_hash == expected_hash else {"reviewed-binary-changed"}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"unreviewed-binary"}
    return _text_failure_categories(text, path)


def iter_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.name not in SKIP_FILES
        and not SKIP_DIRS.intersection(path.relative_to(root).parts)
    )


def scan_worktree(root: Path) -> set[str]:
    failures: set[str] = set()
    for path in iter_files(root):
        relative = _normalized_path(path.relative_to(root))
        path_categories = _path_failure_categories(relative)
        path_categories.update(
            _text_failure_categories(str(relative), PurePosixPath(".git/path-metadata"))
        )
        categories = set(path_categories)
        try:
            data = path.read_bytes()
        except OSError:
            categories.add("unreadable-file")
        else:
            categories.update(_content_failure_categories(data, relative))
        identifier = str(relative)
        if path_categories:
            path_digest = hashlib.sha256(str(relative).encode("utf-8")).hexdigest()[:12]
            identifier = f"path-{path_digest}"
        for category in categories:
            failures.add(f"worktree:{identifier}:{category}")
    return failures


def _git(root: Path, *args: str, input_data: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        input=input_data,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError("Git privacy inspection failed")
    return result.stdout


def _reachable_blob_paths(root: Path) -> list[tuple[str, PurePosixPath]]:
    commits = _git(root, "rev-list", "--all").decode("ascii", errors="replace").splitlines()
    blob_paths: set[tuple[str, PurePosixPath]] = set()
    for commit_id in commits:
        tree = _git(root, "ls-tree", "-r", "-z", "--full-tree", commit_id)
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            metadata, separator, raw_path = entry.partition(b"\t")
            fields = metadata.split(b" ", 2)
            if not separator or len(fields) != 3 or fields[1] != b"blob":
                raise RuntimeError("Git tree privacy inspection failed")
            object_id = fields[2].decode("ascii", errors="strict")
            path = _normalized_path(raw_path.decode("utf-8", errors="replace"))
            blob_paths.add((object_id, path))
    return sorted(blob_paths, key=lambda item: (str(item[1]), item[0]))


def _identity_failure_categories(name: str) -> set[str]:
    failures = _text_failure_categories(name, PurePosixPath(".git/identity-metadata"))
    if not PUBLIC_GIT_IDENTITY_NAME.fullmatch(name):
        failures.add("non-handle-identity-name")
    return failures


def _scan_commit_metadata(root: Path, commit_ids: list[str]) -> set[str]:
    failures: set[str] = set()
    for commit_id in commit_ids:
        raw = _git(
            root,
            "show",
            "-s",
            "--format=%an%x00%ae%x00%cn%x00%ce%x00%B",
            commit_id,
        ).decode("utf-8", errors="replace")
        fields = raw.split("\0", 4)
        if len(fields) != 5:
            failures.add(f"git-metadata:{commit_id[:12]}:unparseable-commit")
            continue
        author_name, author_email, committer_name, committer_email, message = fields
        for category in _identity_failure_categories(author_name):
            failures.add(f"git-metadata:{commit_id[:12]}:author-name:{category}")
        for category in _identity_failure_categories(committer_name):
            failures.add(f"git-metadata:{commit_id[:12]}:committer-name:{category}")
        if not GITHUB_NOREPLY_EMAIL.fullmatch(author_email):
            failures.add(f"git-metadata:{commit_id[:12]}:author-email")
        if not GITHUB_NOREPLY_EMAIL.fullmatch(committer_email):
            failures.add(f"git-metadata:{commit_id[:12]}:committer-email")
        for category in _text_failure_categories(message, PurePosixPath(".git/commit-message")):
            failures.add(f"git-metadata:{commit_id[:12]}:commit-message:{category}")
    return failures


def _scan_tag_metadata(root: Path) -> set[str]:
    failures: set[str] = set()
    refs = _git(
        root,
        "for-each-ref",
        "--format=%(refname)%00%(objecttype)%00%(objectname)",
        "refs/tags",
    ).decode("utf-8", errors="replace")
    for line in refs.splitlines():
        fields = line.split("\0")
        if len(fields) != 3:
            failures.add("git-tag-metadata:unparseable-ref")
            continue
        ref_name, object_type, object_id = fields
        short_id = object_id[:12]
        for category in _text_failure_categories(ref_name, PurePosixPath(".git/tag-ref")):
            failures.add(f"git-tag-metadata:{short_id}:ref:{category}")
        if object_type != "tag":
            continue

        raw = _git(root, "cat-file", "tag", object_id).decode("utf-8", errors="replace")
        headers, separator, message = raw.partition("\n\n")
        tagger_lines = [line for line in headers.splitlines() if line.startswith("tagger ")]
        if not separator or len(tagger_lines) != 1:
            failures.add(f"git-tag-metadata:{short_id}:unparseable-tag")
            continue
        match = re.fullmatch(r"tagger (.*) <([^<>]+)> \d+ [+-]\d{4}", tagger_lines[0])
        if not match:
            failures.add(f"git-tag-metadata:{short_id}:unparseable-tagger")
            continue
        tagger_name, tagger_email = match.groups()
        for category in _identity_failure_categories(tagger_name):
            failures.add(f"git-tag-metadata:{short_id}:tagger-name:{category}")
        if not GITHUB_NOREPLY_EMAIL.fullmatch(tagger_email):
            failures.add(f"git-tag-metadata:{short_id}:tagger-email")
        for category in _text_failure_categories(message, PurePosixPath(".git/tag-message")):
            failures.add(f"git-tag-metadata:{short_id}:tag-message:{category}")
    return failures


def scan_git_history(root: Path) -> set[str]:
    if not (root / ".git").exists():
        return set()

    failures: set[str] = set()
    try:
        commit_ids = _git(root, "rev-list", "--all").decode("ascii", errors="replace").splitlines()
        for object_id, path in _reachable_blob_paths(root):
            path_categories = _path_failure_categories(path)
            path_categories.update(
                _text_failure_categories(str(path), PurePosixPath(".git/path-metadata"))
            )
            categories = set(path_categories)
            data = _git(root, "cat-file", "blob", object_id)
            categories.update(_content_failure_categories(data, path))
            identifier = str(path)
            if path_categories:
                path_digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
                identifier = f"path-{path_digest}"
            for category in categories:
                failures.add(f"history:{object_id[:12]}:{identifier}:{category}")
        failures.update(_scan_commit_metadata(root, commit_ids))
        failures.update(_scan_tag_metadata(root))
    except RuntimeError:
        failures.add("git-history:inspection-failed")
    return failures


def scan_repository(root: Path) -> set[str]:
    return scan_worktree(root) | scan_git_history(root)


def main() -> int:
    failures = scan_repository(ROOT)
    if failures:
        print("Privacy check failed in:")
        for identifier in sorted(failures):
            print(identifier)
        return 1
    print(f"Privacy check passed ({len(iter_files(ROOT))} working-tree files scanned).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
