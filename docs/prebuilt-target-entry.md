# Prebuilt target stream entry v1

This module is a target-side stream adapter around the existing image and static deployment SDKs. It does not retrieve artifacts, authorize a release, load target policy, choose a host, or activate itself. Importing it has no deployment effect. An independently installed and authorized target launcher must supply an enabled immutable `Invocation`; until that launcher and transport exist, this component is not an operational entry point.

## Request frame

The input is one binary frame, followed by end-of-stream. All integers are unsigned, big-endian. There is no JSON request, base64 encoding, legacy framing, or format autodetection.

| Offset | Width | Field | Required value or bound |
| --- | ---: | --- | --- |
| 0 | 8 bytes | magic | ASCII `PTARGET1` |
| 8 | 1 byte | version | `1` |
| 9 | 1 byte | output kind | `1` image, `2` static |
| 10 | 4 bytes | manifest length | `1..16384` |
| 14 | 4 bytes | bundle length | image: `0`; static: `1..67108864` |
| 18 | manifest length | raw manifest | Existing release-manifest v1 bytes |
| 18 + manifest length | bundle length | raw static bundle | Present only for static output |

The header is always 18 bytes. The fixed header and both lengths are validated before payload reads. Payload reads request at most 64 KiB and accept partial reads. EOF before the complete payload, a read that returns non-bytes or more than requested, a kind mismatch, or any trailing byte refuses the frame. No payload is written to a caller-selected path. The manifest carries source identity as data; the header carries no command, target routing, identity, policy, credential, or authority fields. The unchanged SDK compares manifest data with the separately trusted release identity and selected output.

The local stream object is a trusted transport seam and must enforce its own blocking read/write/flush deadline. This adapter uses bounded cooperative checks before and after every read; Python cannot forcibly interrupt a stream implementation that blocks inside `read()`.

## Trusted invocation

The launcher creates an exact `Invocation` with `enabled=True`, output mode, immutable `ReleaseIdentity`, exact matching target spec, a `deploy-` plus 40 lowercase hexadecimal correlation value, and a finite timeout from greater than zero through 1800 seconds. The default is disabled. Image mode requires an image namespace and the trusted registry-reader capability, and rejects static selection fields. Static mode requires the trusted `StaticArtifactSelection`, and rejects image namespace or registry-reader fields. Both modes may receive the actual SDK executor seam and a monotonic clock. Subclasses, mixed capabilities, malformed values, and a missing capability fail before reading input or entering an SDK.

The clock history guard and absolute monotonic deadline are shared with the selected SDK. The adapter checks the whole parse-and-deploy budget around bounded reads, SDK execution, receipt construction, and output. The SDK retains its own 1800-second maximum. A short launcher timeout is honored. A clock that moves backwards, returns a non-finite value, fails, or reaches the deadline is refused. If the deadline or clock prevents output before SDK entry, a fixed `EntryError` is raised without writing a receipt. After SDK entry, expiry before output raises `receipt_write_failed_ambiguous`, because a target effect may have happened; it does not send a late failure receipt or imply rollback.

Image mode calls `prebuilt_target.execute_image_deployment` with the raw manifest, the separate trusted identity/namespace/registry reader, target policy, and the same executor/clock/deadline. The SDK checks the immutable digest against its independent registry read, then performs the existing no-build pull, activation, migration, and observed service-health checks. Static mode calls `prebuilt_static_target.execute_static_deployment` with the raw manifest, selected artifact, actual bundle bytes, target policy, and the same executor/clock/deadline. The SDK verifies the selected artifact and bytes, uses the existing bounded archive preparation, runs no-build activation, and checks observed service health. Post-activation static failures retain assets for recovery as the SDK requires.

## Receipt

The adapter writes one compact JSON object followed by LF. Its encoded size is at most 4096 bytes. The receipt contains only the version, trusted correlation, status/effect, trusted source repository/SHA/run/attempt, environment/project, output kind, and selected actual SDK result fields. A successful receipt includes `health: "healthy"`, the observed service list, and the immutable image; image mode also includes the SDK's migration result. Static mode includes the selected artifact ID and verified SHA-256. Failure receipts use stable error codes and an explicit `effect` of `none` for refusal before SDK entry or `unknown` after SDK entry. Static post-activation failures set `recovery_required` without returning an asset path.

The adapter never serializes a manifest, private runtime input, target path, command output, exception message, stack trace, or credential. It validates result class and trusted identity bindings before emitting success. A sink is checked for a write capability before any effect. A short/failed write or flush raises `receipt_write_failed_ambiguous`; a clock failure or exhausted budget during/after output after SDK entry also produces that ambiguous exception. A late pre-SDK refusal raises the specific fixed clock/deadline error and its already written failure receipt still has `effect: "none"`. The adapter never retries output, claims rollback, or fabricates a second receipt. The target effect may have happened even when the caller loses its acknowledgement. A caller must require normal function completion within its deadline as well as a valid receipt; a JSON success line alone is insufficient delivery evidence.

## Scope and remaining prerequisites

This source component does not install a CLI, socket, forced command, service, SSH path, or launcher. It does not read policy files, perform WM-SEC-17 authorization, retrieve artifacts, establish a network identity, or create a replay lock. Correlation is receipt data, not authority or exactly-once protection. It does not run on the operations host and must not be used there to build images or run Docker. Protected installation and launcher wiring, transport, shared hosted break-glass exclusion, exact toolchain adoption, candidate staging, failure recovery, consumer rollout, and live evidence remain separate gates.
