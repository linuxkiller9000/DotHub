# DotHub

**Discover. Customize. Download. Install.** DotHub is a lightweight Linux desktop customization discovery and resource management platform. It uses Python's standard library, SQLite, semantic HTML, CSS, and vanilla JavaScript; it needs no Node build step, containers, or user accounts.

## Requirements

- Python 3.10 or newer (developed with Python 3.14)
- Optional: `unzip` for the guided installer's archive preview

All database and uploaded content defaults to `~/.local/share/dothub`, outside this project. Override with `DOTHUB_DATA`, `DOTHUB_DB`, or `DOTHUB_STORAGE`. The project currently has no third-party Python runtime dependencies.

## Install and run

```bash
cd ~/container/DotHup
python -m venv .venv
source .venv/bin/activate
# No pip install is needed; the runtime uses Python's standard library.
python -m backend.main
```

Open <http://127.0.0.1:8000>. The server binds to localhost. To change the port, set `DOTHUB_PORT=8080`. Remote binding is disabled unless `DOTHUB_ALLOW_REMOTE=1` is explicitly set; do not expose this development server directly to the internet.

The empty first-run experience is intentional. It does not include fake community records or statistics. The seeded category and environment vocabulary is extensible in the database.

## Automatic discovery

When the local server starts, its lightweight background collector waits 15 seconds, then checks GitHub's public repository search and any configured feeds. It repeats every 12 hours by default. Change the interval with `DOTHUB_COLLECT_INTERVAL_HOURS` (1–168 hours), or disable collection with `DOTHUB_AUTO_COLLECT=0`. The local admin page can also run a scan immediately and add public HTTPS RSS/Atom feeds.

The collector stores titles, source links, creator/license metadata where provided, and a short feed excerpt in the pending review queue. It does **not** download GitHub repositories or Reddit attachments, execute anything, or publish candidates. Feed URLs must pass a `robots.txt` check; redirects and non-public host addresses are rejected. Use feeds you are authorized to access.

Reddit discovery is disabled until an already authorized Reddit Data API bearer token is configured as `DOTHUB_REDDIT_BEARER_TOKEN`. DotHub does not scrape Reddit pages, use cookies, bypass robots rules, or ask for account credentials. Reddit currently says new public Data API access requests have stopped and remaining public API access is planned to end in March 2027; this connector may therefore be unavailable or temporary. See the official [Reddit developer FAQ](https://developers.reddit.com/docs/guides/faq) and [app registration notice](https://developers.reddit.com/app-registration).

GitHub's unauthenticated public API has rate limits. DotHub makes a small number of searches per scheduled cycle, honors API failures, and never attempts to evade rate limits. See [GitHub REST API rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api).

## Use the application

- Browse and combine search, category, and environment filters at Explore. Sorting by newest, alphabetical, or popularity is available; popularity uses actual download counts.
- Open a resource card for its attribution, license, compatibility, status, and download details. A resource is only marked `Verified` when an administrator records that status.
- Submit a ZIP (maximum 30 MiB compressed, 150 MiB expanded, 3,000 entries) or a public GitHub repository. Archives are checked for traversal paths, symlinks, encryption, oversize entries, and unsafe filenames, then remain in the local moderation queue. A direct GitHub import fetches its repository archive only after someone submits that specific repository; scheduled discovery is metadata-only.
- Visit **Local admin** on the same machine to create draft resources and dependency records, inspect submissions, reject, or approve. An approval publishes the inspected submission; unknown licenses are conservatively non-redistributable. Review extracted files before approval.
- Create a database backup from the admin page. The backup is placed under `~/.local/share/dothub/backups`.
- Export the public library from admin or run `python -m tools.export_static --output export`. Only published records and permitted archives are exported. The output is static and project-subpath-relative; GitHub Pages cannot accept uploads, write the SQLite database, or run moderation. Those features require a separately hosted backend.

## Guided install helper

`installers/installer.sh detect` reports detected distribution and package manager. `installers/installer.sh plan RESOURCE.zip [DESTINATION]` lists intended paths after basic traversal checks. It does not install packages, copy files, request sudo, overwrite configs, or execute imported scripts. Review the resource's own install documentation and make backups before installing manually. Distribution-specific package commands are not invented; unverified dependency metadata is labeled.

## Tests

```bash
python -m unittest discover -s tests -v
```

Coverage includes empty and populated SQLite, resource persistence, ZIP traversal rejection, static export publication filtering, archive attribution files, homepage HTTP response, and remote admin denial. The HTTP test exercises the WSGI application directly rather than starting a separate process. Collector tests use mocked responses and do not contact external services.

## Arch Linux notes

Python and SQLite are part of the base Arch repositories. No database server is needed. If `python -m venv` reports missing `ensurepip`, install the distribution's `python` package and recreate the environment. Automatic discovery needs an internet connection; set `DOTHUB_AUTO_COLLECT=0` when running offline.

## Current limitations

- The local app uses a small standard-library WSGI server rather than FastAPI because this installation is runnable without third-party runtime packages.
- GitHub import fetches the public archive through GitHub's codeload endpoint when a user submits a specific repository; automatic discovery does not fetch archives. License detection is not complete for every repository. Confirm the license and redistribution rights before approval.
- Upload moderation is local and relies on the administrator to inspect content. There is no public account system and no remote admin authentication system; remote binding is deliberately gated.
- Compatibility, setup references, installation-method, and media schema are ready for extension, while their full editing workflows and rich media upload processing are not implemented yet.
- The installer is a safe detection and preview helper, not an automatic guided installer. It does not modify user configuration or install packages.
- Static export produces browse, setup, about, search, category, environment, and resource pages plus public JSON and a local broken-link report.
- “Any other website” is supported through configured RSS/Atom feeds, not unrestricted recursive web crawling. Sites without a feed or documented API need a source-specific integration and must permit automated access.

## Project layout

`backend/` contains the HTTP app and SQLite schema; `frontend/` contains the local interface; `tools/` contains static export and archive generation; `installers/` contains the independent Bash helper; `tests/` contains offline tests. Runtime database and uploads live in XDG data storage and are ignored by Git.
