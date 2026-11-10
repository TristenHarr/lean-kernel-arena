# The Quiet Board

An unofficial companion to the Lean Kernel Arena: every Arena checker, built from its entry as
written, run on the Arena's own test inputs (byte for byte), on one dedicated machine that does
nothing else, and ranked on what its users live with: wall time on one core and on four, peak
memory, energy, and soundness. The site is a strict addition to the Arena's: `lka.py build-site
--board board.json` renders the Arena's pages exactly as before, and the board under `quiet/`.

The full method, with every rule typeset, is the site's methodology page
(`templates/quiet/methodology.html`). In short:

- **Measure the target.** Scored: wall time (4 cores and 1 core), peak memory of every process of
  the checker together, package energy. Recorded and shown, not scored: instructions, cycles, IPC.
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
| `lka_quiet/` | Standard-library Python: run records and validity (`record`, `perf`, `sysfs`), the root helper (`helper`), sessions (`session`, `plan`), scoring (`board`, `stats`, `rng`), the site (`site`, `charts`), builds (`build`), the quiet state (`quiet`), preflight and calibration. |
| `quietbox/` | The box: `lka-shim.c` (the last step before a checker), `lka-measure`, `lka-quiet`, `lka-preflight` (root commands), `provision.sh`, `calibrate.py`, the runner service, the selftest. |
| `machines/` | Machine profiles: lanes, the conditions a run is held to, paths. Calibrated values are written in by `quietbox/calibrate.py` and committed. |
| `scoring/` | One file per season: metrics, families, categories, statistics. |
| `templates/quiet/`, `templates/static/quiet.*` | The board's pages, stylesheet (light and dark) and its one small script. |
| `tests_lka/` | The unit tests, and `mutants.py`, which checks that each promise above has a test that fails without it. |
| `.github/workflows/quiet-board.yml` | Build, measure and score on the box; publish from GitHub. |

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
3. Stage the inputs and build the reference checker:
   `lka.py quiet-stage ...` and `lka.py quiet-build --checker official --results /srv/lka/results`.
4. Calibrate as root: `quietbox/calibrate.py soak`, then `sentinel` and `box` with the chosen clock,
   then `noise` with each quieting knob, then `write`. Commit the calibrated profile and re-run
   `provision.sh` so `/etc/lka/machine.json` matches it.
5. `sudo /usr/local/libexec/lka/lka-preflight --write` records the machine's epoch.
6. Put a fine-grained token for the repository (Administration: read and write) in
   `/etc/lka/github-token` (mode 0600, root), and `systemctl enable --now lka-runner`.
7. In the repository: set the variable `QUIET_BOARD` to `on`, enable Pages from GitHub Actions,
   require approval for workflows from outside collaborators, and disable `build-and-deploy.yml`
   (it deploys the official Arena's Pages).
8. Run the **Quiet board setup** workflow once: it merges every checker and test from
   leanprover/lean-kernel-arena, rebuilds every checker on the box and starts measuring.

From then on the board triggers as the arena's own site does: a push to `master` builds, measures,
scores and deploys (with the quiet board as the home page and the official board at `arena.html`);
a pull request is checked on GitHub's runners and never reaches the box; a manual run can rebuild
every checker. The nightly sync merges upstream and starts the board when something new landed, and
a nightly run continues a round that one job could not finish.

Any change to the machine (kernel, firmware, microcode, the clock, the helper) is a new epoch:
`lka-preflight --check` refuses to start until a new epoch is written on purpose, and runs of
different epochs never share a board.

## Re-scoring

Every run record of an epoch is published as a release asset (`quiet-<epoch>`). With them and the
staged-tests manifest, `lka.py quiet-score` reproduces `board.json` byte for byte.
