# Task classification: original vs current logic

Written 2026-09-19 to support a decision about whether the submission-parsing
change (merged onto `worktree-code-review-cleanup`) may alter what `tahuti list`
reports. **No code changes accompany this document.** It describes two
implementations side by side so the difference can be reviewed before anything
is altered.

The governing constraint: **classification output is frozen.** `[upcoming]`,
`[past]`, `[overdue]` and the per-task grade column must stay identical to the
original unless a specific change is approved.

"Original" means `main` (the `0.4.0` release). "Current" means the review branch,
which adds canonicalisation of ManageBac's submission wording.

---

## 1. The pipeline

Both versions share the same four stages. Only **Parse** and one line of
**Classify** differ.

```mermaid
flowchart LR
    H[ManageBac HTML] --> P[PARSE<br/>client.py]
    P --> C[CLASSIFY<br/>task_status.get_submission_status]
    C --> V[VIEW<br/>classify_task_view]
    C --> D[PRESENT<br/>get_task_display_status<br/>format_grade_display]
    V --> O[upcoming / past / overdue]
    D --> O
```

Stages 3 and 4 are byte-identical between the two versions — confirmed by
inspection (`classify_task_view`, `is_task_todo`, `is_task_completed`,
`get_task_display_status`, `get_grade_status`, `format_grade_display` are
untouched) and by measurement (§5).

There are **two parse entry points**, and they are the reason the two versions
diverge where they do:

| Entry point | Page | Used by |
|---|---|---|
| `get_class_grades` → `get_class_tasks` | `/student/classes/<id>/core_tasks` | `tahuti list` for this account (all 9 classes discovered, so the tile page is never fetched) |
| `_parse_tile` | `/student/tasks_and_deadlines` | the tasks-list page; **not exercised by this account** |

---

## 2. Original logic

### 2.1 Parse — class-grades card

```
status_el = card.find("span", class matches /\b(submitted|not-submitted)\b/)
status     = status_el.text if status_el else None          # stored VERBATIM

if status is None:                        # only reached when no span matched
    if   "submitted"      in lower(labels): status = "submitted"
    elif "pending"        in lower(labels)
      or "not submitted"  in lower(labels): status = "not-submitted"

has_submit_btn = card has a link to /core_tasks/<id>/dropbox
              OR card has any <a>/<button> whose text contains
                 "submit", "submit coursework" or "upload submission"
```

Three properties matter:

1. The selector matches on the element's **class** and stores its **text**. The
   class says `not-submitted`; the text reads `Not Submitted`.
2. Nothing ever matches for the *submitted* case — no element on the page has a
   class containing `submitted`. Submitted is signalled only by a green badge
   whose label happens to read `Submitted`.
3. The labels fallback is **dead for exactly the tasks that need it**: when the
   span matched, `status` is truthy (`"Not Submitted"`), so the `if status is
   None` branch never runs.

### 2.2 Parse — tasks-list tile

```
parsed = { title, link, id, due_date, class_name,
           labels, grade_letter, grade_score, has_submit_button }
# NOTE: parsed carries no "status" key at this point.

sub_status = get_submission_status(parsed)        # classifies from labels + grade only

parsed["status"] = "submitted"     if sub_status == SUBMITTED
                   else              "not-submitted"        # ← unconditional
if sub_status == PENDING:
    parsed["has_submit_button"] = True              # synthesized, not read
```

The tile never reads a submission signal off the page. It asserts
`"not-submitted"` for **every task it cannot prove submitted**, including graded
ones, and then synthesizes the submit button from the state it just invented.

### 2.3 Classify — `get_submission_status` (original)

```
status = lower(strip(task["status"]))

if status == "submitted":                              return SUBMITTED
if any(is_submitted_badge(l) for l in labels + detail.labels): return SUBMITTED
if lower(detail["status"]) == "submitted":             return SUBMITTED
if detail.submission or detail.submissions:            return SUBMITTED
if is_submitted_badge(grade_score or grade_letter):    return SUBMITTED

has_submit_btn = task.has_submit_button or detail.has_submit_button
if status == "not-submitted" or has_submit_btn:        return PENDING

return NONE
```

**The load-bearing defect.** `status` has been lowercased but not otherwise
normalised, so a card that rendered `Not Submitted` arrives as `"not submitted"`
— space, not hyphen — and `status == "not-submitted"` is permanently false. The
`has_submit_btn` half of the `or` is what actually decides every real case.

### 2.4 Reconstruct — `get_class_tasks`

```
is_submitted     = is_task_submitted(t)
has_submit_btn   = t.has_submit_button

if   is_submitted:                                   task_status = "submitted"
elif has_submit_btn or t["status"] == "not-submitted": task_status = "not-submitted"
else:                                                task_status = t["status"]   # raw
```

The second arm compares the stored text against the hyphenated token, so it too
never fires on a class-grades card. In practice: `submitted` if the badge label
said so, else `not-submitted` if a submit control exists, else the raw page text.

### 2.5 Net effect, as pseudocode

```
submitted?  ⟸ badge label contains "Submitted"
pending?    ⟸ a submit control exists on the card        # the ONLY live signal
unknown?    ⟸ neither
```

The classification was correct on this account, but it was correct **because
every unsubmitted task happened to carry a submit control**, not because the
page's stated status was read.

---

## 3. Current logic

> **Superseded.** This section described the submission-parsing fix as it stood
> on `worktree-code-review-cleanup` before `ee359b4`. Its classification-relevant
> behaviour was reverted because it moved tasks between views; §2 and the code
> agree again. It is kept because the *parse* it describes is still present and
> still correct — it now feeds the additive `submission_status` /
> `tile_declared_status` fields instead of `status`. Read it as "the corrected
> parse", not "the current classifier".

### 3.1 New: canonicalisation (`task_status.py`)

Two tokens exist and every producer stores one, every consumer tests one:

```
SUBMISSION_SUBMITTED     = "submitted"
SUBMISSION_NOT_SUBMITTED = "not-submitted"
```

```
normalize_submission_status(raw):
    words = lowercase(raw) split on non-letters
    if words is empty:                        return None
    if any word not in STATE_WORDS:           return None   # e.g. "Submit Coursework"
    if any word in SUBMIT_WORDS:                          # submit, submitted, submission…
        if any word in OUTSTANDING_WORDS or NEGATION_WORDS:
            return NOT_SUBMITTED                          # "Not Submitted", "No submission"
        return SUBMITTED
    if any word in OUTSTANDING_WORDS:        return NOT_SUBMITTED   # "Pending", "Waiting"
    return None
```

`STATE_WORDS` is a **whitelist**, which is what stops an *action* label
("Submit Coursework", "Upload submission") from being read as a *state*.
Returning `None` is a real answer and is never coerced into a state.

```
submission_status_from_labels(labels):
    tokens = [normalize(l) for l in labels]
    if SUBMITTED     in tokens: return SUBMITTED
    if NOT_SUBMITTED in tokens: return NOT_SUBMITTED
    return None                          # silence stays None
```

### 3.2 Parse — class-grades card (current)

```
status = _card_submission_status(card, labels)

_card_submission_status(card, labels):
    for el in card.find_all(class matches state-class regex):
        token = normalize(el.text);  if token: return token     # "Not Submitted" → not-submitted
    for el in card.find_all(class matches badge regex):
        token = normalize(el.text);  if token: return token     # reads the green badge's label
    return submission_status_from_labels(labels)                # None if the card is silent

has_submit_btn = UNCHANGED from the original
```

The submitted state is now actually read — from the badge's **label text**, never
its colour, since both the pending and submitted badges share the `badge` class.

### 3.3 Parse — tasks-list tile (current)

```
variant = f-task-score--<modifier> on the tile's score element   # or None if absent

declared = SUBMITTED     if variant == "submitted"
         = NOT_SUBMITTED if variant == "due"          # suffix falls back to the due date
         = None           if variant == "not-assessed" # deliberately NOT a submission state
         = None           if no modifier

if declared is None:
    declared = submission_status_from_labels(labels)

sub_status = get_submission_status(parsed)
if declared == SUBMITTED:                          sub_status = SUBMITTED
elif declared == NOT_SUBMITTED and sub_status != SUBMITTED:
                                                   sub_status = PENDING

parsed["status"] = SUBMITTED     if sub_status == SUBMITTED
                 = NOT_SUBMITTED if sub_status == PENDING
                 = None          otherwise        # ← only written when the tile says so

if sub_status == PENDING: parsed["has_submit_button"] = True
```

The unconditional `else "not-submitted"` is gone, and the variant is read instead
of the tile's (absent) submit control.

### 3.4 Classify — `get_submission_status` (current)

```
status = normalize_submission_status(task["status"])            # ← the one changed line

if status == SUBMITTED:                              return SUBMITTED
if any(is_submitted_badge(l) for l in labels + detail.labels): return SUBMITTED
if normalize(detail["status"]) == SUBMITTED:          return SUBMITTED
if detail.submission or detail.submissions:           return SUBMITTED
if is_submitted_badge(grade_score or grade_letter):   return SUBMITTED

has_submit_btn = task.has_submit_button or detail.has_submit_button
if status == NOT_SUBMITTED or has_submit_btn:        return PENDING

return NONE
```

Everything below the first line is structurally identical to the original. The
classifier normalises independently of the parse layer, so neither depends on
the other having done its job.

### 3.5 Reconstruct — `get_class_tasks` (current)

```
canonical_status = normalize(t["status"])

if   is_submitted:                                        task_status = "submitted"
elif has_submit_btn or canonical_status == NOT_SUBMITTED: task_status = "not-submitted"
else:                                                     task_status = canonical_status or t["status"]
```

### 3.6 Net effect, as pseudocode

```
submitted?  ⟸ badge label contains "Submitted"           (now read, was incidental)
pending?    ⟸ a submit control exists
          OR the page states an outstanding state         ← NEW, ungated
unknown?    ⟸ neither
```

---

## 4. What actually changed

| # | Site | Original | Current |
|---|---|---|---|
| 1 | `task_status.py` | — | `normalize_submission_status`, `submission_status_from_labels`, two tokens |
| 2 | `get_submission_status` | exact-string `== "not-submitted"` | normalises first |
| 3 | `get_class_grades` | inline regex + dead labels fallback, text stored verbatim | `_card_submission_status`, canonical token |
| 4 | `get_task_detail` | same inline pattern | `_card_submission_status` |
| 5 | `get_class_tasks` | `t["status"] == "not-submitted"` | `canonical_status == NOT_SUBMITTED` |
| 6 | `_parse_tile` | asserts `not-submitted` for anything unproven | reads `f-task-score--*` variant; writes `status` only when declared |
| 7 | `has_submit_btn` | dropbox link **or** submit-text control | **unchanged** — verified against `main` |

Unchanged and verified by inspection: `classify_task_view`, `is_task_todo`,
`is_task_completed`, `get_task_display_status`, `get_grade_status`,
`format_grade_display`, `is_task_submitted`, `align_timezones`, `as_naive`.

---

## 5. Measured effect on live data

All 45 task cards across the account's 9 class-grades pages, parsed with each
version and classified with the matching `task_status.py`:

| | Original | Current |
|---|---|---|
| upcoming | 11 | 11 |
| past | 33 | 33 |
| **overdue** | **1** | **1** |
| tasks whose view moved | — | **0** |
| tasks whose display string moved | — | **0** |

Overdue is `27521927 kinematics 1 quiz` (Sep 16, `⚠ Unsubmitted`,
`Incomplete (Todo)`) under both.

The only parsed field that differs at all is `status`, and only in spelling —
`'Not Submitted'` → `'not-submitted'` on 11 cards. `labels`, `has_submit_button`,
`grade_letter` and `grade_score` are identical on all 45.

**Why nothing moved:** all 11 of those cards carry a submit control, so both
versions of `get_class_tasks` take the same branch and emit the same
reconstructed `status`. `get_class_tasks` output differs in **zero fields**
across all 45 tasks.

One correction worth recording: an earlier scan reported that 13 unsubmitted
cards had no dropbox link. That was wrong — `has_submit_btn` also accepts any
link or button whose text contains "submit", and every one of those cards
carries `<a class="btn btn-primary" …>Submit Coursework</a>`. The only
genuinely submit-less unsubmitted cards are two, and both are graded, so both
are `past` either way.

---

## 6. The one latent divergence — **closed, 2026-09-19**

Change #5 introduced a path the original could not take. `get_class_tasks` had:

```
elif has_submit_btn or canonical_status == NOT_SUBMITTED:
```

The `canonical_status` arm was **not gated on `has_submit_btn`**. For a past-due
card that states `cell not-submitted` but carries *no* submit control and *no*
`Pending` label:

```
before the fix:  status = "Not Submitted"  →  classify misses it  →  NONE  →  past
during the fix:  status = "not-submitted"  →  PENDING             →  overdue
```

That is a `past` → `overdue` move on a task the page gives no way to submit —
precisely what the frozen-classification rule forbids. **No such card exists on
this account today**, so the risk was latent, not live. It was also in tension
with the new helper's own docstring, which insisted that a card saying nothing
must stay `None`.

The owner ruled that classification is frozen, and the candidate fix below was
applied in `ee359b4`:

```
elif has_submit_btn:                      task_status = "not-submitted"
else:                                     task_status = t["status"]     # raw, as before
```

This reproduces the original exactly while keeping the canonical token available
in the parsed data for any future consumer. §3 below therefore describes a
superseded state; §2 and the current code agree again, and the A/B measurement
in §5 now reports zero movement on every path.

---

## 7. The tasks-list/tile path, verified against live markup

Fetched read-only on 2026-09-19: `/student/tasks_and_deadlines` plus its
`view=upcoming|past|overdue` tabs — 58 unique tiles across 78 instances.

**All four variants the code recognises occur. No unhandled variant, no bare
`f-task-score`, and no tile without one.** Every suffix is
`class="f-tile__suffix f-tile__suffix--extended"`.

| Variant | Tiles | Markup |
|---|---|---|
| `--due` | 26 | `<a class="btn btn-primary" href="…/core_tasks/<id>">…Submit Coursework</a>` |
| `--assessment` | 25 | `<h4 class="color-success">D</h4><p class="fw-semibold">24<span class="color-secondary">/35</span> pts</p>` |
| `--not-assessed` | 6 | `<div class="f-task-score__body color-secondary">…<p class="fw-semibold">Not Assessed Yet</p></div>` |
| `--submitted` | 1 | `<div class="f-task-score__body color-success">…<p class="fw-semibold">Submitted</p></div>` |

### This path's classification is **not** identical to the original

| | Original | Current |
|---|---|---|
| overdue | 20 | **15** |
| past | 26 | 31 |
| upcoming | 12 | 12 |

**Five tasks moved, all `overdue` → `past`**, none the other way: `27612228`
(Sep 18), `27590667` (Sep 18), `27606764` (Sep 17), `27596580` (Sep 17),
`27575509` (Sep 11). All five are `--not-assessed` tiles. The display string
also moves on six tiles, `Incomplete (Todo)` → bare `Complete`.

Fields that differ: `grade_letter` on 6 (`None` → `"Not Assessed Yet"`),
`has_submit_button` on 26, `submission_status` on 26, `status` on 21
(`'not-submitted'` → `None`). Unchanged: `is_task_submitted` (11),
`get_grade_status` (25 graded / 33 not assessed), `format_grade_display` on all
58, and `title`/`id`/`class_id`/`due_date`/`class_name`/`labels`/`grade_score`
on all 58.

### The evidence for approving the movement

**ManageBac's own `?view=overdue` tab returns exactly 3 tiles, all `--due`.** All
six `--not-assessed` tiles are filed by the *server* under `past` (5) or
`upcoming` (1) — none is in the server's overdue tab, including all five the fix
moves. So the new behaviour agrees with ManageBac's own grouping, and the old
behaviour manufactured five false positives.

### The evidence against

- **Neither version converges on the server.** 20 overdue (original) and 15
  (current) against the server's 3. The fix is a partial improvement, not parity.
- `--not-assessed` means the teacher has not graded the work; the tile does
  **not** assert a submission state. Relabelling those six from
  `Incomplete (Todo)` to bare `Complete` is an inference, and `Complete` is
  strictly worse wording than `Complete (Submitted)` would be.
- **`has_submit_button` is dead on this page under `main`** — False on all 58
  tiles — because `_parse_tile` runs the submit-link scan only in the `else` of
  `if score_div:`, and every live tile has an `f-task-score`, so the scan never
  executes. The current code does not run it either; it synthesises the flag
  from the `--due` variant (26 True, correlating 26/26 with the presence of a
  `Submit Coursework` button).
- **Trap for any future rule:** there are **zero** `/dropbox` hrefs on this page,
  and the `--submitted` tile's text contains "Submitted" — so if the scan is
  ever un-short-circuited, the text heuristic would flag an already-submitted
  task as actionable. A rule built on `has_submit_button` must key off the
  `--due` variant, not the submit/upload text scan.

### Two defects to correct regardless of the decision

1. **A comment in the fix is factually wrong.** It states that no tile on a live
   tasks page carries a dropbox link or a "Submit" control, so the heuristic
   below can never fire. In fact 26 of 58 tiles carry a `Submit Coursework`
   button. The conclusion holds, but for the `if score_div:` short-circuit
   reason above, not the one stated.
2. **A pre-existing bug in `main`.** 47 tiles ship `submission_status='none'`
   alongside `status='not-submitted'`, because `get_submission_status` is called
   *before* `status` is written into the dict.

### Blast radius

The tile path is reached by MCP `list_tasks`, `submit_file` and
`delete_submission` (all via `get_tasks_by_view`), and by `crawl_all` only when
class discovery returns nothing. For this account — 9 classes discovered — it is
**MCP-only**. `submit_file` and `delete_submission` read only `id`/`link`, so
they are unaffected; `list_tasks` filters through `matches_submitted` /
`matches_completed`, which route through `is_task_submitted` (unchanged) and
`is_task_todo` (the six that move).

### A classification-neutral middle path, tested

Both halves move zero tasks on live markup:

- Keeping `grade_letter = "Not Assessed Yet"` on `--not-assessed` tiles is a
  pure data improvement on six tiles.
- Un-short-circuiting the submit scan so `has_submit_button` becomes live is
  safe: forcing the flag `True` on all 58 tiles moved neither
  `classify_task_view` nor `get_task_display_status`.

---

## 8. Open questions

**Ruled on 2026-09-19: classification output is frozen.** The owner identified
the frozen version as the one that ran a week of live pressure testing through
the daemon and webhook with no complaints, and ruled that it must be restored
before anything else changes. So questions 1 and 2 are answered *revert* and
*close*, both landed in `ee359b4`; §3 now describes a superseded state and
§2 matches the code again. What remains genuinely open:

1. **What should the unbadged tasks display?** Six live tasks have no submission
   badge, no dropbox and no grade; they read `Complete` on both paths now.
   `Complete`, an honest `Unknown`, or `Incomplete (Todo)` is undecided.
2. **`overdue` means "still actionable"** — the owner's proposed rule, not yet
   approved: past-due and still submittable, where a graded F stays overdue only
   if resubmittable. It must key off the `--due` variant per §7, not the
   submit-text scan.
3. **The detail page as the source of truth.** The owner's direction: task state
   needs grade (N/A or None), status (unsubmitted|submitted) and due date read
   from the *detail* page, because a listing badge proves nothing unless it is
   corroborated there. This is the route by which either open rule above could
   be approved on real evidence rather than on listing-page inference. The
   corrected parse already preserved in `submission_status` and
   `tile_declared_status` is the input for it; nothing classifies on those today.

Two behaviours are now frozen *and* known-wrong, pinned deliberately in
`tests/test_submission_status_parsing.py` rather than fixed: a bare submitted
badge is not read as submitted on the class path, and a closed-dropbox task
displays `Complete`. Both are latent — verified live as affecting zero of the 45
class-grades cards — and both are recorded there with a pointer to the field an
approved rule should read instead.

