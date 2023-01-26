#!/usr/bin/env python3
import os
import sys
import time
import urllib.parse

import jwt
from git import Repo
from git.util import Actor
from github import Github
import requests
from yaml import safe_load, safe_dump


CHECK_SLEEP = 10
MAX_PR_WAIT = 60
MAIN_BRANCH = "main"
AUTHOR_NAME = "Automated Update"
AUTHOR_EMAIL = "infrastructure@profian.com"
REQUIRED_CHECKS_PR = [
    "clusters / services"
]
REQUIRED_CHECKS_DEPLOY = [
]


def get_gh_full_name(repo):
    return urllib.parse.urlparse(repo.remote().url).path[1:].removesuffix(".git")


def get_pr_head(pr):
    return pr.get_commits().reversed[0]


def create_branch(repo, environment, service, new_version):
    branch_name = f"upgrade-{service}-{new_version}-{environment}".replace(":", "-").replace("@", "-")
    print(f"Creating branch {branch_name}", flush=True)

    new_branch = repo.create_head(branch_name)
    new_branch.checkout()

    return branch_name


def update_file(environment, service, image_tag):
    filename = f"apps/{environment}/{service}/patch-deployment.yaml"

    with open(filename) as f:
        contents = safe_load(f)

    print(f"Old file: {contents}", flush=True)

    contents["spec"]["template"]["spec"]["containers"][0]["image"] = image_tag

    print(f"New file: {contents}", flush=True)

    with open(filename, "w") as f:
        safe_dump(contents, f)

    return filename


def commit(repo, filename, environment, service, new_version):
    repo.index.add([filename])

    diff = repo.head.commit.diff()
    if len(diff) == 0:
        print("No changes, skipping commit", flush=True)
        return False

    repo.index.commit(
        create_body(service, new_version, environment),
        author=get_author(),
        committer=get_author(),
    )

    print(f"Commit created: {repo.head.commit}", flush=True)

    return True


def push(repo, branch_name):
    origin = repo.remote("origin")
    origin.push(branch_name).raise_if_error()

    print("Pushed branch to GitHub", flush=True)


def get_author():
    return Actor(AUTHOR_NAME, AUTHOR_EMAIL)


def create_body(service, new_version, environment):
    return (f"Upgrade {service} to {new_version} in {environment}\n"
            "\n"
            "This PR was automatically created by the upgrade workflow.\n"
            "\n"
            f"Signed-off-by: {AUTHOR_NAME} <{AUTHOR_EMAIL}>")


def create_pr(gh_repo, branchname, environment, service, new_version):
    pr = gh_repo.create_pull(
        title=f"Upgrade {service} to {new_version} in {environment}",
        body=create_body(service, new_version, environment),
        head=branchname,
        base=MAIN_BRANCH,
    )

    print(f"Created pull request {pr}", flush=True)

    return pr


def wait_for_commit_checks(commit, required_checks):
    print("Waiting for commit checks to complete", flush=True)

    start_time = time.time()

    while True:
        if time.time() - start_time >= MAX_PR_WAIT:
            raise Exception(f"Timed out waiting for PR checks ({MAX_PR_WAIT} seconds)")

        print(f"Waiting {CHECK_SLEEP} seconds before checking...", flush=True)
        time.sleep(CHECK_SLEEP)

        required_pending = set(required_checks)

        any_pending = False

        check_runs = commit.get_check_runs()
        for run in check_runs:
            if run.status != "completed":
                any_pending = True
                print(f"Check run {run} pending", flush=True)
                continue
            if run.conclusion != "success":
                raise Exception(f"Check run {run} failed")
            if run.name in required_pending:
                required_pending.remove(run.name)

        statuses = commit.get_statuses()
        for status in statuses:
            if status.state == "pending":
                any_pending = True
                print(f"Status {status.context} pending", flush=True)
                continue
            if status.state != "success":
                raise Exception(f"Status {status.context} failed")

        if any_pending:
            print("Check runs pending, waiting", flush=True)
            continue

        if required_pending:
            print(f"Required checks {required_pending} not yet scheduled", flush=True)
            continue

        break

    print("All checks passed", flush=True)


def merge_pr(pr, github, branchname):
    print("Merging PR", flush=True)
    result = pr.merge()
    if not result.merged:
        raise Exception(f"PR merge failed: {result}")

    git_ref = github.get_git_ref(f"heads/{branchname}")
    git_ref.delete()


def perform_environment(repo, github, environment, service, new_version, image_tag):
    print(f"Updating {environment}", flush=True)

    branchname = create_branch(repo, environment, service, new_version)
    fname = update_file(environment, service, image_tag)
    if not commit(repo, fname, environment, service, new_version):
        # This method returns True if there were changes, and False if there were
        # no changes. If there were no changes, this environment is already
        # running the latest version, so we don't need to push the branch
        # to GitHub.
        return
    push(repo, branchname)
    pr = create_pr(github, branchname, environment, service, new_version)
    pr_head = get_pr_head(pr)
    wait_for_commit_checks(pr_head, REQUIRED_CHECKS_PR)
    if environment == "production":
        print("Not merging PR to production", flush=True)
        return
    merge_pr(pr, github, branchname)

    main_git_ref = github.get_git_ref(f"heads/{MAIN_BRANCH}")
    main_commit = github.get_commit(main_git_ref.object.sha)
    print("Waiting for main branch checks to complete", flush=True)
    wait_for_commit_checks(main_commit, REQUIRED_CHECKS_DEPLOY)
    print(f"Environment {environment} updated succesfully", flush=True)


def get_github_token():
    app_id = os.environ["UPDATER_APP_ID"]
    private_key = os.environ["UPDATER_APP_PRIVATE_KEY"]
    install_id = os.environ["UPDATER_APP_INSTALL_ID"]

    print(f"Getting a GitHub token for app {app_id} (install {install_id})", flush=True)

    signing_key = jwt.jwk_from_pem(private_key.encode("utf-8"))
    payload = {
        "iat": int(time.time()),
        "exp": int(time.time()) + 30,
        "iss": int(app_id),
    }
    jwt_instance = jwt.JWT()
    jwtoken = jwt_instance.encode(payload, signing_key, alg="RS256")

    resp = requests.post(
        f"https://api.github.com/app/installations/{install_id}/access_tokens",
        headers={
            "Accept": "aplication/vnd.github+json",
            "Authorization": f"Bearer {jwtoken}",
        }
    )
    resp.raise_for_status()
    resp = resp.json()

    token = resp.pop("token")

    print(f"GitHub Token expires_at: {resp['expires_at']}, permissions: {resp['permissions']}", flush=True)

    return token


def main():
    if len(sys.argv) != 4:
        print("Usage: upgrade.py <service> <git_ref> <image_digest>")
        sys.exit(1)
    service, git_ref, image_digest = sys.argv[1:]

    if git_ref.startswith('refs/tags/'):
        new_version = git_ref[10:].lstrip("v")
        print(f"Updating {service} to released version {new_version}", flush=True)
        image_tag = f"ghcr.io/profianinc/{service}:{new_version}"
        if "-rc" in new_version:
            print("Release candidate found, upgrading testing and staging", flush=True)
            highest_environment = "staging"
        else:
            print("Production release found, upgrading all environments", flush=True)
            highest_environment = "production"
    else:
        new_version = f"digest:{image_digest}"
        print(f"Updating {service} to image digest {image_digest} in testing", flush=True)
        image_tag = f"ghcr.io/profianinc/{service}@{image_digest}"
        highest_environment = "testing"

    if highest_environment == "testing":
        envs = ["testing"]
    elif highest_environment == "staging":
        envs = ["testing", "staging"]
    elif highest_environment == "production":
        envs = ["testing", "staging", "production"]
    else:
        raise Exception(f"Invalid highest environment: {highest_environment}")

    print(f"Upgrading {service} to {image_tag} in envs {envs}", flush=True)

    repo = Repo(".")
    start_branch = repo.head.ref

    repo_name = get_gh_full_name(repo)

    github = Github(get_github_token())
    github = github.get_repo(repo_name)
    print(f"GitHub repo: {github}", flush=True)

    for env in envs:
        start_branch.checkout()
        origin = repo.remote("origin").pull()

        perform_environment(repo, github, env, service, new_version, image_tag)


if __name__ == "__main__":
    main()
