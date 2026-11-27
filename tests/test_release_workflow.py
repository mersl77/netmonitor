"""Regrese L30/L30b: build z cílového commitu a zveřejnění kompletního konceptu."""
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent.parent


def test_build_checks_out_prepared_release_sha():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    build = re.search(r"^  build:\n.*?(?=^  [\w-]+:|\Z)", workflow, re.M | re.S)
    assert build is not None, "Chybí job build"

    checkout = re.search(
        r"^      - uses: actions/checkout@v5\n(?P<options>(?:        .*\n)*)",
        build.group(),
        re.M,
    )
    assert checkout is not None, "V jobu build chybí checkout"
    assert "        with:\n          ref: ${{ needs.prepare.outputs.sha }}\n" in checkout.group("options"), (
        "Checkout v jobu build musí použít commit z jobu prepare i pro koncept bez tagu"
    )


def _job(name: str) -> str:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    job = re.search(rf"^  {name}:\n.*?(?=^  [\w-]+:|\Z)", workflow, re.M | re.S)
    assert job is not None, f"Chybí job {name}"
    return job.group()


def test_prepare_creates_draft_release_targeting_push_sha_without_creating_tag():
    prepare = _job("prepare")
    create = re.search(r'gh release create "\$TAG"[^\n]*', prepare)
    assert create is not None
    assert "--draft" in create.group()
    assert '--target "$GITHUB_SHA"' in create.group()
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "git/refs" not in workflow, "Tag se má vytvořit až při zveřejnění přes Releases API"


def test_prepare_outputs_nonempty_build_sha_from_tag_or_draft_target():
    prepare = _job("prepare")
    assert "      sha: ${{ steps.v.outputs.sha }}\n" in prepare
    assert 'SHA=$(gh api "repos/$GITHUB_REPOSITORY/commits/$TAG" --jq \'.sha\')' in prepare
    assert '--json targetCommitish --jq \'.targetCommitish\'' in prepare
    assert 'SHA=$(gh api "repos/$GITHUB_REPOSITORY/commits/$TARGET" --jq \'.sha\')' in prepare
    assert 'SHA="$GITHUB_SHA"' in prepare
    outputs = prepare.index('echo "sha=$SHA"')
    assert re.search(r'if \[ -z "\$SHA" \]; then\n.*?exit 1', prepare[:outputs], re.S)


def test_prepare_rejects_existing_tag_on_different_commit():
    prepare = _job("prepare")
    assert 'gh api "repos/$GITHUB_REPOSITORY/git/ref/tags/$TAG"' in prepare
    # Endpoint commits rozbalí i anotovaný tag na commit.
    assert 'gh api "repos/$GITHUB_REPOSITORY/commits/$TAG" --jq \'.sha\'' in prepare
    assert re.search(r'if .*\[ "\$TAG_SHA" != "\$GITHUB_SHA" \]; then\n.*?exit 1', prepare, re.S)
    assert "zkontrolujte tag, případně spusťte Actions → Build & Release → Run workflow s tagem" in prepare


def test_prepare_retries_publish_of_complete_draft():
    prepare = _job("prepare")
    assert '--json isDraft --jq \'.isDraft\'' in prepare
    assert 'if [ -z "$MISSING" ] && [ "$IS_DRAFT" != "true" ]; then' in prepare
    assert 'echo "missing=$MISSING"' in prepare
    assert 'BUILD=true' in prepare
    assert 'BUILD=false' in prepare
    assert 'echo "build=$BUILD"' in prepare
    assert 'echo "sha=$SHA"' in prepare


def test_prepare_creates_missing_release_for_tag_and_manual_runs():
    prepare = _job("prepare")
    # Tagový i ruční běh vyberou existující tag a koncept připnou k jeho commitu.
    assert 'TAG="$INPUT_TAG"' in prepare
    assert 'TAG="$REF_NAME"' in prepare
    assert 'SHA="$TAG_SHA"' in prepare
    assert re.search(r'elif \[ "\$NEW_VERSION" != "true" \]; then\n.*?exit 1', prepare, re.S)
    assert re.search(
        r'VER="\$\{TAG#v\}".*?python3 scripts/changelog_section.py "\$VER".*?'
        r'gh release create "\$TAG"[^\n]*--draft[^\n]*--target "\$SHA"',
        prepare,
        re.S,
    )


def test_asset_checks_use_here_strings_with_pipefail():
    for name in ("prepare", "publish"):
        job = _job(name)
        assert 'echo "$NAMES" |' not in job
        checks = re.findall(r'^\s+grep -q -- .+ <<<"\$NAMES"', job, re.M)
        assert len(checks) == 4, f"{name} musí kontrolovat všechny platformy bez SIGPIPE"


def test_publish_verifies_all_platforms_before_publishing():
    publish = _job("publish")
    assert "    needs: [prepare, build]\n" in publish
    assert "    if: needs.prepare.outputs.build == 'true'\n" in publish
    assert "always()" not in publish
    edit = publish.index('gh release edit "$TAG"')
    for pattern in (
        r"-setup\.exe$", r"linux-x86_64\.tar\.gz$",
        r"macos-arm64\.dmg$", r"macos-x86_64\.dmg$",
    ):
        assert publish.index(pattern) < edit
    assert re.search(r'if \[ -n "\$MISSING" \]; then\n.*?exit 1', publish, re.S)
    assert '--json isDraft --jq \'.isDraft\'' in publish
    assert 'if [ "$IS_DRAFT" = "true" ]; then' in publish
    assert '--draft=false' in publish


def test_release_runs_are_serialized():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert re.search(
        r"^concurrency:\n  group: release\n  cancel-in-progress: false\n", workflow, re.M,
    )
