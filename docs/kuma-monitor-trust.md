# Kuma monitor configuration trust boundary

Monitor declarations are treated as data from app configuration. The sync script copies bounded YAML values before processing them and rejects shell-style environment references, recursive or oversized structures, and malformed destination scalars. Monitor values never use controller environment expansion. Invalid declarations stop the single-app sync before monitor metadata reads or writes; in a multi-app run, the invalid app is reported, other apps continue, and pruning is skipped.

Notification configuration follows a separate trusted path. Its existing selected environment expansion and typed SMTP coercion remain available to the trusted sync operation. This separation prevents an app monitor declaration from selecting controller credential values without changing notification delivery configuration.

URL checks require an absolute HTTP or HTTPS URL with a host and a valid optional port, and reject user information, control characters, and surrounding whitespace. Host and port fields receive corresponding literal-scalar checks. These syntax checks do not provide network egress allowlisting or prove protection against every server-side request forgery path.

Literal internal service names with underscores remain accepted; validation does not impose public DNS registration rules. Numeric shell references such as `$1` are rejected alongside named references. Literal dollar punctuation without a reference remains supported.

This is a source-only change in `workflow-templates`. The existing `webapp-management` workflow tracks the template's `main` branch, so future invocations may consume this source after it lands. This document does not establish that a caller has adopted a reviewed revision, that a live Kuma sync ran safely, or that the S286 or WM-INF-91 acceptance evidence is complete.
