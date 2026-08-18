# A. Envelope — authored by the Expertenchat

## Goal & expected outcome
- Ziel: `restore-source-export`'s snapshot resolver muss "latest" innerhalb des B2-Repos auf den
  Snapshot des tatsächlich angefragten Quell-Servers (`source_target`) beschränken, statt den
  zeitlich neuesten Snapshot im gesamten geteilten Repo zu nehmen.
- Expected outcome: Ein `latest`-Restore für `source_target=main-prod` findet den zeitlich
  neuesten Snapshot **mit host-Tag `main-prod`**, auch wenn ein anderer Server (z.B.
  `research-prod`) danach noch einen eigenen Snapshot ins selbe Repo geschrieben hat. Bestehendes
  Verhalten für einen explizit übergebenen `snapshot_id` (kein `latest`) bleibt unverändert.

## Scope + non-goals
- In scope: der "🔍 Resolve snapshot + find dump path"-Step in
  `.github/actions/restore-source-export/action.yml` (aktuell `restic snapshots --json | jq
  'sort_by(.time) | last | .id'` bei `SNAPSHOT_INPUT=latest`, Zeile ~208). `backup.py` taggt
  bereits jeden Snapshot mit `--host $(hostname)` — der Fix filtert vor dem Sortieren per
  `restic snapshots --host <source_target>`.
- Explizit NICHT in scope: `restore-dest-import`, `backup/action.yml` (INF-24/INF-25, bereits
  gefixt und released als `v2.10.1`), `sync-staging.yml`/`backup.yml` in `webapp-management`
  (Caller-Pins werden NICHT in diesem WO gebumpt — separater Schritt, siehe unten), die
  Matrix-Reihenfolge/Serialisierung der Backup-Targets, `resolve_inventory_targets.py`.
- Do-not-touch: der explizite `snapshot_id`-Pfad (`restic snapshots "$SNAPSHOT_INPUT" --json`) —
  der ist bereits eindeutig (eine konkrete Snapshot-ID) und braucht keinen Host-Filter.

## Tier · precondition / gate
- Tier: 3 (Shared-Core-Composite in `workflow-templates`, Backup/Restore ist eine sensitive
  Surface). Kein Precondition/Gate — eigenständig lauffähig.

## Risks
- Falscher Host-Tag-Wert (z.B. FQDN vs. kurzer Hostname) lässt den Filter leer laufen ->
  `restic snapshots --host X` liefert nichts -> derselbe "Snapshot nicht auflösbar"-Fehler wie
  heute, nur früher. `backup.py` taggt mit `os.uname().nodename` (siehe Kontext-Paket) — das MUSS
  exakt dem `source_target`-Wert entsprechen (`main-prod`, `innoservice-prod`, `research-prod`),
  wie er auch in `ssh_host: ${{ matrix.target }}.tail990d7f.ts.net` verwendet wird. Verifizieren,
  nicht annehmen.
- Der `--host`-Filter ändert das Fehlerbild bei einem echten "der Ziel-Server hat noch nie
  gesichert"-Fall: vorher fand man evtl. zufällig einen fremden Snapshot (falsch, aber "erfolgreich
  aussehend"), jetzt bricht die Resolution sauber mit "kein Snapshot für Host X" ab. Das ist
  gewünscht, aber die Fehlermeldung an dieser Stelle sollte das auch so benennen (nicht die
  bestehende generische "Snapshot 'latest' could not be resolved"-Meldung unverändert lassen, wenn
  sie jetzt einen anderen Grund hat — Wortlaut ist Implementer-Ermessen, Sache muss nur stimmen).

## Required tests to WRITE (you write them and run YOUR OWN new ones; the orchestrator's run is the gate)
Neue Datei `.github/scripts/test_restore_source_export.py`, im Stil von
`.github/scripts/test_backup_action.py` (strukturelle + behaviorale Assertions gegen den
Bash-Text des Steps, keine echte B2/restic-Anbindung nötig):
- Der resolve-Step muss bei `SNAPSHOT_INPUT=latest` `restic snapshots` mit einem `--host`-Flag
  aufrufen, dessen Wert aus `SOURCE_TARGET`/`inputs.source_target` kommt (nicht hart codiert).
- Der explizite `snapshot_id`-Pfad (`SNAPSHOT_INPUT != latest`) darf NICHT zusätzlich nach Host
  gefiltert werden (regressionsfrei zum bestehenden Verhalten).
- Negativtest: der alte, host-blinde Aufruf (`restic snapshots --json | jq 'sort_by(.time) |
  last'` ohne `--host`) darf im `latest`-Zweig nicht mehr vorkommen.

---

# B. Implementation map — filled by the Orchestrator — ADDRESSED TO THE IMPLEMENTER

## Context package

**Named files to change:**
- `.github/actions/restore-source-export/action.yml`, Step "🔍 Resolve snapshot + find dump path"
  (aktuell Zeilen 184–246 im Repo-Stand vor diesem WO). Der relevante Teil, `latest`-Zweig:

```bash
if [ "$SNAPSHOT_INPUT" = "latest" ]; then
  # `restic snapshots --latest 1` returns the latest snapshot PER (host, paths)
  # tuple — when an old server identity is still in the repo, the first entry
  # may be a stale snapshot. Sort all snapshots by .time and pick the truly
  # newest one.
  SNAP="$(restic snapshots --json | jq -r 'sort_by(.time) | last | .id // empty')"
else
  SNAP="$(restic snapshots "$SNAPSHOT_INPUT" --json | jq -r '.[0].id // empty')"
fi
if [ -z "$SNAP" ]; then
  echo "❌ Snapshot '${SNAPSHOT_INPUT}' could not be resolved against the configured B2 repo."
  exit 1
fi
```

  `SOURCE_TARGET` ist bereits als env-Var im selben Step gesetzt (`inputs.source_target`, siehe
  weiter oben im `env:`-Block dieses Steps) — für Logging/Naming, aber bislang NICHT für die
  restic-Abfrage selbst benutzt. Das ist exakt die Lücke.

- `webapp-ops-scripts/backup.py`, Zeile ~319–329: bestätigt, dass jeder Snapshot mit
  `--host os.uname().nodename` und `--tag daily` erzeugt wird — `os.uname().nodename` ist der
  kurze Hostname des jeweiligen Servers (`main-prod`, `innoservice-prod`, `research-prod`), NICHT
  der Tailscale-FQDN. Nur zum Verifizieren des Filterwerts, NICHT zum Ändern.

**Root cause (Kontext, nicht Aufgabe):** Alle drei Backup-Targets teilen sich EIN B2-Restic-Repo
(`backup.yml`, `max-parallel: 1`, seriell main-prod → innoservice-prod → research-prod). Der
`sort_by(.time) | last`-Aufruf ignoriert den Host komplett und pickt global den zeitlich neuesten
Snapshot im ganzen Repo — das ist seit `research-prod` (INF-16, hostet nur hram/hpc-bridge) als
dritter, zuletzt laufender Backup-Origin dazukam, systematisch der research-prod-Snapshot. Für
jede App, die nicht auf research-prod läuft, findet der nachfolgende Dump-Regex nichts.

**Invarianten / Do-not-touch:**
- Das `::add-mask::${RESTIC_REPOSITORY}`-Masking direkt darüber unverändert lassen.
- Der `$SNAP`-Output (`snapshot_id`) und dessen Weiterverwendung im Dump-Finder-Step direkt danach
  bleiben strukturell gleich (Fix ändert NUR, welcher Snapshot als "latest" gilt).
- Kein Verhalten am expliziten `snapshot_id`-Pfad ändern.

Directive: Arbeite aus diesem Paket; öffne nur die genannten Dateien zur Verifikation. Bei Bedarf
für tieferes Kontext-Graben: read-only Explore-Subagent (Haiku).

## Target repo working directory (absolute)

`C:\Users\biglmi\Documents\webapps\workflow-templates`

## Preamble — REQUIRED, part of this file

> The text above is the COMPLETE spec — the committed WO file's content, not a plan to refine;
> there is no separate plan file. Read the nearest `AGENTS.md`, the relevant
> `.codex/skills/<role>/SKILL.md`, and the app `MEMORY.md` ONLY for conventions. Stay in scope; do
> not touch auth/permissions/deps/schema/CI unless the spec says so; do not update `MEMORY.md`.
> **Do NOT edit `WORK_ORDERS.md`** — the register row and the review verdicts are the
> orchestrator's alone. Do NOT `git add`/`commit`/`push` — leave every change uncommitted in the
> working tree for the orchestrator's independent review. WRITE the tests the `Required tests`
> section calls for AND **RUN the tests you just wrote** to confirm they execute and pass — that
> is the ONLY test run you do (NOT the app's affected/full suite, NOT any review). The
> orchestrator re-runs the authoritative set + does the independent review after you finish —
> those are the gate; your own run does not count as the gate.
>
> Narrate continuously: a `PLAN: <step1> | <step2> | …` line up front, then a single-line
> `PROGRESS: [<n>/<total>] <present-tense action>` before every relevant action (and `… done` on
> completion), spaced so no gap exceeds ~2 min, stdout unbuffered, plus exactly one final
> `RESULT: DONE|BLOCKED <reason>`.

---

# C. Orchestrator only — NOT ADDRESSED TO THE IMPLEMENTER

> **If you are the implementer reading this work order as your own specification: STOP at this
> line. Everything below describes what the Orchestrator does AFTER you finish. You do none of
> it — no reviewers, no verification run, no register edit, no commit.** You ARE the invocation
> described below; do NOT shell out to `codex exec`.

## Execution directive

Implement through `codex exec` in the background — invoked directly via Bash (never the
`debugger`/`*_coder` Agent wrappers) with BOTH flags `--skip-git-repo-check` and
`--dangerously-bypass-approvals-and-sandbox`. `.claude/codex-status.md` hat für 2026-08-18 noch
keinen Eintrag → Codex wird als erster Versuch genutzt (ein Probe erlaubt; bei Fehlschlag Fallback
auf direkte Claude-Implementierung, was die Autorschaft kippt und `reviewer` zwingend macht — bei
Tier 3 ohnehin schon Pflicht).

## Review routing

Tier 3, kein Frontend-Diff → `reviewer` (Sonnet, voller Kontext: Diff inline + dieser WO-Abschnitt
+ `.github/actions/restore-source-export/action.yml` + `backup.py`-Auszug). Kein `ui_reviewer`
(kein Frontend). Kein `sec_reviewer` (kein Auth/Security-Bezug — B2-Zugangsdaten selbst werden
nicht berührt). Zusätzlich eigener zielgerichteter Pass auf Secret-Handling/Masking im Diff, da
das ursprüngliche `::add-mask::`-Kommentar direkt neben der geänderten Stelle sitzt.

## Verification

Authoritative Testlauf: die neue `test_restore_source_export.py` plus die bestehende
`test_backup_action.py` (Nachbar-Suite im selben `.github/scripts/`-Verzeichnis, direkt
betroffener Bereich). Kein Full-Run — `workflow-templates` hat keine app-weite Suite, das ist
bereits der volle affected-Bereich. Kein Prototyp im Scope, keine Preview nötig (reine
CI-Action-Logik, nichts Browser-Renderbares).

## Register + commit

`WORK_ORDERS.md`-Zeile `INF-31` mit dem genannten `reviewer`-Verdikt eintragen, Commit auf
`main` (dieses Repo hat keinen `develop`, siehe `CLAUDE.md`/`AGENTS.md` Branching für
Infra/Platform-Repos). Nach dem Commit: separater Hinweis an den Operator, dass die
Caller-Pins (`webapp-management/.github/workflows/sync-staging.yml`, aktuell
`restore-source-export@v2.10.0`) noch auf die neue Version gebumpt werden müssen — das ist NICHT
Teil dieses WOs (anderes Repo, eigener Tier-3-Schritt), aber ohne den Bump bleibt der Fix wirkungslos.
