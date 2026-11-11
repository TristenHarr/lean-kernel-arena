# The Quiet Board

An unofficial companion to the Lean Kernel Arena: every Arena checker, built from its entry as
written, run on the Arena's own test inputs (byte for byte), on one dedicated machine that does
nothing else, and ranked on what its users live with: wall time on one core and on four, peak
memory, energy, and soundness. The site is a strict addition to the Arena's: `lka.py build-site
--board board.json` renders the Arena's pages exactly as before, and the board under `quiet/`.

The full method, with every rule typeset, is the site's methodology page
(`templates/quiet/methodology.html`). In short:

- **Measure the target.** The overall score: wall time (4 cores and 1 core), peak memory of every
  process of the checker together, package energy. Instructions retired (every thread, user and
  kernel) have a category of their own, and decide the **Arena order** category: the official board's
  own rule (soundness, completeness, Mathlib instructions, declines) on the same runs, checked against
  lka.py's `board_order_key` so the two boards read side by side. Recorded and shown: cycles, IPC.
- **Inputs no checker can know in advance.** Each round (a calendar month) measures the Arena's inputs
  re-encoded from the round's seed (the same declarations and verdicts in new bytes; `lka_quiet/encode.py`)
  and adds an invalid twin of each test the season names (one early declaration's value swapped for
  another's of a different type, kept only if Lean's kernel rejects it; `lka_quiet/twins.py`). The seed
  stays on the box, the board publishes its sha256, and it is released when the round ends. The staged
  inputs are readable by one group, given to a measured run alone, which sees its own input and nothing
  else staged; no build ever can.
- **Runs prove their conditions.** A run that ran off the pinned clock, was throttled, shared its
  cores, or had a counter multiplexed is kept for the record and repeated. A run in which any of the
  checker's processes died of a signal or of memory is an error, whatever exit code a wrapper made.
- **Enough runs.** A small test runs until its median's distribution-free 95% interval is within
  1%; a heavy one three times while its spread stays within 1%. Passes are interleaved in an order
  drawn from the round, so drift falls on every checker alike.
- **Scored geometrically, failures at twice the test's own limit.** Factors over Lean's kernel,
  weighted over metrics and test families, reference-invariant; a checker's own shorter time limit
  never lowers what a failure costs.
- **Honest uncertainty.** Factor intervals from a paired stratified bootstrap; ranks are marginal
  confidence sets (Mogstad, Romano, Shaikh & Wilhelm, 2024), Bonferroni-corrected, so checkers the
  data cannot separate share a rank.
- **Rules fixed in advance.** A season's `scoring/<season>.yaml` is committed before its first run.

## Layout

| Path | What it is |
|---|---|
| `lka_quiet/` | Standard-library Python: run records and validity (`record`, `perf`, `sysfs`), the root helper (`helper`), sessions (`session`, `plan`), scoring (`board`, `stats`, `rng`), the site (`site`, `charts`), builds (`build`), the quiet state (`quiet`), preflight and calibration, and a round's inputs (`encode`, `twins`). |
| `quietbox/` | The box: `lka-shim.c` (the last step before a checker), `lka-measure`, `lka-quiet`, `lka-preflight` (root commands), `provision.sh`, `calibrate.py`, the runner service, the selftest. |
| `machines/` | Machine profiles: lanes, the conditions a run is held to, paths. Calibrated values are written in by `quietbox/calibrate.py` and committed. |
| `scoring/` | One file per season: metrics, families, categories, statistics. |
| `templates/quiet/`, `templates/static/quiet.*` | The board's pages, stylesheet (light and dark) and its one small script. |
| `tests_lka/` | The unit tests, and `mutants.py`, which checks that each promise above has a test that fails without it. |
| `.github/workflows/quiet-board.yml` | The board, triggered as the Arena's own site is: build, measure and score on the box; publish (`quiet-publish.yml`). |
| `.github/workflows/quiet-import.yml` | The first import, by hand: every kernel built and run once, one at a time, then ranked. |
| `.github/workflows/quiet-pr.yml` | A pull request's checkers measured on the box, once approved, and reported on the PR. |
| `.github/actions/quiet-round` | A round's seed and inputs, and the kernel that verifies its twins. |

## Checking it, without a quiet box

```sh
nix develop --command python3 -m unittest discover -s tests_lka -t .   # every unit test
nix develop --command python3 tests_lka/mutants.py                      # every mutant must be killed
quietbox/selftest-in-container.sh                                       # the measurement chain, end to end, in Docker

# the latest official round under the board's rules (a preview, labelled as such on every page,
# naming the round's revision and when it was published), rendered into _out
nix develop --command quietbox/preview-live.sh
```

## Standing up a quiet box

1. Install Ubuntu Server 24.04 on a machine that will do nothing else. In its firmware, fix the fan
   at full speed and note the power limits.
2. `sudo quietbox/provision.sh machines/<machine>.yaml` from a checkout: packages, Nix, accounts,
   the sandbox, the egress policy, the root helpers, the runner service; it ends with the selftest
   and refuses to finish unless every check passes.
3. `sudo quietbox/bringup.sh machines/<machine>.yaml` runs the rest in order, each phase idempotent:
   it builds the twin-verifying kernel, draws the round seed, fetches the Arena's published round,
   exports any test no export holds, stages the round's inputs (the manifest), builds the reference
   checker, calibrates (the soak for the clock, then the sentinel band, the box factor and the noise
   floor, then `write`), records the machine's epoch, and enables the runner. A single phase runs on
   its own, e.g. `bringup.sh machines/<machine>.yaml stage`. After `calibrate` it stops to have you
   **commit the calibrated profile** and re-run `provision.sh` so `/etc/lka/machine.json` matches it;
   then `bringup.sh machines/<machine>.yaml epoch` re-records the epoch (the profile is part of the
   fingerprint) and `... runner` enables the runner once the token is in place.
4. Before `bringup.sh` reaches its `runner` phase, put a fine-grained token for the repository
   (Administration: read and write) in `/etc/lka/github-token` (mode 0600, root).
5. In the repository: set the variable `QUIET_BOARD` to `on`, enable Pages from GitHub Actions,
   require approval for workflows from outside collaborators, disable `build-and-deploy.yml` (it
   deploys the official Arena's Pages), and create the environment `quiet-box` with yourself as a
   required reviewer: no pull request's checker reaches the box until you approve its run.
6. Run the **Quiet board import** workflow once: it merges every checker and test from
   leanprover/lean-kernel-arena, then on the box builds each kernel from scratch and runs it once on
   every test, one kernel at a time (each one's verdicts and first timings show up as its own job),
   then ranks them all in interleaved passes until every cell is measured enough, and publishes.

From then on the board triggers as the arena's own site does: a push to `master` builds, measures,
scores and deploys (with the quiet board as the home page and the official board at `arena.html`);
a manual run can rebuild every checker. A pull request is checked on GitHub's runners, and once you
approve it in the `quiet-box` environment, the checkers it adds or changes are built on the box as
`pr<N>-<name>` (their own users, homes and records, so they never stand in for the board's), measured
on the round's inputs beside every checker, and their places reported on the pull request. That
workflow and all of the board's code always come from `master`; the pull request is only read, and
its checkers run only in the sandbox. When it closes, its checkers' users and homes are removed. The nightly sync merges upstream and starts the board when something new landed, and
a nightly run continues a round that one job could not finish.

Any change to the machine (kernel, firmware, microcode, the clock, the helper) is a new epoch:
`lka-preflight --check` refuses to start until a new epoch is written on purpose, and runs of
different epochs never share a board.

## Re-scoring

Every run record of an epoch is published as a release asset (`quiet-<epoch>`). With them and the
staged-tests manifest, `lka.py quiet-score` reproduces `board.json` byte for byte.
