# Multi-repository git status — assessment and staged plan

**Status:** R1-R4 implemented 2026-08-22 against `9612548`; suites green at 1177 pytest, 990
vitest (from 1088 / 990). R5 and R6 are not started and R7 is still a note. The decisions in
section 2 were all confirmed as written.

Written 2026-08-17 against `40b03f3`. The measurements in section 0 and the line numbers
throughout are from that commit, so they have drifted: `status.py` in particular grew the
fan-out and every line number in section 1 is now short by roughly that much.

**One thing here is wrong and was corrected during implementation** -- the per-repo cap, in
section R2. See the note beside it.

Scope: make the existing bottom-right status quadrant answer for **every git checkout found
under the observed root** when the root itself is not inside a checkout (the `~/projects/{a,b,c}`
workspace layout). No new panel, no new socket frame kind.

---

## 0. Baseline, measured on this host

| Measurement | Command | Result |
|---|---|---|
| Backend suite before any change | `.venv/bin/pytest -q` | `1088 passed, 20 skipped in 28.89s` |
| Frontend suite before any change | `node node_modules/vitest/vitest.mjs run` | `990 passed (990)` in 9.05 s |
| Area touched by this plan | `pytest tests/test_status.py tests/test_repo.py tests/test_file_view.py tests/test_diff.py tests/test_hub_status.py tests/test_tree.py` | `248 passed in 3.09s` |
| One `git status --porcelain -z` fork, warm cache | 5 real repos under `~/projects`, sequential | **3.9 ms each** (20 ms total) |
| Same, cold cache | first run of the loop | **11.6 ms each** (58 ms total) |
| Downward discovery walk, `~/projects`, depth 1/2/3 | `os.scandir`, prune `IGNORED_DIRS` + dotted, stop at `.git` | 7 dirs, **0.2–0.4 ms**, 5 repos |
| Same walk rooted at `$HOME`, depth 3 | as above | 26 dirs, **< 1 ms**, 5 repos |

Two of these decide the design. Discovery is **50–100× cheaper than the forks it precedes**, so
it needs no cache. And a fork is ~4 ms warm but its timeout is 5 s, so the *worst* case — not the
measured one — is what has to be bounded.

---

## 1. Assessment: how the status path is shaped today

### The five seams, and which are load-bearing

`status.py` is already split the way this feature needs. `git_status` is the only impure function
in it; `parse_status`, `relativize` and `status_frame` are pure and pinned by 62 tests. That split
is **load-bearing** and the plan keeps it: everything new that decides something goes in a pure
function, and the one impure function only gains an orchestration branch.

`gitcmd.run_git` is the single fork point, and it already carries the whole discipline: never
`shell=True`, never raises, never hangs, kill + close transport + reap on timeout. N repos means
N calls to it, each inheriting all of that for free. **This is the seam that makes the feature
cheap**; nothing new forks `git`.

`file_view.resolve_inside` is the security chokepoint for the click path, and it is
*orthogonal* to which repository owns the file — it answers "does this land under the observed
root", which stays the right question with N sub-repos. **Load-bearing, unchanged, and the new
code must consume its output rather than re-deriving from the raw string.**

`statusList.ts` derives `visible` from the entry count and never from `repo`. That is why the
whole backend feature can ship with **zero frontend change**: prefixed paths are still paths, the
panel appears, the rows sort, `splitPath` dims `peruca/src/` and names `x.ts`. Load-bearing, and
it is the single biggest reason this feature is small.

`EventHub.set_status`'s replaceable slot + dedupe + position before the seed in
`replay_messages()` is untouched by anything here. Accidental to this feature; leave it alone.

### The three things that are actually in the way

1. **`git_status` asks one question and gets one answer.** `find_checkout_root(root)` walks
   *upward*; a container root has no `.git` at or above it, so the function returns `None`
   before forking anything (`rhizome_graph/status.py:245-247`). There is no downward question in
   the codebase at all — `repo.py`'s three functions all walk up, and `tree.scan_tree` walks down
   but prunes every dotted directory, so it can never *see* a `.git`.
2. **`relativize` is a filter in one direction only.** `rhizome_graph/status.py:154-192` handles
   observed-inside-checkout (strip a prefix, or drop). The multi-repo case is the inverse
   (checkout-inside-observed → prepend). Its 12 pinned tests describe a *filter*; the new need is
   a *map*.
3. **`git_diff` runs with `cwd` = the observed root** (`daemon/server.py:713` →
   `file_view.py:108` → `diff.py:96`). In a container that is not a repository, so `git` exits
   128, `run_git` answers `None`, and every click falls through to the text branch. Existing
   files still open; a **deleted** file — the exact case `file_view`'s diff-before-existence
   ordering was built for, and the row a user most wants to click in this panel — answers
   `no such file`.

### One defect the feature widens, that already exists

`Session.publish_status` (`daemon/server.py:598-615`) awaits `git_status(self.root)` and then
calls `self.hub.set_status(...)` **unconditionally**. If the root is switched while that await is
in flight, the stale answer overwrites the fresh one. Today the window is one fork (≤ 5 s). With
16 repos at concurrency 4 it becomes ≤ 20 s of the panel listing files of a workspace nobody is
watching. This is worth fixing on its own merits and must be fixed before the fan-out lands.

### What `repo: true/false` actually costs

`grep` over `web/src/*.ts` finds **no consumer of `status.repo` outside `protocol.ts`**. The flag
is parsed, typed, documented and never read. Redefining it as "at least one checkout is described
by this frame" therefore changes nothing on screen today, and keeps the `None` vs `[]` distinction
in `status_frame` doing exactly the job it does now.

---

## 2. Decisions to confirm before step 1

These are the assumptions the plan is built on. Each one is a place where I chose; say so if you
would have chosen otherwise, because steps 1–4 encode them.

1. **Upward wins.** If `find_checkout_root(root)` answers non-`None` — root *is* a checkout, or
   root is a subdirectory of one — behaviour is byte-for-byte today's, and no downward walk
   happens at all. Consequence: a repository that *contains* vendored checkouts keeps its single
   repo panel. This is what makes backwards compatibility structural rather than test-by-test.
2. **Discovery does not descend into a checkout it found.** A submodule or a nested checkout is
   reported by its parent (`git status` shows it as one modified entry). Descending would
   double-count and multiply the forks.
3. **Four constants:** `MAX_DEPTH = 3` (covers `~/projects/a` and `~/src/github.com/org/repo`),
   `MAX_CHECKOUTS = 16`, `MAX_SCANNED_DIRS = 4000`, `MAX_CONCURRENT_STATUS = 4`. Together they
   bound a poll round at 4 waves × 5 s = **20 s worst case**, and `_status_busy` stops rounds
   stacking.
4. **No discovery cache.** Re-walk every poll. Measured at 0.2–0.4 ms against ~20 ms of forks, and
   it means a `git clone` into the workspace shows up within one poll with no invalidation logic
   to get wrong.
5. **Fairness by round-robin interleave, not by a per-repo quota.** See R2 for why a quota is the
   wrong shape.
6. **Discovery reuses `tree.IGNORED_DIRS` + the dotted-directory skip.** Consequence: a checkout
   at `~/.dotfiles`, or one under `vendor/`, is never discovered. Justification: the graph does
   not draw those files either (`scan_tree` prunes them identically), so a row for one would
   point at a node that does not exist. Confirm you accept that.
7. **Phase 1 ships with no frontend change.** Grouping the panel per repository (phase 3) is a
   separate, optional decision.
8. **A `.git` *file* counts** (worktree, submodule), same as `repo._find_dot_git` treats it.

---

## 3. The plan

Ranked, ordered, and every step is one RED test plus one GREEN implementation. Both suites are
green between any two steps, so the work can stop anywhere.

New test files throughout — `tests/test_checkouts.py`, `tests/test_status_multi_repo.py` — so
`tests/test_status.py`'s 62 assertions stay byte-identical and a reviewer can see nothing moved.

---

### R1 — There is no downward "which checkouts are under here" question. **Rank: now**

**What is missing.** Every repository question in the tree walks upward
(`rhizome_graph/repo.py:29-48`, `:51-65`, `:68-87`). The one downward walk,
`rhizome_graph/tree.py:86-99`, prunes every dotted directory in place, so it can never observe a
`.git`. Nothing can answer "what checkouts sit below this directory".

**Where.** New module `rhizome_graph/checkouts.py`. Not in `status.py` (it is not about the
porcelain format, and `file_view` will want it too), not in `repo.py` (whose entire docstring is
the upward walk and the files-never-subprocess doctrine, both irrelevant here).

**Why it costs to leave it out.** Without it the feature has no input. Putting it in `status.py`
instead costs the next change: the click router (R4) would then import the status module to ask a
path question, and `file_view` would depend on the porcelain parser.

**Target shape.**

```
find_checkouts(root, max_depth=3, max_checkouts=16, max_dirs=4000) -> list[str]
    # checkout prefixes relative to root, sorted, "/" separated.
    # [""] when root itself holds .git; [] when there are none.

owning_checkout(observed_root, absolute_path) -> str | None
    # the checkout root at-or-above absolute_path that is itself at-or-under
    # observed_root, else None. Wraps repo.find_checkout_root; forks nothing.
```

Both are filesystem-reading and pure of policy: no `git`, no network, no state. What stops the
boundary being crossed later: `checkouts.py` imports `repo` and `tree` and nothing else — no
`gitcmd`, no `subprocess`. The module docstring says so, the way `highlight.ts` says "no shiki
outside this file".

**Steps.**

| # | RED (developer-tester) | GREEN (developer-backend) |
|---|---|---|
| 1.1 | `tests/test_checkouts.py`: a container holding `a/.git` and `b/.git` and a plain `c/` answers `["a", "b"]`, sorted; today `find_checkouts` does not exist. | `checkouts.find_checkouts`, `os.scandir`-based, depth-first, prunes `tree._is_ignored_dir` names and symlinked dirs. |
| 1.2 | RED: a root that itself holds `.git` answers `[""]` and does **not** list its children's checkouts. | the at-root branch, and the "stop on found" rule. |
| 1.3 | RED: a checkout at depth 3 (`org/repo/.git`) is found; one at depth 4 is not. `MAX_DEPTH == 3`. | the depth bound. |

> **Ambiguity resolved while implementing.** This row's `org/repo/.git` is a two-segment prefix,
> while decision 3 justifies `MAX_DEPTH = 3` as covering `~/src/github.com/org/repo`, a
> three-segment one. Those are different bounds. It shipped as **segments of the returned
> prefix** -- the reading decision 3's own rationale requires, and one under which this row's
> claim is true as well. So `github.com/org/repo` is found and `a/b/c/d` is not; the bound also
> saves the directory open, since `c` is never scanned.
| 1.4 | RED: 20 sibling checkouts answer 16 (`MAX_CHECKOUTS`), and a tree of 5000 empty dirs stops at `MAX_SCANNED_DIRS` without raising and without hanging. | the two budgets. |
| 1.5 | RED: a `.git` *file* (worktree marker) counts; a symlinked directory is not followed; an unreadable directory yields fewer results, never an exception. | `os.path.exists` on the candidate, `follow_symlinks=False`, `try/except OSError` per directory. |
| 1.6 | RED: `owning_checkout(container, container/a/src/x.py)` is `container/a`; for a path whose nearest checkout is *above* the observed root it is `None`; for a root that is itself the checkout it is that root. | `owning_checkout` over `repo.find_checkout_root`, comparing with `os.path.realpath` on **both** sides (`find_checkout_root` returns `abspath`; a root with a symlinked component would otherwise silently never match). |

**Test to write first.** 1.1 — property: *discovery finds a checkout that is a direct child of the
observed root*. Input that trips it today: `tmp_path/{a/.git, b/.git, c/}` — the module does not
exist, so the import fails, which is the right RED.

**Owner.** `developer-tester` → `developer-backend`.

---

### R2 — `git_status` has no branch for "the root is not a checkout but contains some". **Rank: now**

**What is missing.** `rhizome_graph/status.py:244-253` returns `None` the moment the upward walk
comes back empty. Everything after it — the fork, the parse, the relativize — is reachable only in
the single-repo case.

**Where.** `rhizome_graph/status.py:245` (the early return), plus two new pure functions in the
same module.

**Why it costs.** This is the feature. And the shape chosen here decides whether the *next* change
— a per-repo branch, a per-repo header — is a parameter or a rewrite.

**Target shape.**

```
git_status(root, timeout):
    checkout_root = find_checkout_root(root)
    if checkout_root is not None:
        <today's four lines, unchanged>          # backwards compatibility, structurally
    prefixes = await asyncio.to_thread(find_checkouts, root)
    if not prefixes: return None                 # still no fork, still repo: false
    groups = await <bounded gather of run_git per prefix>
    if every group failed: return None
    return interleave(groups)
```

Two new pure helpers:

- `prefix_entries(entries, prefix) -> list[StatusEntry]` — prepend `prefix + "/"` to each path and
  record `prefix` on the entry. A **map**, deliberately not folded into `relativize`, which is a
  **filter** with 12 tests describing dropping. One function with two moods is a function whose
  tests you have to re-read to know which mood each pins.
- `interleave(groups) -> list[StatusEntry]` — round-robin, stable.

**Why interleave rather than a per-repo cap.** `status_frame` cuts the head at 200. Over a list
ordered repo-by-repo, one repo with 300 untracked files fills the whole cut and hides every other
repo — the exact failure this feature exists to prevent, just moved one level up. A per-repo quota
would have to be `200 // N`, i.e. a constant that depends on N, which is a tuning knob nobody can
choose. Round-robin makes the *existing* head-cut fair with no new constant and no signature
change: the first 200 of an interleaved list carry ~200/N from each repo. Wire order is irrelevant
to what is drawn, because `statusList.ts` sorts.

A per-repo cap is still applied **before** the interleave, purely as a memory bound: 16 repos ×
5000 entries parsed every 3 s to keep 200 is garbage the loop does not need.

> **Correction, made while implementing.** This paragraph originally set that cap at
> `DEFAULT_MAX_ENTRIES` (200) and argued it "can never bind before the global cut matters,
> because a lone repo can still fill all 200". That is true about *which rows are shown* and
> false about `truncated`. `status_frame` derives truncation as `len(entries) > len(shown)`, so
> a lone sub-repo with 300 pending changes, capped to exactly 200, computes `200 > 200` ->
> `truncated: False`: the panel claims the list is complete over a list it cut. The same
> repository observed directly reports `True`. One repository, two claims about completeness,
> decided by whether the root happens to sit one directory higher -- and `truncated` exists
> precisely because "a silently cut list reads as the whole truth".
>
> The cap shipped as `MAX_ENTRIES_PER_CHECKOUT = DEFAULT_MAX_ENTRIES + 1`. Carrying one entry
> more than the frame can show keeps the signal exact in both directions: anything above the
> global cut still exceeds it after the per-repo cap, anything at or below it is untouched. The
> memory bound is unharmed -- 16 × 201 is the same nothing as 16 × 200. Four tests in
> `tests/test_status_multi_repo.py` pin it, the tightest being a repository of exactly 201
> entries, where a cap of 200 would cut away precisely the evidence of the cut.

**Concurrency.** `asyncio.Semaphore(MAX_CONCURRENT_STATUS)` created **inside the call**, never at
module level — a module-level semaphore binds to the loop that created it and breaks the moment a
second loop exists, which in this suite is every test.

**Steps.**

| # | RED (developer-tester) | GREEN (developer-backend) |
|---|---|---|
| 2.1 | `tests/test_status_multi_repo.py`: `prefix_entries([StatusEntry("src/x", "modified")], "a")` → path `a/src/x`, state unchanged, input list not mutated; `prefix=""` is identity. | `prefix_entries`, plus `StatusEntry.repo: str = ""` (defaulted, so every existing construction and the pinned frozen-pair test still pass). |
| 2.2 | RED: `interleave([[a1,a2,a3],[b1],[c1,c2]])` → `a1,b1,c1,a2,c2,a3`; empty groups vanish; a single group is returned in order (this is the single-repo invariant). | `interleave`. |
| 2.3 | RED, with real repos in `tmp_path`: a container with `a/` and `b/`, each with one modified file, answers both, paths prefixed, relative to the container. | the `find_checkouts` fallback branch in `git_status`, sequential for now. |
| 2.4 | RED: `tests/test_status.py::test_a_directory_outside_any_repository_does_not_even_fork` is **re-asserted verbatim in the new file** for an empty container — still zero forks. | nothing; it must already pass. This is the guard that the fallback is gated on discovery, not run blind. |
| 2.5 | RED: the fallback is not entered at all when the root is a checkout — a spy on `find_checkouts` records zero calls for a single-repo root and for a subdirectory of one. | the upward-first ordering, made explicit. |
| 2.6 | RED: with 8 sub-repos and a `run_git` stub that records concurrent entries, the high-water mark is ≤ 4; and one repo whose `git` fails does not lose the other seven. | the semaphore and `return_exceptions`-style tolerance. |
| 2.7 | RED: discovery runs off the loop — an `asyncio.to_thread` spy, or a `find_checkouts` that blocks while the loop services another task. | `await asyncio.to_thread(find_checkouts, root)`. |
| 2.8 | RED: 3 repos of 150 entries each, `status_frame(..., max_entries=200)` — every repo is represented in the 200. | nothing new; this passes because of 2.2. It is the fairness pin, and it belongs in the suite permanently. |

**Test to write first.** 2.1 — property: *an entry from a sub-repo is re-expressed relative to the
observed root by prepending the sub-repo's prefix*. Input that trips it today: `prefix_entries`
does not exist; `relativize(entries, "/c/a", "/c")` — the closest existing function — answers `[]`
for exactly this input, which is the bug in miniature.

**Owner.** `developer-tester` → `developer-backend`.

---

### R3 — `publish_status` can overwrite a fresh panel with an abandoned root's answer. **Rank: now**

**What is wrong.** `daemon/server.py:610-615` publishes whatever `git_status` returns, without
checking that the root it was asked about is still the root. Today the race window is one fork
(≤ 5 s); after R2 it is up to 20 s.

**Where.** `daemon/server.py:598-615` (`publish_status`), and the two callers that can interleave:
`daemon/server.py:653` (inside `switch_root`) and `daemon/server.py:692` (`poll_status`).

**Why it costs.** A `ctrl+L` switch away from a big workspace leaves the panel listing files that
do not exist under the new root, and every one of them is clickable — a click that will be refused
by `resolve_inside`, so the user gets an error panel for a row the page is showing them. It also
makes R2's 20 s ceiling a user-visible lie rather than a latency.

**Target shape.** `publish_status` captures `self.root` at entry and drops the result if
`self.root` has moved by the time the await returns. The drop is silent — the switch's own
`publish_status` (`server.py:653`) has already published the right frame, or is about to.

**Steps.**

| # | RED (developer-tester) | GREEN (developer-backend) |
|---|---|---|
| 3.1 | `tests/test_hub_status.py` (or a new sibling): a `Session` whose `git_status` is stubbed to change `session.root` mid-await publishes **nothing**; `hub._status` keeps the frame it had. | capture the root, compare after the await, return early. |
| 3.2 | RED: the ordinary case is untouched — a call with a stable root still publishes, and `_status_busy` is still cleared on the exception path. | keep the `try/finally`. |

**Test to write first.** 3.1 — property: *a status answer computed for a root that is no longer
observed is discarded, not published*. Input that trips it today: stub `git_status` to set
`session.root = "/elsewhere"` and return one entry; today that entry reaches `hub.set_status`.

**Owner.** `developer-tester` → `developer-backend`. This step is independent of R1 and R2 and can
land first.

---

### R4 — A click on a sub-repo row cannot reach `git diff`, and a deleted row is unopenable. **Rank: now**

**What is wrong.** `file_view` runs `git_diff` with `cwd` = the observed root
(`rhizome_graph/file_view.py:108` → `rhizome_graph/diff.py:96`). In a container that is not a
repository, so `git` exits 128, `run_git` answers `None`, and the diff route is dead for every
file in every sub-repo. Existing files fall through to text — which loses the whole point of the
panel, "what did the agent just do to this file". A **deleted** file reaches
`file_view.py:115-118` and answers `no such file`, undoing the ordering that
`rhizome_graph/file_view.py:20-25` exists to document.

**Where.** `rhizome_graph/file_view.py:102-118`, reached from `daemon/server.py:713`.

**Why it costs.** The status panel's most valuable row is the deleted one, and it is the one that
breaks. It also degrades silently: a text fallback looks like a working panel.

**Target shape.** After `resolve_inside` has answered — **not before, and from its output, never
from the raw string** — decide the diff's working directory:

```
checkout = owning_checkout(root, target)          # target is the RESOLVED path
if checkout is None or checkout == realpath(root):
    cwd, path = root, relative_path                # byte-for-byte today
else:
    cwd, path = checkout, relpath(target, checkout)
diff = await git_diff(cwd, path)
```

Three properties hold this together, and each is a test:

- **The chokepoint stays single and stays first.** `resolve_inside` is unchanged and still the
  only containment check; the new code consumes its output. There is no second path to a `cwd`.
- **`relpath(target, checkout)` cannot escape.** `checkout` was found by walking *up from*
  `target`, so `target` is inside it by construction — the result can never begin with `..`.
- **The single-repo case keeps the raw string.** Deliberate: `target` is a `realpath`, so a
  symlinked file would be diffed as its destination rather than as the link. Today's behaviour
  diffs the link. In the sub-repo branch that asymmetry is unavoidable (the checkout is only
  knowable from the resolved path) and is worth one line in the docstring; in the compat branch it
  is avoidable, so it is avoided.

**Steps.**

| # | RED (developer-tester) | GREEN (developer-backend) |
|---|---|---|
| 4.1 | `tests/test_file_view.py` sibling, real repos in `tmp_path`: a container with `a/` a checkout; a file inside `a` that is **deleted from disk but present in HEAD** answers `mode: "diff"` with the removal. Today it answers `error: "no such file"`. | the `owning_checkout` branch in `file_view`. |
| 4.2 | RED, spy on `git_diff`: a single-repo root still calls it with `(root, relative_path_as_received)` — same cwd, same string. | the `checkout == realpath(root)` compat branch. |
| 4.3 | RED: a root that is a *subdirectory* of a checkout still calls `git_diff(root, relative_path)` (the checkout is above the observed root → `None` → compat). | already covered by 4.2's branch; this pins it. |
| 4.4 | RED: a refused path (`../../etc/passwd`, an absolute path, a NUL byte) still answers `refused:` and `owning_checkout` is never called — a spy records zero calls. | ordering: `resolve_inside` first, unconditionally. |
| 4.5 | RED: a modified file inside sub-repo `a/` answers `mode: "diff"` and the diff body names the file's path *relative to `a`*, as `git` prints it. | nothing new; pins the `relpath` choice. |

**Test to write first.** 4.1 — property: *a file deleted from a sub-repository still opens as its
diff*. Input that trips it today: container root, `a/.git`, `a/x.txt` committed then `rm`'d, then
`file_view(container, "a/x.txt")` → `{"mode": "error", "error": "no such file"}`.

**Owner.** `developer-tester` → `developer-backend`.

---

### R5 — The panel does not say which repository a row belongs to. **Rank: next — needs your decision**

**What is missing.** After R1–R4 the feature works and the panel needs no change: `peruca/src/x.ts`
sorts and renders, and `splitPath` already dims the directory. What it does *not* do is group — the
sort is state-first (`web/src/statusList.ts:105-111`), so repo `a`'s modified files sit next to
repo `b`'s while repo `a`'s untracked files are 40 rows below. If "show the status of EACH
sub-repository" means the panel should read as sections, this step is the one that delivers it.

**Why it cannot be done in the frontend alone.** The repo boundary is not derivable from a flat
path: `a/b/c.ts` may belong to checkout `a` or to checkout `a/b`. The daemon has to say.

**Where.** `rhizome_graph/status.py:219-226` (`status_frame`'s entry serialization),
`web/src/protocol.ts:353-414`, `web/src/statusList.ts:99-122`, `web/src/statusHud.ts:36-57`,
`web/src/style.css:300-400`.

**Target shape.** Per-entry `repo` key, **emitted only when non-empty**. That is the existing
doctrine — "with no token available the frame carries **no** `token` key at all, not an empty
one" — and here it also means `tests/test_status.py::test_the_frame_carries_each_entry_as_a_path_and_a_state`,
which asserts exact dict equality on `{"path", "state"}`, **keeps passing untouched**. That test is
the reason the key must be conditional; a tester should verify it before writing anything else.

`parseStatus` degrades a missing or mistyped `repo` to `""` — per-entry, never costing the frame,
exactly as it already degrades a bad `state`.

Then one of two orderings; pick one:

- **(a) sort only** — `(repo, state rank, path)`. No new row variant, no CSS, ~10 lines in a pure
  module. Reads as sections without headers.
- **(b) header rows** — `StatusRow` grows a variant, `statusHud.buildRow` branches, CSS gains a
  header style. Reads better; costs a discriminated union in a pure module and a real DOM change,
  and `web/tests/statusList.test.ts` grows a shape.

I would ship (a) and stop, unless you already know the workspace has enough repos that headers
earn their keep. (b) is not wrong, it is just not free, and (a) is a strict prerequisite of it.

**Steps.**

| # | RED | GREEN |
|---|---|---|
| 5.1 | pytest: a frame built from entries with `repo=""` is **identical** to today's, key for key. | nothing — this must already pass. |
| 5.2 | pytest: an entry with `repo="a"` serializes with `"repo": "a"`. | conditional key in `status_frame`. |
| 5.3 | vitest `statusProtocol.test.ts`: `repo` absent → `""`; `repo: 42` → `""` and the row survives. | `parseStatus`. |
| 5.4 | vitest `statusList.test.ts`: rows group by `repo` (empty first), then state, then path; the cut still respects the order. | `buildStatusList`'s comparator. |
| 5.5 *(only if (b))* | vitest: a header row precedes each group, carries the repo name and its count, and is not clickable. | `statusList` row variant + `statusHud.buildRow` + CSS. |

**Test to write first.** 5.1 — property: *the single-repo frame is unchanged, key for key*. Input:
`status_frame([StatusEntry("a.txt", "modified")])` must equal today's dict exactly. It is a
regression jaw, and it is what lets 5.2 be written without fear.

**Owner.** 5.1–5.2 `developer-backend`; 5.3–5.5 `developer-frontend`.

---

### R6 — Per-repo branch in the panel. **Rank: noted, gated on R5(b)**

`read_branch(os.path.join(root, prefix))` is a file read of `.git/HEAD`, so it stays inside
`repo.py`'s "files, never `subprocess`" doctrine, and done inside the same `to_thread` as
discovery it costs nothing measurable (a few dozen bytes per repo per poll). But it has nowhere to
go until a header row exists, and the HUD caption is one line that cannot hold sixteen branches.

**Recommendation on the caption:** leave it. Over a container, `read_branch` answers `None` and
the caption shows the root with no branch, which is honest. Do not sum, do not pick one, do not
list. **No action.**

---

### R7 — A round deadline, and a discovery cache. **Rank: noted, with triggers**

Both are defensible and neither is justified by anything I measured, so they are written down
rather than built.

- **Round deadline.** Worst case per round is 20 s (4 waves × 5 s), during which `_status_busy`
  suppresses new rounds and the panel is stale. Trigger to build it: a real workspace where a
  round is observed above 15 s. The shape would be a single `asyncio.wait_for` around the gather,
  publishing whatever completed.
- **Discovery cache.** Measured at 0.2–0.4 ms against ~20 ms of forks; caching would save 2% of a
  poll and buy an invalidation bug. Trigger: discovery measured above 50 ms on a real root. The
  shape would be a `Session`-owned cache cleared in `switch_root` — never module-level state in
  `status.py`, which is stateless by design.

---

## 4. What conflicts with what

The three terms do not align here, and the plan resolves them in this order.

- **Maintainability vs performance, at discovery.** Re-walking every poll is the maintainable
  answer (no cache, no invalidation, new clones appear by themselves) *and* the cheap one, because
  the measurement says so. They agree only because of the measurement; had the walk been 50 ms the
  plan would be different, which is why the number is in the document.
- **Performance vs correctness, at the cap.** A per-repo quota is cheaper to compute and unfair;
  interleave is fair and allocates one list. The list is ≤ 3200 entries every 3 s on the daemon's
  loop — free at this rhythm. Fairness wins.
- **Security vs convenience, at the click.** The convenient shape is to derive the sub-repo from
  the incoming path string, which is cheap and needs no `realpath`. That would be a **second place
  a path is interpreted**, upstream of the chokepoint, and it is precisely how a chokepoint becomes
  bypassable. The plan pays for the resolved path first and derives everything from it. The cost is
  one behavioural asymmetry (symlinks in the sub-repo branch), stated out loud in R4 rather than
  hidden.

Nothing here reaches the network surface in a new way: no new frame kind, no new command, no new
path parameter, the same two gates (`control_allowed` then `token_matches`) in front of the same
`file` command. If R4 is implemented differently from the shape above — in particular if anything
derives a `cwd` before `resolve_inside` has answered — that is a finding for `security-auditor`,
not a style note.

---

## 5. What I examined and found sound

- **`rhizome_graph/gitcmd.py` as the single fork point.** N callers instead of 2 changes nothing
  about it; the kill/close/reap discipline is inherited whole. No second runner is needed and none
  is proposed.
- **`resolve_inside` as the click chokepoint** (`file_view.py:66-87`). It answers a question
  orthogonal to repository ownership, so the feature adds no path around it. It stays first, alone,
  and unchanged.
- **`status_frame`'s `None` vs `[]`** (`status.py:201-226`). Sufficient for the multi-repo case
  with no new field: "no checkout anywhere below" is `None`, "checkouts, all clean" is `[]`.
- **`statusList.ts`'s `visible` from the entry count, never from `repo`** (`statusList.ts:99-102`).
  This is what lets the entire backend feature ship with the frontend untouched.
- **`parseStatus`'s per-entry degradation** (`protocol.ts:398-414`). It is what makes the optional
  `repo` key in R5 safe to add without a version negotiation.
- **`find_checkout_root`'s upward walk** (`repo.py:68-87`). Reused untouched by both the
  backwards-compatibility branch and the click router. No change proposed.
- **`_status_busy`** (`server.py:577, 690`). Correct for round stacking, which is the job it was
  written for. It is *not* a staleness guard, and R3 adds the missing one beside it rather than
  overloading it.
- **`tests/test_status.py::test_a_directory_outside_any_repository_does_not_even_fork`**
  (`test_status.py:553-569`) — I expected this to collide with the feature and it does not: its
  fixture is an *empty* plain directory, so discovery finds nothing and nothing forks. The test
  stays valid and becomes a better test than it was, because it now also pins that the fallback is
  gated on discovery rather than run blind. R2 step 2.4 re-asserts it in the new file.

## 6. Where I stopped

- **Not read:** `daemon/watcher.py`, `web/src/main.ts` beyond the six lines that bind `openFile`
  and `statusHud`, and `web/src/style.css` beyond the `#status` selector names. If the panel gains
  header rows (R5(b)) the CSS needs a real read first.
- **Not measured:** `git status` on a large repository with a cold cache. My numbers are five
  small-to-medium repos on a warm page cache; the 5 s timeout exists because that case is slow, and
  the 20 s worst case is arithmetic from the constants, not an observation.
- **Not measured:** discovery over a hostile tree — a network mount, a directory with 10 000
  children, a symlink loop. `MAX_SCANNED_DIRS` is a guess at a safe ceiling, not a measured one.
  Step 1.4 makes it a pinned guess.
- **Not run:** the opt-in packaging tests (`RHIZOME_PACKAGE_TESTS=1`). Nothing in this plan touches
  packaging, so I judged them irrelevant rather than checking.
- **Not verified, and unverifiable here:** how a 16-repo panel looks. Row count, scroll depth, and
  whether prefixed paths make `splitPath`'s dimmed directory unreadable at the panel's width are
  visual questions, and this host has no browser (the same gap `CLAUDE.md` records for the read
  ring and the file viewer).
- **Estimated, not measured:** that the interleave's allocation is free at a 3 s rhythm. The
  ceiling that would make it matter is roughly `MAX_CHECKOUTS × per-repo cap` growing past ~50 000
  entries per round, which the two caps make impossible.
