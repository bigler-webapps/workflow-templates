# Static release producer contract

`.github/actions/publish-static-release` packages static files that a caller has
already built. It does not run application setup, build commands, hooks, local
actions, or deployment logic. The action accepts `release_contract: v1`, an
exact workflow reference, a repository-relative asset directory, and one
approved archive root. The default archive root is `report/report`; mapping an
already-built `frontend/dist` directory there does not grant deployment
routing authority.

Before opening asset content, the producer checks the GitHub repository, full
source SHA, supported `develop` or `main` ref, workflow reference, run ID and
attempt, then compares the source SHA with `git rev-parse HEAD`. Git runs with
fixed argv, a short deadline, suppressed errors, and only basic tool/OS
environment. A mismatch or unknown contract mode fails before output
publication. The manifest is source evidence; WM-SEC-17 must independently
authorize the originating run and source state.

The bundle is uncompressed USTAR under the published static preparer's
contract: at most 64 MiB, 512 entries, 16 MiB per file and 48 MiB of file
data. Only non-executable regular files are included. Symlinks, special files,
case-folding collisions, path traversal, executable files and control files
such as `.env`, `.git`, `.github`, compose/project/secrets files, Dockerfile,
Caddyfile and script/action formats are rejected. Ordinary browser assets
remain data. Files are opened without following links and checked for identity
and size changes during bounded copying. USTAR ownership/device values and
file mode are normalized. Platforms without the required no-follow directory
protections fail closed.

The producer also rejects hard-linked files, SSH/private-key names, newline or
colon names, and trees deeper than 32 components. A regular file replaced by a
FIFO cannot block a content open. The control-path classification reuses the
published static preparer's rules, with additional key exclusions before reads.

The action creates a random private directory under `RUNNER_TEMP` with mode
0700 and files with mode 0600. Its first pinned `actions/upload-artifact`
4.5.0 invocation uploads only `bundle.tar` as
`release-static-<run id>-<attempt>`, with missing-file failure, no overwrite,
and seven-day retention. After upload, the action uses the actual positive
artifact ID and actual bundle bytes to produce canonical `release.json` via
the shared `release_manifest.produce_static_manifest` API. The second pinned
upload publishes only that manifest as
`release-manifest-<run id>-<attempt>`. An always-running step removes only the
invocation-owned private directory.

`RUNNER_TEMP` must resolve without symlink ancestors and lie outside the actual
Git checkout root, obtained with fixed `git rev-parse --show-toplevel` argv.
Assets resolve relative to that root even from a checkout subdirectory.
A private preparation record binds the checked identity, exact bundle
length and hash across the upload/finalize phases. Changed bundle bytes or
source identity fail before manifest publication. Cleanup validates all child
names before deleting anything and preserves an unexpected child. Action Python
processes use isolated mode and load only their installed sibling helpers.

The CLI publishes its cleanup path before copying assets, handles normal
SIGINT/SIGTERM cancellation, and removes partially prepared outputs on a Python
interruption. The final action step is idempotent after that cleanup. A forced
kill or host failure cannot execute process cleanup; runner temporary-directory
lifecycle and real cancellation behavior remain part of candidate acceptance.

The static manifest binds source repository/SHA/ref/workflow/run/attempt to
the static artifact name, immutable artifact ID, byte length, and SHA-256.
Consumers must independently select the artifact from authoritative GitHub
run metadata, validate the manifest and bytes, and pass the bundle through the
published safe USTAR preparer. Upload completion does not establish deployment
success or release authorization. These local tests use synthetic Git output
and artifact IDs; actual GitHub artifact upload/retrieval and a static
candidate deployment remain separate acceptance evidence.
