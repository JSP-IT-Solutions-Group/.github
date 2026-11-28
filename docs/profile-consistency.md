# Profile consistency check

A **read-only** check that the company's GitHub profiles agree with the
Finnish and English JSP-Product™ catalogues. It reports problems in the job
summary and as annotations. It **never edits** a profile or the website, uses
no secrets and needs only `contents: read`.

A separate, opt-in [weekly catalogue sync](#weekly-catalogue-sync) proposes
the fixes as a **pull request**: it updates the counts and a generated list of
all solutions with links to their English product pages. A person reviews and
merges it; nothing is pushed to the default branch or merged automatically.

| Check | What is compared | Outcome when it differs |
|---|---|---|
| Catalogue | Solutions and solution groups in each language, from the page's structured data, cross-checked against the product cards and the visible counter | WARN (the page disagrees with itself) |
| Language parity | English vs Finnish: counts, product ids and group keys | FAIL |
| Profile counts | "N software solutions" / "N solution groups" (or the Finnish "N ratkaisua" / "N ratkaisuryhmässä") in profile READMEs vs the reference (English) catalogue. Text inside the generated solution list is ignored. | FAIL |
| Profile list | When the profile has a generated solution list (between `jsp-catalogue` markers): it lists exactly the catalogue's products | FAIL |
| Profile links | Every link in the profile README: http(s), images, HTML links, reference links, relative files | FAIL for 404/410 or missing files, WARN when it can't be verified (403, 429, 5xx, timeouts) |

Profiles currently checked by the weekly run in this repository:

- `profile/README.md` (organization profile): links only, because it states no counts.
- `JSP-IT/JSP-IT` `README.md` (company profile): counts are **required**, and links are checked too. That repository also runs the check on its own pull requests.

## Data source

The authoritative product data lives in the website's source code, which is
not in these repositories. The tools read what the website **publishes**,
in this order:

1. **`/catalogue.json`** (primary): a machine-readable view of the catalogue
   that the site generates on every request from the same product catalogue as
   the overview page: `https://jsp-it.com/catalogue.json` (English) and
   `https://jsp-it.fi/catalogue.json` (Finnish). Schema version 1:

   ```json
   {
     "version": 1, "language": "en", "count": 102,
     "categories": [{ "key": "automation", "label": "Automation and AI" }],
     "products": [{ "id": "products_ai", "name": "JSP-AI™",
                    "summary": "AI assistants and intelligent information processing",
                    "category": "automation",
                    "url": "https://jsp-it.com/products_show?type=products_ai" }]
   }
   ```

   The tools require `version` 1, `categories[].key` and `products[].id`, and
   ignore unknown fields. A mismatch between `count` and the number of products,
   a product in an undefined group, duplicate ids or the wrong `language` is a
   warning in the check; the sync refuses inconsistent data and falls back.
2. **The HTML overview page** (fallback), used when the JSON request fails, is
   not JSON or has another schema version. The page's structured data is read
   in this order:

| Signal | Used for |
|---|---|
| `<script id="jsp-solution-advisor-catalog" type="application/json">` | **Primary**: the number of unique product `id`s, and product ids for parity |
| `<script id="jsp-product-category-catalog" type="application/json">` | **Primary**: the number of solution groups, and group keys for parity |
| `data-product-category` attributes on product cards | Cross-check, and fallback (with a warning) when the JSON is missing or invalid |
| Visible counter ("102 solutions" / "102 ratkaisua") | Cross-check only |

Using a fallback source is reported as a warning that names the reason, for
example the bot-protection page.

Sources are configured as `|`-separated lists, for example
`--catalogue "en=https://jsp-it.com/catalogue.json|https://jsp-it.com/products_jsp_overview"`.

**Freshness.** Requests send `Cache-Control: no-cache` and `Pragma: no-cache`.
If a response still carries cache indicators (`Age` > 0, `X-Cache: HIT`,
`CF-Cache-Status: HIT`), the summary says so next to the counts. At the time
of writing, the overview pages return `Cache-Control: no-store` and `/catalogue.json` returns
`Cache-Control: public, max-age=3600`, so a catalogue change can take up to an hour to appear in the JSON.

## Outcomes and failure handling

| Situation | Reported as | Exit code | Job fails? |
|---|---|---|---|
| Everything matches | PASS | 0 | no |
| Page signals disagree with each other, structured data fell back to cards, or a link could not be verified | WARN | 0 | no |
| Profile count differs, language parity broken, broken link, or a required count statement missing | FAIL | 1 | yes, unless `enforce: false` |
| A catalogue or remote profile can't be fetched (network error, 5xx, missing structured data with no cards) | WARN "data unavailable", **never** a discrepancy | 3 | only with `fail-on-unavailable: true` |
| The checker itself crashes or is missing | `result=error` | — | yes |

When only one language is unavailable, the other is still compared, and real
discrepancies against it still fail. Fetches retry transient errors (5xx,
429, timeouts) with back-off. 404/410 are not retried.

**When the check fails:**

1. Open the job summary. It shows the catalogue counts, the profile sentence that disagrees, and the broken links.
2. Confirm on the live pages (<https://jsp-it.com/products_jsp_overview>, <https://jsp-it.fi/products_jsp_overview>).
3. Merge the pull request from the [weekly catalogue sync](#weekly-catalogue-sync) if one is open, or update the profile README by hand in a reviewed pull request. The check itself never edits profiles.
4. A parity failure (English and Finnish differ) needs a website fix, not a profile change. Report it to the website maintainers.

**When data is unavailable:** re-run the workflow later. If the site is up
but the structured data is reported missing, the page markup has changed.
Update `PRODUCTS_ISLAND` / `CATEGORIES_ISLAND` in
`profile-consistency/check_profile_consistency.py` and the fixtures, in a
reviewed pull request.

## Usage

In a profile repository (this is the workflow used in `JSP-IT/JSP-IT`):

```yaml
name: Profile consistency
on:
  pull_request:
    paths: ["README.md"]
  schedule:
    - cron: "23 6 * * 1"
  workflow_dispatch:
permissions:
  contents: read
jobs:
  profile-consistency:
    permissions:
      contents: read
    uses: JSP-IT-Solutions-Group/.github/.github/workflows/profile-consistency.yml@profile-consistency-v1
    with:
      counts-profiles: README.md
      tooling-ref: profile-consistency-v1
```

Pin `uses:` and `tooling-ref` to the same tag or commit SHA. Until the first
tag exists, use a commit SHA from `main`.

### Inputs

| Input | Default | Description |
|---|---|---|
| `profiles` | — | Comma-separated READMEs (caller-repo paths or https URLs). Counts are compared only when the profile states them. |
| `counts-profiles` | — | Comma-separated READMEs that **must** state the counts. |
| `catalogue-en-url` | `https://jsp-it.com/catalogue.json\|https://jsp-it.com/products_jsp_overview` | English catalogue source(s), tried in order. |
| `catalogue-fi-url` | `https://jsp-it.fi/catalogue.json\|https://jsp-it.fi/products_jsp_overview` | Finnish catalogue source(s), tried in order. |
| `check-links` | `true` | Check profile links. |
| `fail-on-unavailable` | `false` | Fail when data can't be fetched. |
| `enforce` | `true` | Fail on discrepancies and broken links. |
| `attempts` | `4` | Tries when the catalogue data is unavailable, for example because of the site's bot protection. Discrepancies are never retried. |
| `retry-delay-seconds` | `90` | Wait between attempts. |
| `tooling-ref` | `profile-consistency-v1` | Ref of this repository to load the checker from. |
| `runs-on` | `ubuntu-24.04` | Runner with bash and Python 3.11+. |

Outputs: `result` (`pass`/`warn`/`fail`/`error`), `unavailable`
(`true`/`false`), `solutions` and `groups` (English catalogue).

### Locally

```sh
python3 profile-consistency/check_profile_consistency.py \
  --profile profile/README.md \
  --counts-profile https://raw.githubusercontent.com/JSP-IT/JSP-IT/main/README.md
python3 -m unittest discover -s tests/profile-consistency -v   # synthetic fixtures, no external network
```

## Weekly catalogue sync

`profile-consistency/sync_profile.py` and the reusable workflow
`.github/workflows/profile-consistency-sync.yml` keep a profile README in
step with the **English** catalogue:

- the counts in sentences such as "102 software solutions across 15 solution groups";
- a generated list of all solutions between `<!-- jsp-catalogue:start -->` and `<!-- jsp-catalogue:end -->`. It has one collapsible `<details>` block per solution group (so the profile stays short on mobile), each solution links to its English page (`https://jsp-it.com/products_show?type=…`) with its one-line summary, and each group links to its filtered catalogue view.

On the first run, when the markers do not exist yet, the list is inserted
under a "📋 All JSP-Product™ solutions" heading after the line linking to the
complete catalogue. Later runs only replace the content between the markers.
Do not edit inside the markers; changes there are overwritten.

**How a run behaves:**

| Situation | Result |
|---|---|
| Profile already matches | Nothing changes. An open sync pull request is closed with a comment. |
| Catalogue changed | One pull request from the `automation/jsp-catalogue-sync` branch is opened, or the open one is updated, listing added and removed solutions and count changes. |
| Catalogue data unavailable (bot protection, outage, markup change) | Retried up to `attempts` times (default 4, 90 s apart). If it is still unavailable, nothing changes, a warning is shown and the next scheduled run tries again. |
| Markers broken or no place to insert the list | The job fails without changing anything. |

Pull requests are created with the workflow's `GITHUB_TOKEN`, so they are
authored by `github-actions[bot]`. GitHub does not start other workflows for
them, so the consistency check runs on the next schedule or a manual run
instead. The calling job needs `contents: write` and `pull-requests: write`,
and the repository must allow GitHub Actions to create pull requests
(*Settings → Actions → General → Workflow permissions*). The branch protection
of the default branch is unaffected: changes still go through a reviewed pull
request.

Caller example: [`profile-consistency/examples/jsp-it-catalogue-sync.yml`](../profile-consistency/examples/jsp-it-catalogue-sync.yml)
(Mondays 06:05 UTC and on demand, before the 06:23 consistency check).

| Input | Default | Description |
|---|---|---|
| `profile-path` | `README.md` | README to update. |
| `catalogue-url` | `https://jsp-it.com/catalogue.json\|https://jsp-it.com/products_jsp_overview` | English catalogue source(s), tried in order. Product links come from the JSON; group links use the overview page. |
| `branch` | `automation/jsp-catalogue-sync` | Branch for the sync commit; one pull request is kept open from it. |
| `attempts` | `4` | Tries when the catalogue data is unavailable, for example because of the site's bot protection. |
| `retry-delay-seconds` | `90` | Wait between attempts. |
| `tooling-ref` | `profile-consistency-v1` | Ref of this repository to load the scripts from. |
| `runs-on` | `ubuntu-24.04` | Runner with bash, git, gh and Python 3.11+. |

Locally (dry run unless `--write`):

```sh
python3 profile-consistency/sync_profile.py --profile README.md
python3 profile-consistency/sync_profile.py --profile README.md --write
```

## Weekly published-projects sync

`profile-consistency/sync_projects.py` keeps the list of **published
open-source projects** in a profile README in step with the account's public
GitHub repositories. It runs in the same sync workflow and its changes go into
the same pull request as the catalogue sync (set `catalogue: false` to sync
only projects).

**Which repositories are listed:** public (checked by both `private` and
`visibility`), not a fork, not archived, not a template, with a licence that
GitHub recognises and at least one published release. The profile repository
(named like the account) and `.github` are never listed; `projects-exclude`
leaves out more. Private repositories are never listed and never queried for
releases.

**Content:** the name comes from the repository name (`sbom-release-diff` →
"SBOM Release Diff"), the description from the repository's **About** text
(edit it on GitHub to change the profile text), and language and licence from
GitHub. Projects are sorted by name.

| `projects-format` | Output (between `<!-- github-projects:start -->` / `<!-- github-projects:end -->`) |
|---|---|
| `list` | `- **[Name](url)** — Description.<br>` + `Language · Licence · latest-release badge` |
| `table` | `\| Project \| Description \| Language \| Release \| Stars \|` with release and stars badges |

The markers must be added once by hand around the existing list or table; the
sync refuses to run without them (exit 2) instead of guessing where the list is.

| Input | Default | Description |
|---|---|---|
| `catalogue` | `true` | Also sync the JSP-Product counts and solution list. |
| `projects-owner` | — | GitHub account to list projects from. Empty skips the projects sync. |
| `projects-format` | `list` | `list` or `table`. |
| `projects-exclude` | — | Comma-separated repository names to leave out. |

**Repositories with required status checks:** pull requests opened with
`GITHUB_TOKEN` do not start other workflows, so a required check would never
run on the sync pull request. The workflow outputs `branch`; the caller adds a
small job that runs `gh workflow run <check>.yml --ref <branch>` with
`actions: write` (`workflow_dispatch` is allowed for `GITHUB_TOKEN`). See
[`examples/projects-sync-with-required-checks.yml`](../profile-consistency/examples/projects-sync-with-required-checks.yml).
The shared workflow itself never gets `actions: write`.

Locally (dry run unless `--write`; set `GH_TOKEN` to avoid API rate limits):

```sh
python3 profile-consistency/sync_projects.py --profile README.md --owner JSP-IT --format list
```

## Commercial offerings vs open-source projects

The catalogue lists **commercial JSP-Product™ solutions**. Many of them are
built on third-party open-source software, which the catalogue credits with
links to those upstream projects. Those are not JSP-IT's own open-source
projects. JSP-IT's original, independently licensed projects are listed
separately in the `JSP-IT/JSP-IT` profile under "Published Open-Source
Projects". This check counts only catalogue products. It never adds
open-source projects to the counts, or the other way round.

## Proposed website change (not implemented)

(`/catalogue.json`, proposed for the bot-protection issue, is now published; see [Data source](#data-source).)

Neither catalogue page currently links to JSP-IT's own GitHub or open-source
projects: there is no link in the page body or footer, and none in the
JSON-LD `sameAs` list. The only GitHub links point to third-party upstream
projects. Because the website source is maintained outside these
repositories, **no website change is made here.** For the website
maintainers to consider:

- A clearly labelled footer link in each language, next to the other company links:
  - EN: `<a href="https://github.com/JSP-IT">JSP-IT open-source projects on GitHub</a>`
  - FI: `<a href="https://github.com/JSP-IT">JSP-IT:n avoimen lähdekoodin projektit GitHubissa</a>`
- Accessibility: the link text names the destination on its own (no "click here", no icon-only link). If it opens a new tab, add a visible or screen-reader hint such as "(opens in a new tab)". Keep the footer's existing focus styles and contrast.
- Optional SEO: add `https://github.com/JSP-IT` to the organization's JSON-LD `sameAs` array on both language versions.
- Keep it out of the product catalogue itself, so open-source projects are not presented as JSP-Product™ solutions.

## Versioning and maintenance

- Versioned with SemVer: `__version__` in the checker and `profile-consistency/VERSION`, with tags `profile-consistency-vX.Y.Z` and a moving `profile-consistency-vX`. Breaking input or output changes bump the major version.
- **Owner:** JSP-IT Solutions Group maintainers. Changes go through reviewed pull requests, and the self-test (actionlint, unit tests) must pass.
- **When products are added or removed:** the weekly run fails until the company profile is updated by hand. That is intended.
- **When the page markup changes:** see [failure handling](#outcomes-and-failure-handling).
- **Action pins** are full commit SHAs. Update them together with the other workflows in this repository.

## Limitations

- Count statements are found with English and Finnish patterns ("N … solutions", "N solution groups", "N ratkaisua", "N ratkaisuryhmässä"). Unusual wording is not recognised. Profiles that must state counts fail loudly in that case rather than passing silently.
- Profiles are compared with one reference language (English by default, `--reference-language` locally).
- Link checks see what an anonymous client sees. Some sites block automated requests (403/429). Those links are reported as "could not be verified", not as broken.
- At most 200 links per profile are checked.
- **Bot protection (observed 2026-11-27/28):** GitHub-hosted runners are often served an nginx interstitial titled “One moment, please...” (HTTP 200, about 12 KB, no catalogue data) instead of the overview page. The website therefore publishes `/catalogue.json`, and the hosting's bot protection should have an exception for that **path** (not for a user agent, which anyone could copy). Until then, and as a safety net, both workflows retry when the data is unavailable (`attempts`, `retry-delay-seconds`) and fall back to the overview page. If every source and attempt fails, the result is “data unavailable” (exit 3), never a discrepancy, and the message shows the status, size, title and server of each response. The tools deliberately do not spoof a browser user agent or try to solve the challenge.
