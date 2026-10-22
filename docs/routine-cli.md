# Routine Claude CLI contract

The runner role's `ansible/roles/github_runner/files/routine-cli.json` is the
single selected package/version/Node-major authority. CI runner provisioning
consumes that file and rejects inventory overrides that differ. The reusable
`.github/actions/check-routine-cli` action reads the same manifest and the
stdlib-only helper from its own checkout, so its behavior does not depend on
the caller's working directory.

The preflight executes only `claude --version`, with a five-second timeout and
bounded stdout/stderr. It requires exactly the selected version followed by
` (Claude Code)`, allowing one final LF or CRLF. It does not authenticate,
read provider configuration, invoke a model, install packages, fetch code, or
echo executable output. A mismatch or malformed manifest fails with a fixed
diagnostic code. The action sets `DISABLE_AUTOUPDATER=1` in `GITHUB_ENV` for
subsequent steps.

The role installs and inspects Claude under the manifest's fixed `/usr/local`
npm prefix and verifies `/usr/local/bin/claude` directly. The shared action uses
that same absolute location; earlier `GITHUB_PATH` changes cannot substitute
another executable. This is a version/location check, not an authenticity
attestation or containment against a privileged process that can replace system
binaries. Such CI-host authority remains a separate WM-SEC-18 residual. The
helper's explicit executable override is for private fixtures and is not exposed
as an action input. Its child has an empty private working/config/home directory,
a minimal environment without inherited credentials, bounded output, and a
separate process group that is killed on completion or refusal. Only Linux CI
is supported. These controls do not establish a network sandbox for the CLI.
The action also invokes fixed `/usr/bin/bash` without profiles and
`/usr/bin/python3 -I`; `GITHUB_PATH` cannot substitute the interpreter and Python
environment variables/user site packages cannot replace its imports.
When adopting the component, bind the actual agent invocation to the same fixed
location as well; a preflight does not constrain a later unrelated PATH lookup.

Consumers must pin the action through a released, versioned
`workflow-templates` reference. Adopt it only after the collection version and
CI runner provisioning agree on the manifest. The active routine agent image
currently has its own Claude and Codex pins, and the active workflow does not
yet consume this preflight. Aligning that image and wiring its authorized
versioned consumer reference remain WM-RT-30 adoption work. This source batch
leaves active routine workflows unchanged; adoption is a later authorized
step after provisioning evidence exists. Existing Codex pins and model
selection stay unchanged in this batch.
