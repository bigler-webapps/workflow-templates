# Deployment Python toolchain contract

`.github/scripts/deploy_toolchain.py` provides a strict offline contract parser,
a deterministic requirements renderer, and a read-only runtime drift check for
the deployment Python profiles. The versioned authority is
`toolchains/deploy-python.json`; the two requirements files are generated
representations and must match its pins byte for byte after Git CRLF-to-LF
normalization. A lone carriage return still refuses. The reviewed JSON is the
single version/hash authority; the validator constrains names, exact numeric
version syntax, wheel/platform identity and schema, not a second pin table.
Changing that trusted contract still requires a reviewed dependency change.
Contract, requirements and parser-source reads also require regular files and
use nonblocking, no-final-symlink opens with bounded reads. This is not a
verification of parent-directory ownership or trusted checkout custody.

## Public API

- `parse_contract(raw: bytes) -> ToolchainContract` accepts only the bounded
  schema 1 contract and checks it against the allowed distribution, wheel,
  profile, requirements path and console entrypoint identities.
- `render_requirements(contract, profile) -> bytes` returns canonical
  `--require-hashes --only-binary=:all: --no-deps` input. It does not write a
  file or install packages.
- `check_runtime(contract, profile, requirements, *, collector=None,
  runtime=None) -> CheckResult` checks exact requirements bytes, the supported
  runtime, selected installed distribution versions and selected entrypoint
  metadata. Results contain only `ok`, the profile, contract ID and a fixed
  code. `ToolchainError.code` is also a fixed safe value.
- `python -I .github/scripts/deploy_toolchain.py --profile controller` (or
  `target-parser`) reports one compact JSON result and exits zero only when the
  check passes. It does not offer repair or install behavior.

The controller profile checks `PyYAML==6.0.3` and
`django-core-micha==3.17.0`, including the exact `generate-env` console-script
metadata. The target-parser profile checks only PyYAML and does not inspect the
dcm distribution or entrypoints. Parser-version coherence is checked against
the literal `COMPOSE_PARSER_VERSION` in the target executor source without
importing or executing that module.

The current supported candidate is CPython 3.12 on Linux x86_64. Exact wheel
filenames and SHA256 values are in the JSON contract and both profiles' hashed
requirements inputs. An adopter must record the exact provisioned OS Python
security patch and verify the actual host architecture; this source checker
does not establish either.

## Provisioning boundary

Provisioning is separate from deployment execution. The expected installer
shape is:

```sh
python -m pip install --require-hashes --only-binary=:all: --no-deps -r toolchains/deploy-python-requirements.txt
```

The target-parser host uses `toolchains/compose-parser-requirements.txt` with
the same flags. These commands are documentation for a separately reviewed
provisioning process. The checker itself runs no subprocess, socket, package
code, entrypoint, provider or installer, and writes no requirements file. The
real collector uses `importlib.metadata.Distribution` with a bounded reader to
parse only the selected exact wheel's `METADATA` and `entry_points.txt`.
Each is capped at 64 KiB before parsing, opened as a regular file without
following its final symlink, and duplicate selected installations refuse.
Only normal unpacked wheel installations in at most 64 Python search roots
are supported; eggs, ZIPs and alternate versions do not fall back to broad
metadata discovery. An absent exact selected wheel reports missing, while
inconsistent version metadata refuses. An entrypoint value is data, never
loaded or invoked. No environment, credential, application configuration or
`.env` file is read.

The contracts and tests establish only this bounded Python source slice. They
do not establish the full system toolchain, actual operations or target host
identity, root-owned provisioning, protected source checkout, early CI
containment, action/transfer/hosted-recovery wiring, release or installation,
static deployment, staging, consumer migration, credential removal, or closure
of S231, S288 or S304. Those remain separate WFT-CI-45 and WM-INF-89/90/91
acceptance steps.
