import subprocess
from pathlib import PurePosixPath

from tools import privacy_check


def _run_git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def _init_repo(repo, email):
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.name", "synthetic-fixture")
    _run_git(repo, "config", "user.email", email)
    _run_git(repo, "config", "commit.gpgsign", "false")


def _commit_all(repo, message):
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", message)


def test_text_scan_detects_generic_private_values():
    apple_field = "HKCharacteristicTypeIdentifier" + "DateOfBirth"
    private_home = "/" + "Users/" + "fixture/account.json"
    secret = "sk-" + ("A" * 24)
    personal_email = "person" + "@" + "example.invalid"
    text = "\n".join(
        (
            private_home,
            "<" + "Me " + apple_field + '="1980-01-02"/>',
            secret,
            personal_email,
        )
    )

    categories = privacy_check._text_failure_categories(text, PurePosixPath("fixture.txt"))

    assert categories >= {
        "absolute-home-path",
        "apple-health-demographics",
        "email-address",
        "openai-style-key",
    }


def test_text_scan_detects_environment_style_dob_assignment():
    synthetic_date = "-".join(("2000", "06", "15"))
    text = f'DOOP_ATHLETE_DOB="{synthetic_date}"'

    categories = privacy_check._text_failure_categories(text, PurePosixPath("config.txt"))

    assert "date-of-birth-value" in categories


def test_text_scan_detects_past_epoch_seconds_and_milliseconds():
    past_seconds_value = int("17" + ("0" * 8))
    past_seconds = str(past_seconds_value)
    past_milliseconds = str((past_seconds_value * 1000) + 123)

    for literal in (past_seconds, past_milliseconds):
        categories = privacy_check._text_failure_categories(
            f"timestamp = {literal}", PurePosixPath("fixture.py")
        )
        assert "non-synthetic-epoch-timestamp" in categories


def test_text_scan_allows_future_synthetic_epoch_timestamp():
    future_milliseconds = str(2_300_000_000_123)

    categories = privacy_check._text_failure_categories(
        f"timestamp = {future_milliseconds}", PurePosixPath("fixture.py")
    )

    assert "non-synthetic-epoch-timestamp" not in categories


def test_text_scan_ignores_epoch_shaped_lockfile_metadata():
    lockfile_epoch_shape = "17" + ("0" * 8)
    categories = privacy_check._text_failure_categories(
        f"generated-size = {lockfile_epoch_shape}", PurePosixPath("uv.lock")
    )

    assert "non-synthetic-epoch-timestamp" not in categories


def test_worktree_findings_never_echo_matched_content(tmp_path):
    secret = "sk-" + ("B" * 24)
    (tmp_path / "config.txt").write_text(secret)

    failures = privacy_check.scan_worktree(tmp_path)

    assert failures == {"worktree:config.txt:openai-style-key"}
    assert all(secret not in failure for failure in failures)


def test_frame_literals_are_blocked_outside_the_pinned_synthetic_trace():
    frame = (bytes([0xAA, 24, 0, 0]) + bytes(24)).hex()

    categories = privacy_check._text_failure_categories(
        frame, PurePosixPath("evidence/unreviewed.md")
    )

    assert "captured-frame-literal" in categories


def test_reviewed_binary_asset_is_hash_pinned(tmp_path):
    asset = tmp_path / "site" / "assets" / "hero-biometric-workbench.jpg"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"replacement bytes")

    assert privacy_check.scan_worktree(tmp_path) == {
        "worktree:site/assets/hero-biometric-workbench.jpg:reviewed-binary-changed"
    }


def test_history_scan_finds_removed_paths_and_disallowed_metadata(tmp_path):
    noreply = "fixture" + "@users.noreply.github.com"
    _init_repo(tmp_path, noreply)
    (tmp_path / "safe.txt").write_text("synthetic fixture\n")
    _commit_all(tmp_path, "safe root")

    retired_path = tmp_path / ("retired" + ".csv")
    retired_path.write_text("synthetic,row\n")
    retired_content_path = tmp_path / "retired.txt"
    private_home = "/" + "home/" + "fixture/private.txt"
    retired_content_path.write_text(private_home)
    _commit_all(tmp_path, "add retired fixture")
    retired_path.unlink()
    retired_content_path.unlink()
    _commit_all(tmp_path, "remove retired fixture")

    personal_email = "person" + "@" + "example.invalid"
    _run_git(tmp_path, "config", "user.email", personal_email)
    (tmp_path / "safe.txt").write_text("synthetic fixture updated\n")
    _commit_all(tmp_path, "metadata fixture")

    failures = privacy_check.scan_git_history(tmp_path)

    assert any(
        failure.startswith("history:") and failure.endswith(":forbidden-suffix")
        for failure in failures
    )
    assert any(
        failure.startswith("history:")
        and "retired.txt" in failure
        and failure.endswith(":absolute-home-path")
        for failure in failures
    )
    assert any(failure.endswith(":author-email") for failure in failures)
    assert any(failure.endswith(":committer-email") for failure in failures)
    assert all(personal_email not in failure for failure in failures)


def test_history_scan_allows_github_noreply_metadata(tmp_path):
    noreply = "12345+fixture" + "@users.noreply.github.com"
    _init_repo(tmp_path, noreply)
    (tmp_path / "safe.txt").write_text("synthetic fixture\n")
    _commit_all(tmp_path, "safe root")

    assert privacy_check.scan_git_history(tmp_path) == set()


def test_history_scan_allows_github_web_noreply_metadata(tmp_path):
    _init_repo(tmp_path, "noreply" + "@github.com")
    (tmp_path / "safe.txt").write_text("synthetic fixture\n")
    _commit_all(tmp_path, "safe web commit")

    assert privacy_check.scan_git_history(tmp_path) == set()


def test_history_scan_checks_path_commit_and_identity_metadata_without_echo(tmp_path):
    noreply = "fixture" + "@users.noreply.github.com"
    _init_repo(tmp_path, noreply)
    secret = "sk-" + ("C" * 24)
    path_email = "person" + "@" + "example.invalid"
    (tmp_path / f"{path_email}.txt").write_text("synthetic fixture\n")
    _commit_all(tmp_path, secret)
    _run_git(tmp_path, "config", "user.name", "Personal Display Name")
    (tmp_path / "next.txt").write_text("synthetic fixture\n")
    _commit_all(tmp_path, "safe content change")

    failures = privacy_check.scan_git_history(tmp_path)

    assert any(failure.endswith(":commit-message:openai-style-key") for failure in failures)
    assert any(failure.endswith(":email-address") for failure in failures)
    assert any(failure.endswith(":author-name:non-handle-identity-name") for failure in failures)
    assert all(secret not in failure and path_email not in failure for failure in failures)


def test_history_scan_checks_annotated_tag_metadata_without_echo(tmp_path):
    noreply = "fixture" + "@users.noreply.github.com"
    _init_repo(tmp_path, noreply)
    (tmp_path / "safe.txt").write_text("synthetic fixture\n")
    _commit_all(tmp_path, "safe root")
    tag_secret = "gsk_" + ("D" * 24)
    personal_email = "person" + "@" + "example.invalid"
    _run_git(tmp_path, "config", "user.email", personal_email)
    _run_git(tmp_path, "tag", "-a", "v0.0.0", "-m", tag_secret)

    failures = privacy_check.scan_git_history(tmp_path)

    assert any(failure.endswith(":tagger-email") for failure in failures)
    assert any(failure.endswith(":tag-message:groq-key") for failure in failures)
    assert all(tag_secret not in failure and personal_email not in failure for failure in failures)
