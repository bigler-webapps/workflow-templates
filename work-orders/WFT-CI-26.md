# WFT-CI-26 — `tailnet-connect` reuses a connection the kernel cannot route through

## Part A — Envelope

### Goal

The reuse probe accepts a warm daemon only when the job can actually reach the tailnet through it.

### The defect, and it is written down in the action itself

`tailnet-connect`'s probe decides reuse on two conditions:

```sh
state="$(… .BackendState …)"
tagged="$(… .Self.Tags | index($t) …)"
if [ "$state" = "Running" ] && [ "$tagged" = "true" ]; then
  connected=true
fi
```

`Running` plus the right tag. The action's own comment states the gap outright:

> *"Reuse is keyed on connection + tag only — NOT on routes."*

That is the bug, documented as a note.

### Measured on `netcup-runner-1`, 2026-08-27

```
tailscale status  → Tags ['tag:ci-deploy'], TailscaleIPs ['100.109.210.125', …]
tailscale ping    → pong from mhaas-prod (100.78.112.96) via 37.221.194.248:41641 in 1ms
nc -vz 100.78.112.96 22 → timed out

ip route get 100.78.112.96 → dev tailscale0 … src 37.221.195.133
ip -br addr show           → tailscale0  UNKNOWN  fe80::af99:7174:767c:6691/64
```

**`tailscale0` carries no tailnet IPv4** — only a link-local IPv6. It is the default daemon that
`group_vars/runners.yml` deliberately leaves unregistered (`tailscale_authkey: ""`); the permanent
registration lives on the separate mgmt instance.

So the kernel routes `100.64.0.0/10` into an interface with no tailnet address and picks the host's
**public** IP as source. The far side has no route back. Packets vanish.

`tailscale ping` succeeds throughout, because the CLI talks to the mgmt daemon in userspace and never
touches the kernel path. That is why this reads as "the tailnet works, SSH doesn't" and survived three
wrong diagnoses — a missing target tag, a missing ACL rule, and the wrong host. All three were refuted
by measurement; only the routing table showed it.

### Why this is fleet-latent, not one host's problem

The probe passes on **any** runner whose warm daemon is `Running` and correctly tagged, regardless of
whether the job's kernel routing reaches it. On such a runner every job that takes the reuse branch
silently loses tailnet connectivity while reporting `reuse existing tailnet connection: true`.

It has not bitten the other repos yet, which means something has been leaving `tailscale0` registered
for them — not that their path is correct.

### Scope

Add a third condition to the probe: the daemon's own tailnet IPv4 must be present on a local
interface, so the kernel will use it. Fail the probe when it is not, which drops through to the
existing ephemeral join.

A workable shape — the WO does not prescribe the implementation, only the property:

```sh
ip4="$(printf '%s' "$json" | jq -r '(.Self.TailscaleIPs // []) | map(select(test(":") | not)) | .[0] // empty')"
routable=false
[ -n "$ip4" ] && ip -4 addr show 2>/dev/null | grep -qF "$ip4" && routable=true
```

### Non-goals

- **No change to `group_vars/runners.yml`.** The two-daemon design is deliberate: the default
  instance stays unregistered so per-job ephemeral joins work. This order fixes the probe, not the
  topology.
- No ACL change. The `tag:ci-runner-host` entry added to the SSH deploy rule on 2026-08-27 was based
  on a refuted diagnosis — measured, netcup's daemon carries `tag:ci-deploy` and rule 2 already
  covered it. **Reverting that entry is a separate operator action**, not this order's.
- No change to the join or retry branches.

### Tier

**3** — a composite on the deploy path of every repo.

### Risks

- **An over-strict check reintroduces what reuse was built to avoid.** The action's header says
  `tailscale up` *"tears down the warm datapath (cold first-connect → SSH timeout)"*. If the new
  condition rejects healthy warm daemons, every job pays a cold join and the failure mode returns
  wearing the opposite mask. The condition must be narrow: **no tailnet IPv4 on any local interface**,
  nothing broader.
- The check runs before `jq` availability is guaranteed on some runners — the existing probe already
  guards for that (`|| echo unknown`). Match it; do not assume.
- It can only cause **more** joins, never fewer. That bounds the blast radius: the worst case is the
  path the action already takes when disconnected.

### Tests

Both branches, live, per `AGENTS.md` exception (b) — two-sided, since this is a composite in
`workflow-templates` consumed by every app:

- **Reuse branch:** a runner whose warm daemon holds the tailnet IPv4 on a routable interface still
  reuses. `reuse existing tailnet connection: true`, no join, and an SSH that succeeds.
- **Join branch:** `netcup-runner-1` as it stands today — probe returns false, the ephemeral join
  runs, and `ssh deploy@mhaas-prod … true` succeeds afterwards. **That SSH is the acceptance
  criterion**, not the probe's own output.

`cinevia`'s `staging-health.yml` is the cheapest carrier for the second: it exercises the identical
path with `ssh … true` as its payload and changes nothing.

---

## Part B — Implementation map

### Files

- `.github/actions/tailnet-connect/action.yml` — the `ts_probe` step, currently around lines 39-61.
- Its test file under `.github/scripts/`, if one exists — find it before editing, not after.

### Context

- `webapp-management/ansible/group_vars/runners.yml` explains the two-daemon topology and why the
  default instance is unregistered. Read it before changing anything, so the fix does not fight the
  design.
- `workflow-templates/.github/workflows/staging-health.yml` — the `Pre-flight SSH check` step, the
  cheapest live probe of the whole path.

### Progress contract

`PLAN: …` · `PROGRESS: [n/total] <action>` · one final `RESULT: DONE|BLOCKED <reason>`.

---

## Part C — Orchestrator only

*Stop line.*

### Review

Tier 3, independent. The reviewer's question: **can the new condition reject a runner that is in fact
routable?** A false negative there costs every job a cold join, which is the failure this action was
written to prevent.

### Register

`WFT-CI-26`. The Notiz records both branch results and the SSH that followed the join — the probe's
own output is not evidence of anything, which is the whole point of this order.

### Blocks

`WM-TAKE-8`'s three pending pushes (Kira `6a353f0`, Gustav `8d5f2a3`, Photogallery `b823465`) and
cinevia's deploy. None of them should land before the join branch is proven.

### Commit

`main`, after the dispatch evidence exists.
