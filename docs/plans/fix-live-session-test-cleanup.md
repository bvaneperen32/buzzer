# Fix: live-session sockets (B1, B2), the T4 wrong-version fixture, and test data

Status: implemented on `fix/dexter-live-session-test-cleanup`, integrated with `main` after T7
merged (§7), and committed. Verification is complete: the final-review corrections and the second
correction pass are verified by the automated and browser checks (§5.00), and the nine
physical-phone checks passed on the merged build (§8, user-reported).

**T7 dependency: met (2026-09-30).** This branch started from `feature/dexter-t7-question-types`
at `5016570`.
- T7 has since merged into `main`, squashed: `b0c18eb`, merged by `aad922e`.
- This branch then merged `origin/main` with a normal merge commit (`547507d`), keeping both
  histories.
- The bug-fix work was committed after that merge, once verified.
- The merge request compares against `main`; the user opens it. See §7 for how the integration
  went.

## 1. Scope

1. **B1 and B2, fixed together** (T7 spec §11 recorded both and said to fix B1 first or both at
   once).
2. **The T4 wrong-version fixture**, which T8 made obsolete.
3. **Browser-test data hygiene**: track and clean up the records the browser checks create.
4. **Test-running documentation**: the working directory, interpreter and `PYTHONPATH`.

Out of scope: T6, other product limitations, new features. CORS stays at the four original
localhost entries.

**Corrections after review (2026-09-30)**, before the phone checks:

1. `--report` of the cleanup tool made genuinely read-only (§3.6).
2. §7's merge instructions corrected.
3. Reconnect behaviour completed (§3.8–§3.10):
   - the phones hear the host is back;
   - the projector's controls wait for its rejoin;
   - a late answer is a nonfatal rejection.
4. Test-data hygiene extended to the integration suite's existing helpers (§3.11).

**Cleanup safety correction (2026-09-30, later):** a guest is deleted only on positive proof that
the run created it, never on its creation time (§3.12).

**Final-review corrections (2026-09-30, last):** the independent final review found one blocker
and four smaller items. All five are fixed here, each with a regression shown to fail without
its fix (§5.00):

1. **B-1, a late or dropped answer shown as answered at results.** The phone's results now say
   "No answer" from the server's own record for every question type (§3.14).
   - Fixing it showed that results could miss an answer acknowledged while they were being
     built, so showing results now closes the question and waits for answers in flight first
     (§3.14).
2. **The ledger dropped a minted email it couldn't look up** (while saying it had kept it), and
   with it the only proof that guest was the run's (§3.12).
3. **A player joining during the host-disconnect broadcast** could be told the host was still
   there (§3.4).
4. **An answer sent again after a reconnect could be recorded twice** (reproduced: two rows, two
   distribution counts, one lost score update). Each answer now counts once, and a retry after a
   failed attempt completes it (§3.13).
5. **The projector's gating test clicked a disabled button**, which proves nothing. It is
   replaced by a check on what the projector actually sends (§5.00).

**Second correction pass (2026-09-30, before phone sign-off):**

1. **Results could publish while an admitted answer was still recording.** After the bounded wait
   ran out, `_close_answers` only logged, and the results went out anyway. Now nothing is
   published while such an answer can still change them: the host gets `results_delayed`, and
   Show Results can be pressed again (§3.14).
2. **An answer whose Redis count failed after its MySQL commit, with no reconnect**, left the
   results disagreeing: answered, points and rank counted it, but the total and distribution
   didn't. The results now count such a stored answer in Redis first, once (§3.14).
   - The record used to say this case "becomes No answer". It doesn't: the answer is stored,
     and it counts (§6 corrected).
3. **The intermittent export-boundary failures** in T8's and T7's tests were diagnosed from their
   output: a meter read taken before an earlier update's post-response commit. Fixed in the
   test helpers by waiting for each update's commit (§5.00).
4. **The branch's tests were reviewed for purpose.** Overlapping tests were merged, and a weak
   check and a duplicate test were dropped, with no loss of coverage (§5.00).

| Problem | How it was reproduced | Result |
|---|---|---|
| B1 | A projector page (Playwright, production build) goes lobby → question → results → question → results | 5 socket.io WebSockets opened; should be 1 |
| B2 | Start a game over sockets, then read `room:{code}` from Redis | MySQL `IN_PROGRESS`, Redis `LOBBY` |
| T4 | `test_import_rejections_use_the_unprocessable_format[wrong-version]` | Fails: v2 is valid since T8, so the bundle fails later ("A version 2 bundle's 'images' must be a list") |
| Test data | Read-only count in the dev database | 118 courses from browser checks (`T7 checks`), 1,600 `@example.com` guest accounts; ownership of none is recorded |

## 3. Design decisions

### 3.1 B1: one socket per game screen, in both apps

- **Cause.** Each `GameLayout` socket effect depends on `navigate`. React Router 6.30 gives
  `navigate` a new identity on every pathname change, so each screen change closes the socket
  and opens a new one.
- **Fix.** The effect depends only on the room code. Handlers call navigation through a ref that
  every render updates, so they always use the current `navigate`. Cleanup removes every
  listener before disconnecting, so events for a socket that was torn down (StrictMode's double
  mount, or leaving the game) can't change state.

### 3.2 Player answers across a real reconnect

- **The loss.** While a socket is disconnected, socket.io-client buffers emits and sends them on
  reconnect *before* the `connect` handler sends `join_room`. The server handles events
  concurrently, so even an answer sent just after `join_room` can arrive before the server has
  seated the new socket. In both cases it drops the answer without a word, and the phone keeps
  saying "Answer submitted".
- **Readiness.** A socket is ready once the server has seated it. The server sends `sync_state`
  after that, so `sync_state` is the ready signal. The socket stops being ready at `connect` and
  at `disconnect`.
- **Pending answer.** `emitAnswer` stores the answer as pending and sends it only while the
  socket is ready. Otherwise it waits. After the next `sync_state`:

  | `sync_state` says | The phone |
  |---|---|
  | same question, open, not answered | sends the pending answer |
  | same question, already answered (the answer arrived before the drop, its ack didn't) | treats it as accepted and shows "Answer locked in!" |
  | locked, or a different question | drops the pending answer; the page shows the locked state, or moves on |

- **Acceptance comes only from the server.** The pending answer clears on `answer_received`
  (or its duplicate echo), on `answer_rejected` (§3.10), on results, on a different question,
  and on `error`. While it is
  pending, the question page says "Sending your answer…" instead of "Answer submitted — waiting
  for results…". "Answer locked in!" still appears only on the server's `answer_received`.
- **A repeated question is ignored once answered.** After a reconnect the server re-sends the
  open question (the late-join `new_question`) to a player who hasn't answered. That repeat can
  arrive after the re-sent answer's ack. It no longer sends an answered phone back to the
  question. A repeat for an unanswered question keeps the pending answer and the phone's last
  answer.
- **Double taps.** A second submit for the same question while one is pending is ignored on the
  client, as the page's local `submitted` flag used to do.

### 3.3 B2: the room status in Redis

- **Cause.** `_on_host_advance_impl` read `room_state` (LOBBY) before `game_service.start_game`
  wrote IN_PROGRESS to Redis, then wrote its own copy back.
- **Fix.** After `start_game` it re-reads the room and sets `status = IN_PROGRESS` explicitly.
  Every other reader of the Redis status (room cap, `ping`, guest join) accepts LOBBY and
  IN_PROGRESS alike.

### 3.4 Host disconnect and rejoin ordering

With B2 fixed, the documented disconnect behaviour runs for the first time: the lock, the timer
pause, `host_disconnected`, the 5-minute grace, and the unlock on rejoin. Two ordering problems
had to be closed first.

1. **An old socket's disconnect can arrive after the new socket has joined.** A network drop may
   only be noticed at the server's ping timeout, after the host has already reconnected.
   - `disconnect` now does nothing if another host socket of the session is still seated.
   - This also covers a second host (an admin) who is still connected.
2. **A disconnect and a rejoin can interleave** across `await`s. The disconnect's lock could
   then land after the rejoin's unlock, leaving the question locked and the abandon task running
   with the host connected.
   - Both run under a per-session `asyncio.Lock`.
   - The rejoin re-reads the room inside that lock.

Other rules:

- **A manual lock is not undone.** The existing flag (`question_locked_by_disconnect`) still
  decides what the rejoin unlocks.
- **A question whose time ran out stays locked.** If the time was up when the host dropped
  (remaining 0), the rejoin keeps the lock the timer would have set. Before, it unlocked with no
  timer running.
- **`on_rejoin_room` shares the code.** It takes the same helper (`_seat_host`), though no
  client sends it.
- **Dropped answers are logged.** A `submit_answer` from a socket that isn't seated now logs
  `submit_answer_before_join` instead of vanishing.
- **The state comes before the broadcast** (final-review correction 3).
  - `_host_left` stores the lock and registers the abandon task before it emits
    `question_locked` and `host_disconnected`.
  - It used to register the task after both emits. A player joining during them computed its
    `sync_state`'s `hostDisconnected` from the task's presence, got `false`, and could clear
    the banner the broadcast had just raised, with the host still away.
  - The task now starts when `abandonAt` is computed, so the announced deadline and the real
    one coincide; the rejoin still cancels it.

### 3.5 T4 fixture

- The `wrong-version` case uses version 3, which is genuinely unsupported.
- The expected prefix (`Unsupported version`), the 422 `UNPROCESSABLE` format and the
  no-partial-import assertion are unchanged. The application is not changed.

### 3.6 Test data: an ownership ledger

**The ledger.** `tests/ledger.py` writes one JSON file per run in the ledger directory:
`tests/.ledgers/` (git-ignored), or `BUZZER_TEST_LEDGERS`. The browser checks and the
integration suite (§3.11) share it.

- It records the run id, the script, the API base URL, the process id and host, when the run
  started, and every record the run creates. Each record carries its exact id and identifying
  fields: sessions, games, guest accounts, local users, courses.
- Each record is written to disk atomically as soon as its id comes back, so an interrupted run
  leaves a complete ledger behind. A run that creates nothing leaves no file.
- A guest's id comes from its own token, with no request in between.

**Teardown** runs in the scripts' `finally`, so it also runs after a failure or Ctrl-C.

- **Order:** sessions (which clears Redis), games (their questions, images, sessions and scores
  go with them), guests, users, courses.
- **Identity check.** Before each delete, it checks the record still matches what was recorded:
  a game's title, a guest's role and email, a user's username and role, a course's name and
  semester. A mismatch is reported and not deleted.
- **Guests need positive ownership** (§3.12). A guest is deleted only if its email was minted
  by the run. Any other guest record is reported and kept.
- **Creation-time check (secondary).** A record created before its ledger started (compared to
  the second, as MySQL stores it) can't be the run's. It is reported and forgotten, never
  deleted.
  - It never establishes ownership. That is the POST's own response for courses, games, rooms
    and users, and the minted email for guests.
  - The one exception is a record a person names explicitly: the phone checklist's
    `--game-id`.
- **Ledger updates.** A record leaves the ledger once it is gone, or once it is shown not to be
  the run's. The file is removed when it is empty; otherwise it keeps what's left, and the
  problems are printed.

**Courses.** There is no course delete in the API, and none is added.

- Test tooling deletes a recorded course by its exact id, name and semester, over the MySQL
  connection the integration suite's lock tests already use (`BUZZER_TEST_DB_*`, the backend's
  account).
- It first checks that no game or session uses the course. The database refuses otherwise
  anyway: those foreign keys are `NO ACTION`.
- A course's roster and access rows go with it (`CASCADE`).

**Interrupted runs.** `python tests/cleanup.py` tears down every ledger an interrupted run left
behind, from either suite, through the API that run used.

- It skips a ledger whose process is still running on this host (`--force` overrides).
- `--dry-run` shows what it would do.
- **`--report` is read-only.** It lists records that *look* like test data but aren't in any
  ledger (courses of the browser checks' semester, `@example.com` guests), and changes nothing:
  no record and no ledger file.
  - Their ownership can't be established, so they are only reported.
  - It can't be combined with a cleanup.
  - *Corrected 2026-09-30:* it used to run the ordinary teardown of every eligible ledger first,
    and only then report.

**The phone-checklist game.**

- `phone_checklist_setup.py` records its course (only if it created it) and game in a kept
  ledger (`phone-checklist.json`).
- `tests/cleanup.py` never touches a kept ledger. Only `phone_checklist_setup.py --cleanup`, the
  explicit step, deletes those records.
- The existing checklist game (1166, in course 990) predates the ledger. `--cleanup` and
  `--replace` report it and delete nothing.
  - It can be removed only by naming it: `--cleanup --game-id 1166`. The game must still have
    the checklist's title in the checklist's course.
  - Course 990 wasn't created under a ledger, so it is reported and kept.

### 3.7 A lost connection keeps the game screen (found while testing §3.2)

- **The problem.** Holding a phone offline in the browser checks showed that each failed
  reconnection attempt fires `connect_error`, and both `GameLayout`s turned that into the fatal
  error screen. That unmounts the game pages. A real outage longer than one retry would
  therefore:
  - wipe a board's placements or selection;
  - show "Back to Join", a way out of the game.

  The drop tests from T7 reconnected on the first attempt, so they never saw this.
- **The fix, in both apps.** Once the socket has connected, a `disconnect` or `connect_error`
  shows a "Connection lost — reconnecting…" banner over the mounted page. It shows only while
  socket.io will retry (`sock.active`), and clears once the server confirms the rejoin
  (`sync_state`), not merely on `connect`.
  - A first connection that fails, or one the server refuses, still gets the error screen
    (socket.io doesn't retry those).
  - The host's `disconnected` banner still exists; the phone's own banner takes precedence while
    it's shown.

### 3.8 The phones hear the host is back (correction 3)

- **Before:** the "Host disconnected" banner cleared only at the next question. There was no
  "host reconnected" signal, so after the host's return the phones kept saying it was gone,
  most visibly when a manual lock kept the question closed.
- **Now:**
  - When a host is seated while the grace period is running (the room was told
    `host_disconnected`), `_seat_host` sends `host_reconnected` to the room. It does so whether
    or not the question unlocks, so a manual lock stays and the banner still clears.
  - A player's `sync_state` carries `hostDisconnected`, so a phone that reconnects while the
    host is away shows the banner, and one that reconnects after the host's return doesn't.
  - A first join, or a second host joining, sends nothing.

### 3.9 The projector's controls wait for its rejoin (correction 3)

- **Before:** a projector click during its own reconnect was dropped by the server (the socket
  wasn't seated), or, buffered by socket.io, could land after the rejoin as a stale Next or Lock.
- **Now:** the host `GameLayout` has `canControl`: true only between its `sync_state` and the
  next `connect` or `disconnect`.
  - Start, Lock/Unlock, Show Results and Next are disabled until then.
  - `emitAdvance` / `emitLockQuestion` drop anything sent meanwhile, and never queue it.
  - The banner reads "Connection lost — reconnecting… Controls are paused until the game is
    back." and stays until the rejoin is confirmed.
  - Auto-advance's pause and the results countdown run only while `canControl`, and start again
    after a rejoin, so auto-advance never fires into the gap.

### 3.10 A late answer is a rejection, not an error (correction 3)

- **Before:** an answer arriving after its question locked, or after the question moved on,
  got `error`. The phone treated that as fatal: the game screen was replaced, with "Back to
  Join". That included a pending answer re-sent after a reconnect (§3.2).
- **Now:** the gateway sends `answer_rejected` (`questionId`, `reason`, `message`), a new
  event, to that socket:

  | Situation | `reason` |
  |---|---|
  | the question is locked | `locked` |
  | results shown, or a different question open | `closed` |

- **`error` is still used for the genuine failures:** a missing room or a session no longer in
  progress, malformed answers, and refused joins.
- **The phone:**
  - drops the pending answer;
  - marks the question closed, disabling the controls but keeping its selection or placements;
  - says "Too late — answers had already closed, so that answer didn't count.";
  - never shows "Answer locked in!".
- **What clears it:** an unlock reopens the question, and the next question or results carry
  on as usual.

### 3.11 The integration suite's test data (correction 4)

- **Before:** the suite's existing helpers leaked records. `create_guest_tokens` never deleted
  its guests, and `game_setup`, the T4 and T8 environments and several tests never deleted
  their courses. About 250 guests and 230 courses per complete run with the opt-in checks.
- **Now:** `conftest.py` records every creation from the responses themselves.
  - For the session, it wraps `httpx.Client.send` and `AsyncClient.send`. Every successful
    `POST` to a creating route is recorded by the id it returned, whichever helper sent it:
    - `/api/admin/courses`;
    - `/api/admin/games` and `/api/host/games`, and both import routes;
    - `/api/game/rooms`;
    - `/api/auth/guest`;
    - `/api/admin/users`.

    So `game_setup`, `create_room`, `create_guest_tokens`, `T4Env`, `T8Env` and tests' own
    requests are all covered without changes.
  - **Scope.** Records made during a test (its body and function fixtures) go into a per-test
    ledger, torn down after every other fixture of the test, whether it passed or failed.
    Records made by a module- or session-scoped fixture go into the run's ledger, torn down at
    the session's end. The nginx test's module fixture, for example, outlives each of its
    tests.
  - **Safety.** The §3.6 teardown applies:
    - an identity check;
    - positive guest ownership (§3.12);
    - the secondary creation-time check;
    - courses deleted only when unused.

    A problem is a pytest warning, and the record stays in the ledger for `tests/cleanup.py`.
    Records the run didn't create, including the phone checklist, never enter a ledger. No
    historical record is deleted by any pattern.
  - Existing fixture cleanups (`T4Env.cleanup`, `T8Env`'s) still run first; the ledger then
    finds their records gone. `game_setup`'s own game delete was removed: the ledger deletes
    the game and the course.

### 3.12 Guests: positive ownership, not creation time (correction, 2026-09-30)

**The flaw.** §3.6 and §3.11 used to treat a guest as the run's if a `POST /api/auth/guest`
succeeded and the account was no older than the ledger. That doesn't prove the run created it.

- `auth_service.create_guest_user` returns the existing guest for an email that already has one,
  and only updates its display name.
- The response is the same token either way.
- So a reused guest was deleted whenever it had been created in the ledger's first second, or
  by anyone else at any time during the run.

**The fix: the run proves a guest is its own before creating it.**

- **Minting.** Every guest the tooling creates uses an email from `Ledger.mint_guest_email()`:
  `<label>.<32 hex digits>@example.com`, 128 random bits. It is written to the ledger *before*
  the request goes out.
  - The suite's helper is `conftest.mint_guest_email()`, used by `create_guest_tokens`, T4, the
    live-session tests and the hygiene tests.
  - The browser checks' `Api.guest` does the same.
  - No account can have had that email before, because nobody else can know it, so the account
    the request returns was created by that request.
- **Recording.** A guest record carries `owned: true` only when its email was minted by the
  run: the running test's ledger or the run's.
  - The suite's tracker checks this on every `/api/auth/guest` response.
  - A guest from any other email is recorded `owned: false`. So is an older record with no
    `owned` field.
- **Teardown.** It deletes an owned guest after the identity check and the secondary
  creation-time check.
  - A guest that isn't owned is never deleted, whatever its creation time. If it still exists,
    it is reported as "ownership uncertain", then dropped from the ledger.
- **Interrupted between creating and recording.** A minted email with no guest record at all is
  looked up by email. If it's found, it is the run's (the email proves it) and is deleted; if
  not, the request never created one.
  - A guest whose record exists but no longer matches isn't looked up this way; it stays kept
    by the identity check.
  - Minted emails are cleared once settled, so a ledger holding only a mint is still cleaned.
  - **A mint whose guest couldn't be looked up isn't settled** (final-review correction 2). If
    the guest listing fails, the mint stays in the ledger, so its file stays too, and the
    problem says so.
    - The next cleanup finds the guest by that email and deletes it.
    - The mint used to be cleared anyway, and the file removed with it. The message said
      "kept", but the only proof the guest was the run's was gone: the guest was orphaned for
      good, reported afterwards only as an `@example.com` leftover.

### 3.13 An answer counts once (final-review correction 4)

**The problem, reproduced on the code before this correction** (`test_live_session.py`, the
first attempt held at its INSERT by a row lock on its question):

1. A player's answer is still being recorded when the phone's connection drops.
2. The phone re-joins. Its `sync_state` says the server has no answer yet, so it sends the
   answer again (§3.2).
3. Both attempts passed the `has_answered` check before either marked the player answered.
4. Result: 2 `session_scores` rows (2.0 points in MySQL's leaderboard) and the distribution
   counted twice. The Redis running score counted once, because the two score updates were
   read-modify-writes of the same record and one overwrote the other.

The old order had further failure windows. The row was flushed, the score and answered mark set
in Redis, the reply sent, and only then the transaction committed. So a failure after the Redis
step left an answer marked answered with no row, and a retry was then acknowledged without
anything stored.

**The fix, as it works now:**

- **One player's attempts at a question run one at a time.** `_answer_turn` is a lock per
  (session, question, user), removed when unused. A retry waits for the attempt before it (logged
  `submit_answer_waits_for_earlier_attempt`), then sees its outcome. Other players are never held
  up.
- **What the server already has is checked first**, before the phase and the lock:
  - Answered in Redis → `answer_received` with `alreadyAnswered`. So a retry of an answer that
    counted is never told it came too late, even after the lock or results.
  - A stored row not yet counted in Redis (an earlier attempt committed and then failed) → the
    row is counted now (`finish_recorded_answer`, logged `answer_counted_on_retry`),
    acknowledged, and the host told. Never a second row.
- **`record_answer` commits the row before anything else**, then counts it in Redis with
  `state.apply_answer`: the running score, the answered mark and the distribution in one
  MULTI/EXEC, skipped if already marked. Only then is `answer_received` sent.
  - A failure before the commit leaves nothing; the retry records the answer.
  - A failure after it leaves an uncounted row. The retry counts it or, if there's no retry
    first, Show Results does (§3.14).
  - "Answer locked in!" always means stored and counted.
  - A failed attempt gets no reply and is logged `submit_answer_failed` (second pass; before,
    the exception just escaped to socket.io).
- **The player's Redis record can't lose a write.** `apply_answer` (the score) and
  `set_player_connected` (the flag a join or disconnect sets) both WATCH it and retry if it
  changed. The old non-atomic `update_player_score`, `mark_answered` and `increment_answer_dist`
  are gone, so nothing bypasses this.

MySQL still has no unique constraint on (session, user, question): adding one needs a migration,
and existing duplicates (guest merge can produce them) would block it. The turns are per-process,
like the gateway's other bookkeeping (§6).

### 3.14 Results say whether each answer counted (final-review correction 1, B-1)

**The problem.** After an answer was refused as late, or dropped on the rejoin because the
question had locked (§3.2, §3.10), the phone's results for the original question types still
read what it had sent:

- "Incorrect · You answered: B — Mercury";
- "Incorrect · You answered: Paris", when Paris was the right answer;
- for a completeness question, "Answer recorded!" — which it also said for no answer at all.

`lastAnswerData` was never cleared, and the page had no other source. T7's types were right
already: their results carry the server's `answered`.

**The fix:**

- **Every type's per-player `question_results` carries `answered`**: whether the server has a
  row for the player's answer (read from MySQL with the scores). The phone's results say
  **"No answer"** when it is false, with no "You answered" line, whatever the phone sent.
  - Zero points prove nothing either way. An accepted wrong answer still reads "Incorrect", and
    an accepted answer to a completeness question worth 0 still reads "Answer recorded!".
  - An `answered` that is missing (an older server) falls back to the old display.
- **What the phone sent is forgotten when it didn't count** (`forgetAnswer`: on
  `answer_rejected`, on the rejoin's drop, on `error`). This applies only to that question's
  answer (`lastAnswerQuestionRef`), and never to one the server accepted: a refusal of a stale
  answer to an earlier question leaves the current question's accepted answer alone.
- **Showing results first closes the question, then waits for answers in flight.**
  `_close_answers` runs in the QUESTION → RESULTS advance, before anything is read:
  1. It stores `question_locked`, so any later answer is refused as late (`locked`).
  2. It commits the handler's transaction, whose snapshot predates the answers being committed
     and would hide them.
  3. It waits for the answers being handled (`_answers_in_flight`, at most 10 s, logged
     `question_results_wait_for_answers`).

  Before this (reproduced), an answer acknowledged while the results were being built was
  missing from them, and its acknowledgement reached the phone after them. With `answered`, that
  would have been "No answer" for an accepted answer. Now the results count exactly the answers
  acknowledged, and each phone hears its acknowledgement before its results.

**The second pass (2026-09-30, before phone sign-off):**

- **If the wait runs out, nothing is published.** The first version logged and published anyway,
  so an answer still being recorded could change the results after they were shown. Now
  `_close_answers` reports how many answers are still being recorded, and if any are:
  - the question stays closed. The lock is a real one, with its time left stored, so an Unlock
    resumes it. The room is told (`question_locked`) if this Show Results closed it.
  - the host gets `results_delayed` (`questionId`, `answersPending`). Not an `error`: the
    projector shows a notice, Show Results stays enabled, and pressing it again publishes the
    results once the answer has finished or failed.
- **Show Results is handled one at a time per session** (`_results_building`). A second Show
  Results meanwhile is ignored (its outcome is on its way), and so is a lock toggle, which would
  reopen the question under it. This also ends the older issue of a quick second advance
  re-running the results.
- **Stored answers are counted before publishing** (`_count_stored_answers`).
  - An attempt can commit its row and then fail before Redis counts it. If the phone doesn't
    reconnect and retry before Show Results, the results used to disagree. Points, `answered`
    and ranks (from MySQL) counted the answer; the player's total and the distribution (from
    Redis) didn't.
  - Show Results now counts each such row in Redis first, in that player's turn at the question
    and conditional on it not being counted yet. So a retry arriving at the same moment can't
    count it twice, and the results agree everywhere.
  - Reproduced by denying the backend's Redis user `HINCRBY`, so the count's MULTI/EXEC is
    refused whole after the commit. Without the new step, the player's total read 1.0 against 2
    expected.

## 4. Order of work

1. Backend: B2, host-presence serialization, the timer-expired rejoin, the unseated-submit log.
2. Both `GameLayout`s: B1; the player's readiness and pending answer; `QuestionPage` wording.
3. Tests:
   - Unit tests for host presence and ordering (fakes, no stack).
   - Integration tests for B2, a real host disconnect and rejoin (timer, manual lock, both
     orderings), and the ledger (success, failure, interruption, unrelated records preserved).
   - The T4 fixture.
4. Browser scripts: the ledger, the B1 and B2 known-issue checks as assertions, no rejoin waits
   after navigation, new reconnect and host-disconnect scenarios.
5. Documentation: the root `CLAUDE.md` and the T4/T8 spec command examples; the context files
   that record B1 and B2; `tests/browser/README.md`.
6. Corrections (2026-09-30):
   - the ledger and cleanup CLI moved to `tests/`, and `--report` made read-only;
   - the integration suite's tracking and its probe tests;
   - the gateway's `host_reconnected`, `hostDisconnected` and `answer_rejected`;
   - both `GameLayout`s;
   - unit, integration and browser regressions;
   - the docs; this record.
7. Guest ownership:
   - minted emails in `tests/ledger.py`, and the tracker's `owned` flag;
   - every guest-creating helper switched to minted emails;
   - unit regressions (`tests/unit/test_ledger.py`) and the probe's reuse cases, without
     backdating;
   - the full reruns; this record.
8. Final-review corrections (§1):
   - integration tests for the double recording and the results race, run first on the
     unchanged code to confirm both;
   - the answer path (`record_answer`'s order, `state.apply_answer`, the gateway's turns,
     `_close_answers`, `answered` for every type) and `_host_left`'s order;
   - the phone's results and `forgetAnswer`; the ledger's unresolved mints;
   - unit, integration and browser regressions, each also run against its fix undone;
   - the full reruns, the rebuild; the context files, `docs/realtime.md`; this record.
9. The second pass (§1):
   - `_show_results`, `_results_building`, `results_delayed` and the projector's notice;
     counting stored answers before publishing; `submit_answer_failed`;
   - the export-boundary diagnosis and the helpers' commit waits (T8, T7);
   - the tests reviewed and merged; each new regression also run against its fix undone;
   - the full reruns, the rebuild; the context files, `docs/realtime.md`; this record.

## 5. Verification

The stack was the compose dev stack; the browser checks ran against the production builds
through nginx `:8080`. CORS stayed at the four localhost origins throughout, with no LAN entry
added.

### 5.00 Final-review corrections and the second pass (§1): verified (2026-09-30, final code)

#### After the integration with `main` (merge `547507d`)

**The browser checks gained the delayed results.** `bc_answers.py` got a sixth question (true or
false, "The Moon orbits the Earth.").
- Ana taps True while the question's row is held in MySQL (`question_row_held`), so her answer's
  INSERT waits. The host then presses Show Results.
- Checked:
  - once the server's 10 s wait runs out, the projector says an answer is still being saved;
  - Show Results stays enabled, and no phone has results;
  - Ana's phone still says "Sending your answer…", and Ben's says answers are locked.
- The row is then released, and Ana's answer is accepted. Show Results, pressed again:
  - Ana's phone reads "Correct! You answered: True +1,000 pts", and Ben's "No answer";
  - the projector shows the results, and the notice is gone.
- 21 checks, up from 15. With the projector's notice hidden, the run fails at the notice
  ("Locator expected to be visible").

**Checks on the merged code:**

| Check | Result |
|---|---|
| Unit | **636 passed** |
| Integration, complete | **150 passed, 8 skipped, 0 failed**, in 5 min 45 s. The skips are the 8 opt-in ones |
| Opt-in: `--fast` / `--nginx-url` / `--image-load` | 9 / 4 / 2 passed. Answer-ack p95 0.43 s (plain question) and 0.38 s (image question), 0 errors; memory peak 800 MiB |
| Browser checks, rebuilt | editor 100/100, Sort It 52/52, Spot It 93/93, answers 21/21 |
| `ruff check` / `ruff format --check`, `tsc --noEmit` in host, player, admin, `npm run build`, `git diff --check` | clean (`tests/integration` keeps its 4 older findings) |
| Test data | Counts identical before and after (1,393 courses, 5 games, 1,853 guests, 1 local user, 4 sessions, 21 images, 13 score rows). No ledger left; game 1166 (course 990) kept its 7 questions; Redis's `default` user at `+@all` |

#### The second pass

**Each new regression was run with its fix undone, and fails:**

| Regression | With the fix undone |
|---|---|
| unit `test_results_wait_for_a_stuck_answer_and_the_host_can_show_them_again` (the answer completes / fails) | Results published despite the pending answer: both fail. Without the one-at-a-time guard: both fail too (the double click and the lock toggle get through) |
| unit `test_a_failed_attempt_counts_once_whoever_completes_it[after-the-commit-then-results]` | No count before publishing: fails |
| integration `test_results_wait_for_answers_still_being_recorded`, part 2 (still recording when the 10 s wait runs out) | Results published despite the pending answer: no `results_delayed` arrives (`TimeoutError`) |
| integration `test_a_failed_recording_counts_once`, part 2 (the count refused after the commit, via `ACL SETUSER default -hincrby`; no reconnect) | No count before publishing: `(True, 1.0, 1.0, 1) == (True, 1, 2, 1)`. `answered`, points and rank counted the stored answer; the total didn't |
| unit `test_only_a_guest_the_run_minted_is_deleted` (the merged ownership test) | Teardown by creation time, as before §3.12: fails |

**The export-boundary failures, diagnosed from their own output.**

- **T8 `test_question_update_measured_as_stored_json`.** The captured message has the shape of
  `set_headroom`'s own assertion (`AssertionError: {json}`), so the refused PUT was the
  padding's. It said "It is 41,942,081 bytes now", "would make 41,943,080".
  - The test's previous line, `put_probe(config={"n": UINT64_MAX})`, grows the probe's config
    from `{}` to `{\n        "n": 18446744073709551615\n      }`: exactly 41 bytes.
  - That update commits after responding. `set_headroom(1)` read the meter before the commit,
    missed the 41 bytes, and sized the padding 41 bytes too long.
  - The export stood at `LIMIT − 1000 + 41` = 41,942,081, and the padding PUT asked for
    `LIMIT − 1 + 41` = 41,943,080: both numbers exactly.
- **"Timed out after 30s waiting for headroom 65536" was T7's `test_export_size_boundary`**
  (`set_headroom(64 * 1024)`), not T8's as the first pass recorded.
  - The loop's `d == c` iteration ends with a successful `reconfirm` (+c bytes, committed after
    responding), and nothing waits for it.
  - The next iteration's meter read can miss those bytes. The padding then lands c bytes long,
    and the exact wait never matches.
- **An audit of every meter read in both suites** found no other un-awaited update before one.
  Question creates, imports and every image write commit before responding, and the other
  updates are followed by exact meter waits.
- **The fix.** `StoredSizeGame.put_probe` (T8) and `Boundary.reconfirm` (T7) now return only
  once the question reads back as the response stored it (`wait_until`), so a later meter read
  includes the change. No boundary assertion changed, and no retry or sleep was added. In the
  final runs: T8 48/48 twice, and T7 in the complete run.

**The branch's tests, made purposeful.**

| Where | What changed |
|---|---|
| unit `test_gateway_answers.py` (10) | The ack-order test folded into the retry-overlap test. The two failed-attempt tests and the new count-before-publishing case made one parametrized test. The two `_close_answers` tests replaced by one handler-level timeout test (waiting within the bound is the integration test's) |
| unit `test_gateway_host_presence.py` (18 → 15) | Disconnect and rejoin in one test; the three "another host socket seated" / "a join says nothing" tests in one |
| unit `test_ledger.py` (14 → 8) | Five ownership tests became one cleanup over four guests; two identity-mismatch tests one; the lost-record test dropped (the failed-lookup test's second cleanup is that case) |
| integration `test_live_session.py` (9) | The failure before the commit and after it (new) in one test; the wait within the bound and past it (new) in one test |
| integration `test_data_hygiene.py` (9 → 8) | The `--report --dry-run` check folded into the read-only report test |
| browser `bc_spot.py` (94 → 93) | `projector_connection_lost`'s weak "nothing sent in the gap" check dropped: `auto_advance_waits_for_the_rejoin` checks the wire, timers included |

**Checks on the final code:**

| Check | Result |
|---|---|
| Unit | **636 passed** (645 at the first pass, less the 9 merged away; more cases covered) |
| Integration, complete | **150 passed, 8 skipped, 0 failed**, in 5 min 40 s. The skips are the 8 opt-in ones |
| Opt-in: `--fast` / `--nginx-url` / `--image-load` | 9 / 4 / 2 passed. Answer-ack p95 0.44 s (plain question) and 0.25 s (image question), 0 errors; memory peak 817 MiB |
| T8 module, twice more | 48/48, 48/48 |
| Browser checks, final build | editor 100/100, Sort It 52/52, Spot It 93/93, answers 15/15; nothing kept |
| `ruff check` / `ruff format --check`, `tsc --noEmit` in host, player, admin, `npm run build`, `git diff --check` | clean (`tests/integration` keeps its 4 older findings) |
| Test data | Counts identical before and after every run (1,393 courses, 5 games, 1,853 guests, 1 local user, 4 sessions, 21 images, 13 score rows). No ledger left; game 1166 kept its 7 questions; Redis's `default` user back to `+@all` |

#### The first pass

**Both backend defects were confirmed before anything was changed.** The two new integration
tests in `test_live_session.py` ran first against the unchanged backend.

- **The double recording.** In `test_a_retry_overlapping_the_first_attempt_counts_once`, both
  attempts reached the INSERT. The result was 2 rows (2.0 points) and the distribution
  `{"true": 2}`. Redis's running score was 1 only because the two score updates overwrote each
  other.
- **The results race.** In `test_results_wait_for_an_answer_still_being_recorded` (now part 1
  of `test_results_wait_for_answers_still_being_recorded`), the results came out without
  waiting: 0 answers counted and an empty distribution. The answer was acknowledged after them.
- `test_a_retry_after_a_failed_attempt_counts_once` (now part 1 of
  `test_a_failed_recording_counts_once`) passed on the old code too: an INSERT that fails did
  fail before any Redis change. The old code's other failure windows (after the Redis step, at
  the late commit) are covered by the unit tests below.

**Every other regression was run with its fix undone, and fails:**

| Regression | With the fix undone |
|---|---|
| `test_gateway_host_presence.py::test_a_player_joining_during_the_disconnect_broadcast_sees_the_host_gone` (both emits held) | The grace task registered after the broadcasts: `assert False is True` on `hostDisconnected`, twice |
| `test_ledger.py::test_a_failed_lookup_keeps_the_mint_for_the_next_cleanup` | Mints cleared regardless: `FileNotFoundError`, the ledger removed with its only proof |
| `bc_answers.py`, the "No answer" checks | The phone ignoring `answered` and never forgetting: 5 of 15 fail, each with a B-1 symptom. "Incorrect · You answered: B — Mercury"; "Answer recorded!" for no answer; "Incorrect · You answered: Paris" for a dropped right answer; "Answer recorded! · You answered: True" for a late one; "Incorrect" for none |
| `bc_answers.py`, the stale refusal of another question | `forgetAnswer` ignoring the question: 1 fails, the accepted answer's "You answered: True" lost |
| `bc_spot.py` `auto_advance_waits_for_the_rejoin` | The projector's gating removed (emit guard, both timers): 4 of 7 fail. The advance was buffered offline, flushed on reconnect 2.5 s before the rejoin was confirmed, then sent again |

The unit tests of the answer path (`tests/unit/test_gateway_answers.py`, 10) have no "before":
they pin failure points (the row's commit, the Redis count) that only the new code has.

| Check | Result |
|---|---|
| Unit (`cd tests/unit && PYTHONPATH=../../backend python -m pytest`) | **645 passed** (632 before; +10 `test_gateway_answers.py`, +2 host presence, +1 ledger). The 26 warnings are the existing T8 image tests' (Pillow) and passlib's |
| `ruff check` / `ruff format --check` on `backend/` and `scripts/`; `ruff check` on `tests/browser`, `tests/ledger.py`, `tests/cleanup.py`, `tests/unit` and the changed integration files; `ruff format --check` on this branch's new test files; `py_compile` of every changed Python file | clean. `tests/integration` keeps its 4 older findings. `test_t7_question_types.py` and `engine/socket_client.py` weren't ruff-formatted before this branch either, and were left so (only the changed lines follow the format) |
| `tsc --noEmit` in host, player, admin; `npm run build` | clean; rebuilt (and rebuilt again after each fix-undone run, from the restored sources) |
| `git diff --check` (plus each untracked file) | clean |
| Integration, complete (`cd tests/integration && python -m pytest -rs`), final code | **151 passed, 8 skipped, 0 failed**, in 5 min 17 s (147 before; +4 in `test_live_session.py`). The skips are the 8 opt-in ones |
| Opt-in: `test_game_flow.py --fast` | 9 passed |
| Opt-in: `test_t8_nginx_limits.py --nginx-url http://localhost:8080` | 4 passed |
| Opt-in: `test_t8_image_load.py --image-load -s` (150 players), run twice | 2 passed both times. Answer-ack p95 0.41 s / 0.37 s (plain question) and 0.49 s / 0.40 s (image question), 0 errors: within this branch's earlier range (0.32–0.57 s), so the answer path's new turn, row lookup and watched transactions cost nothing measurable at 150 players. Burst uploads all 201; backend memory peaked at 803 MiB |
| Browser checks (`tests/browser/`), final build | editor 100/100, Sort It 52/52, Spot It 94/94 (+7: `auto_advance_waits_for_the_rejoin`), answers 15/15 (new); nothing kept |

**Two earlier complete runs failed, for reasons found and settled:**

- **`test_t7_question_types.py::test_self_contained_results` failed** in both, deterministically.
  - It asserted that non-T7 results have exactly the pre-T7 fields. `answered` now goes to every
    type on purpose (§3.14), so that was the contract, not a bug.
  - The test now counts `answered` among every type's fields, leaving the T7 fields' union
    unchanged. It also asserts that nobody answered its closing true/false question
    (`answered` false for all three).
  - T7 module: 27 passed.
- **Export-boundary tests failed intermittently.** One timed out in the first complete run
  ("headroom 65536"); `test_question_update_measured_as_stored_json` failed in one of three
  module-only runs (a 409, measured 41 bytes over the headroom its helper had just waited for).
  - This pass recorded both as T8's and left them *noted, not scheduled*.
  - The second pass diagnosed both from that output: the "headroom 65536" timeout is in fact
    T7's. Both are fixed; see the second pass, below.

**Test data left behind: none.**

- Database counts before this correction's runs and after all of them were identical: 1,393
  courses, 5 games, 1,853 guests, 1 local user, 4 sessions, 21 images, 13 score rows.
- The ledger directory was empty.
- Game 1166 kept its 7 questions.
- `tests/cleanup.py --report` lists the same leftovers as before: 118 "T7 checks" courses and
  1,852 `@example.com` guests.

### 5.0 Guest-ownership correction (§3.12): verified (2026-09-30, final code)

The first attempt at this verification was interrupted when the host disk filled; it is
recorded at the end of this subsection. After storage was recovered and Docker restarted, the
checks below ran on the final code: the stack's services healthy, 91 GiB free, CORS at the four
localhost origins, and the three apps rebuilt (`npm run build`) and type-checked (`tsc
--noEmit`, clean).

| Check | Result |
|---|---|
| Unit (`cd tests/unit && PYTHONPATH=../../backend python -m pytest`) | **632 passed** (+13 in `tests/unit/test_ledger.py`) |
| Integration, complete (`cd tests/integration && python -m pytest -rs`) | **147 passed, 8 skipped, 0 failed**, in 5 min 6 s. The skips are the 8 opt-in ones (`--fast` ×2, `--image-load` ×2, `--nginx-url` ×4); every T7/T8 lock test ran, and the three T8 export-size tests that failed in the interrupted run passed. No cleanup warnings |
| Opt-in: `test_game_flow.py --fast` (timer expiry) | 9 passed |
| Opt-in: `test_t8_nginx_limits.py --nginx-url http://localhost:8080` | 4 passed |
| Opt-in: `test_t8_image_load.py --image-load -s` (150 players) | 2 passed. Answer-ack p95 0.32 s (plain question) and 0.36 s (image question), 0 errors. The 6 × 30 MP upload burst was all 201; backend memory peaked at 729 MiB |
| Browser checks (`tests/browser/`) | editor 100/100, Sort It 52/52, Spot It 87/87; nothing kept |
| `ruff check` / `ruff format --check` (`backend/`, `scripts/`, the changed test files); `py_compile` of every changed Python file | clean |
| `git diff --check` (plus each untracked file) | clean |

**Test data left behind: none.**

- Database counts before the complete suite, after it, and after the opt-in runs and all three
  browser scripts were identical: 1,393 courses, 5 games, 1,853 guests, 1 local user,
  4 sessions, 21 images.
- The ledger directory was empty after every run.
- The phone-checklist game (1166, 7 questions, 2 images) and its course (990) are unchanged.
- `tests/cleanup.py --report` lists the same pre-existing leftovers as before: 118 "T7 checks"
  courses and 1,852 `@example.com` guests.

**The interrupted run's leftovers, cleaned through its ledger.**

- `integration-2f901a8ba353-test.json` (its process gone) held T8 game 2294 and course 2110.
  Both still matched their records and were created in the second the ledger started.
- `tests/cleanup.py --dry-run` planned exactly those two deletions, `tests/cleanup.py` made them
  (exit 0), and both rows are gone.

**One environmental failure, fixed before the clean run.**

- **The first complete rerun:** 146 passed, 1 failed:
  `test_an_old_host_socket_closing_after_the_rejoin_changes_nothing` ("The backend never logged
  `host_socket_closed_host_still_seated`").
- **The cause was the disk episode, not the code.** That test waits for the server's own log
  line, read with `docker compose logs`.
  - The backend container's log stopped being readable at 07:32:30, when the disk filled.
  - Docker had restarted the container, not recreated it, so it kept that log. Docker then
    returned *no* backend line newer than 07:32:30, with no error, although the backend was
    serving the whole suite (0 lines readable for the last 10 minutes).
  - The host and container clocks agreed to within a second.
- **The fix.**
  - The readable log (1,085,730 lines, 120 MB) was saved to the session's scratchpad, as
    `backend-container-logs-until-0732.log`.
  - The backend alone was recreated with the repo's documented command,
    `docker compose up -d --no-deps --force-recreate backend`. No data volume was touched; the
    backend holds no persistent state.
  - Its log was readable again. nginx still reached it (`/api/health` and the socket.io
    handshake through `:8080`: 200), and CORS was unchanged.
  - Then `test_live_session.py` passed 5/5, and the complete suite ran clean (above).
- **The same log explains the interrupted run's three T8 failures.** Its last readable lines
  are the backend losing its MySQL connection at 07:32:30 ("Can't connect to MySQL server"):
  MySQL went down with the full disk, mid-test.

**The ownership regressions fail without the fix.**

- **Unit (`test_ledger.py`).** It runs against an in-memory admin API with exact timestamps.
  - It covers:
    - a reused guest created in the ledger's first second;
    - a guest another actor created during the run and the run reused;
    - a run-owned guest (deleted);
    - a guest recorded without ownership;
    - an uncertain guest that is already gone (not reported);
    - a lost record found by its minted email;
    - a guest whose record exists but no longer matches (not recovered by email);
    - a mint never used;
    - a dry run;
    - the tracker's `owned` flag.
  - With teardown treating every guest as owned (creation time only, as before), 4 fail,
    including both reuse cases.
- **End to end (`test_data_hygiene.py`, via the probe).** The probe reuses two guests: one
  created before its run, and one another actor creates *during* it (over plain `urllib`, which
  the run can't see). No backdating is involved.
  - Both are kept and reported as "ownership uncertain", while every guest the probe created is
    deleted.
  - Under the old rule, the actor's guest is deleted and the test fails.

**The interrupted first attempt (kept as recorded).**

- **Where it stopped.** Unit tests had passed (632) and so had the affected integration modules
  (72 passed, 2 skipped). The complete run then stopped at 96%, when the host disk filled
  (`OSError: [Errno 28] No space left on device`, 126 MiB free of 460 GiB).
- **What was left.** It had reached 143 passed and 3 failed T8 export-size tests, whose error
  text was lost; Docker then stopped responding.
- **What the disk held.** This session's files were small: the repo 733 MB, the scratchpad
  2 MB, Docker's disk image 17 GB actual. Nothing was deleted to recover space; storage was
  recovered outside this task.

### 5.1 After the corrections (2026-09-30, before §3.12)

| Check | Result |
|---|---|
| Unit (`cd tests/unit && PYTHONPATH=../../backend python -m pytest`) | **619 passed** (baseline 603; +16 in `test_gateway_host_presence.py`) |
| Integration, complete (`cd tests/integration && python -m pytest -rs`) | **147 passed, 8 skipped, 0 failed**, in 5 min 26 s. The skips are the 8 opt-in ones (`--fast` ×2, `--image-load` ×2, `--nginx-url` ×4); every T7/T8 lock test ran. Baseline 132 passed, 1 failed, 8 skipped. This branch adds T4's corrected case, `test_live_session.py` 5, and `test_data_hygiene.py` 9 |
| Opt-in: `test_game_flow.py --fast` (timer expiry) | 9 passed |
| Opt-in: `test_t8_nginx_limits.py --nginx-url http://localhost:8080` | 4 passed (its module fixture's records survived each test and went at the session's end) |
| Opt-in: `test_t8_image_load.py --image-load -s` (150 players) | 2 passed; answer-ack p95 0.40 s (baseline question) / 0.57 s (image question), 0 errors; its 150 guests deleted afterwards |
| Browser checks (`tests/browser/`) | editor 100/100, Sort It 52/52, Spot It 87/87. Ledger directory empty after every run |
| `ruff check` / `ruff format --check` on `backend/` and `scripts/`; `ruff check` on `tests/browser`, `tests/ledger.py`, `tests/cleanup.py` and the new test files | clean. `tests/integration` keeps its 4 older findings (`conftest.py` ×2, `scenarios/edge_cases.py`, `test_t7_question_types.py`) |
| `tsc --noEmit` in host, player, admin; `npm run build` | clean; built |
| `git diff --check` (plus each untracked file) | clean |

**Test data left behind: none.** Database counts before the complete suite, after it, and after
the three opt-in runs:

| Courses | Games | Guests | Local users | Sessions |
|---|---|---|---|---|
| 1,393 | 5 | 1,853 | 1 | 4 |

All three snapshots were identical. The 2026-09-29 complete run plus opt-ins had added 234
courses and 252 guests. The phone-checklist game (1166, 7 questions) and its course (990) are
unchanged.

**Each correction is covered by a regression that fails without it.**

- **Item 1.** `test_report_is_read_only` has an abandoned, eligible ledger next to unrelated
  records.
  - `--report` must leave the ledger file byte-for-byte and every record in place. A plain
    cleanup of the same ledger then deletes it.
  - With `--report` restored to its old order (teardown, then report), the test fails.
  - `test_report_and_dry_run_are_exclusive` checked the two options can't be combined (merged
    into `test_report_is_read_only` in the second pass).
- **Item 3, the host's return.**
  - Unit: `host_reconnected` after a return that unlocks, and after one where a manual lock
    holds; nothing on a first or second host's join.
  - Integration: the phones receive it in both cases, and a phone joining while the host is
    away gets `hostDisconnected: true` (`false` after the return).
  - Browser: the banner clears in both cases, in Spot It and in Sort It.
- **Item 3, the projector's controls.** `projector_connection_lost` (browser) holds the
  projector offline and checks:
  - the banner says the controls are paused, and Show Results and Lock are disabled;
  - a forced click does nothing (*superseded:* a browser never delivers a click to a disabled
    button, so this proved nothing about the gating; replaced by the on-the-wire check in
    §5.00);
  - the phones see "Host disconnected" meanwhile;
  - after the rejoin, the controls work, the phone hears the host is back, nothing was sent
    late, and the game carries on.
- **Item 3, late answers.**
  - Unit: locked, results-shown and moved-on answers are `answer_rejected`, while a lost room
    and a malformed `question_id` stay `error`.
  - Integration: the same over real sockets, plus a refused join after game over, which is
    still `error`.
  - Browser (`late_answers_are_not_errors`): the phone is made late by withholding the
    server's `question_locked`, then its `question_results`. Each time it keeps its screen and
    selection, says the answer didn't count, never shows it accepted, and plays the next
    question normally.
- **Item 4.** `test_data_hygiene.py` runs `hygiene_probe.py` as a pytest session of its own.
  - It creates records the existing ways: `game_setup`, `create_room`, `create_guest_tokens`,
    plain httpx calls, a local user, and a module-scoped fixture.
  - One test passes and one fails. Afterwards every record is gone and no ledger is left.
  - The module fixture's game outlives each test.
  - A guest whose token the probe only obtained, using an existing (older) guest's email, is
    kept and reported.
  - A look-alike unrelated world is untouched.
  - A killed probe session leaves a ledger that `tests/cleanup.py` then clears.

**Phone checklist.**

- The script was run against the dev database with no arguments, `--cleanup` and `--replace`.
  All three refused, and game 1166 and course 990 are unchanged.
- Its create → `tests/cleanup.py` → `--replace` → `--cleanup` cycle ran on sandbox names with a
  scratch ledger directory. Everything was removed, and the kept ledger survived the cleanup
  CLI.
- The `--game-id` adoption of a game created before the ledger:
  - without `--game-id`, the script refuses;
  - with it, only that game is deleted;
  - the course it didn't create is kept.

### 5.2 Before the corrections (2026-09-29; kept as recorded)

| Check | Result |
|---|---|
| Unit | 614 passed |
| Integration, complete | 143 passed, 8 skipped, 0 failed |
| Opt-in `--fast` / `--nginx-url` / `--image-load` | 9 / 4 / 2 passed; answer-ack p95 0.34 s / 0.53 s, 0 errors |
| Browser checks | editor 100/100, Sort It 51/51, Spot It 67/67 |

- **B1 and B2** were reproduced first (§2).
- **The four unit-level guards** were removed one at a time, and each removal failed its own
  test: the host-presence lock, the "another host seated" check, the timer-expired rejoin, and
  the abandon task's own-entry pop.
- **With the B2 fix removed,** all four `test_live_session.py` tests of the time failed.

**Leftovers reported, not deleted** (`tests/cleanup.py --report` and read-only counts; their
ownership can't be established):

- 118 courses in the semester "T7 checks" (ids 726–998), from browser runs before the ledger;
- 1,852 `@example.com` guest accounts (of 1,853 guests in all);
- about 1,270 courses in the integration suite's semesters: "T8 Test", "T4 Test", "Test", "T8".

They predate the ledgers, including the integration runs of 2026-09-29, before item 4 was
corrected.

## 6. Remaining limitations

*Noted, not scheduled* unless said otherwise. The corrections resolved three limitations recorded
here on 2026-09-29:

- the phones' banner staying up after the host's return (now §3.8);
- projector clicks during a reconnect (now §3.9);
- a late or re-sent answer hitting the fatal error screen (now §3.10).

- **A late-noticed host socket.** If the server hasn't yet noticed an old host socket died (its
  ping timeout, about 45 s), a real close of the host's new socket is handled only when the old
  one times out.
- **A pending answer lives in memory**, so a page reload loses it. The phone shows the question
  again.
- **Per-process state.** `_host_presence_locks`, like the other gateway dicts, is per-process,
  and it keeps one small lock per session for the process's lifetime.
  - `_answer_turns`, `_answers_in_flight` and `_results_building` (§3.13, §3.14) are
    per-process too. They empty themselves as answers and results finish.
  - With more than one worker, a retry reaching another process isn't serialized with the first
    attempt, and could still be recorded twice. MySQL has no unique constraint to catch it.
- **An attempt that fails inside the server gets no reply** (a database or Redis error while
  recording; logged `submit_answer_failed`). The phone keeps "Sending your answer…" until its
  next reconnect, which sends the answer again, or until the results. It counts once either way
  (§3.13, §3.14).
  - *Corrected in the second pass.* This record used to say that, with no reconnect, the
    results then say "No answer". That holds only for a failure before the row's commit, which
    leaves nothing. A failure after it leaves a stored answer: the results count it, and the
    phone's results show it answered with its points.
- **Showing results waits at most 10 s** for answers still being recorded. For an answer stuck
  longer (a database lock held that long), the results are held back, not published: the host
  sees a notice and presses Show Results again once it finishes. Auto-advance doesn't retry by
  itself.
- **`results_delayed` is new to the protocol**, like `answer_rejected`. `scripts/` tools and
  other clients that don't handle it simply see no results until they ask again.
- **One live-session test changes the Redis `default` user's ACL for a moment**
  (`test_a_failed_recording_counts_once` denies `HINCRBY`, to make a count fail after its
  commit). It is restored in a `finally`. If a run is killed at that point, restore it with
  `docker compose exec redis redis-cli ACL SETUSER default +hincrby` (`ACL GETUSER default`
  should list `+@all`).
- **Correct versus incorrect is still derived on the phone** (`yourPoints > 0`) for the original
  four types, so partial credit shows "Correct!" (the player's `pages/CLAUDE.md`). Only *whether*
  an answer counted comes from the server now.
- **`answer_rejected` is new to the protocol.** `scripts/simulate_players.py` doesn't listen for
  it. Its simulated late taps time out waiting for `answer_received`, as they did with the old
  `error`.
- **Test-data tracking covers the test suites' httpx traffic.** Records made another way (the
  `scripts/` tools such as `seed_demo.py` and `simulate_players.py`, or by hand) aren't tracked,
  by design. Leftovers from before the ledgers (§5) are reported, never deleted.
- **One live-session test reads the backend's container log.**
  `test_an_old_host_socket_closing_after_the_rejoin_changes_nothing` waits for the server's own
  log line, through `docker compose logs` (or `BUZZER_TEST_BACKEND_LOGS`).
  - If the container's log becomes unreadable, the test fails without any code being at fault.
    That happened once, after the host disk filled (§5.0).
  - Recreating the backend (`docker compose up -d --no-deps --force-recreate backend`) restores
    the log.
- **The physical-phone checks (§8) are user-reported.** All nine passed; the device and browser
  weren't recorded. P7's race is hard to hit by hand, so the browser checks remain its real
  coverage.

## 7. After T7 merges

**Done (2026-09-30).** How the integration went:

1. **Verified.** `origin/main` ends with `aad922e` ("Merge branch
   'feature/dexter-t7-question-types' into 'main'"), which merged T7 as one squash commit,
   `b0c18eb`.
   - T7's branch gained no commits after `5016570`.
   - The squash commit's tree equals T7's final branch outside `ai_log/`.
   - `main`'s only other new work since this branch's base was the teammate's T4 test coverage
     (`b86ea87`), already in this branch through T7's own history.
2. **The uncommitted work was preserved first, twice.**
   - A copy of all 50 modified and untracked paths was made outside the repository, with a
     SHA-256 manifest and the full tracked diff.
   - A `git stash --include-untracked` was taken. It saved everything, but stopped before
     clearing the tracked changes: it couldn't remove `backend/logs/`, the backend container's
     busy bind-mount point, which is empty and not ours.
   - A second stash of the tracked changes alone (identical to the first stash's) then left the
     tree clean. No reset or clean was used.
3. **The merge was clean.** A preview (`git merge-tree`) found no conflicts, and a tree equal to
   this branch's `HEAD`, since `main` differed only by lacking this branch's session logs.
   `git merge origin/main` made the merge commit `547507d`. It changed no file; both histories
   are kept.
4. **Restored and verified.**
   - The full stash was applied, and all 50 paths matched their pre-merge hashes.
   - The path list was identical, and the tracked diff byte-identical to the saved one. Both
     stashes were then dropped.
5. **The diff against `main` holds only this task.**
   - Committed: only this branch's 10 `ai_log/` records differ from `origin/main`.
   - The working tree adds exactly the bug-fix work (40 tracked files, 10 untracked). Its diff
     against `origin/main` is byte-identical to its diff against `HEAD`, so no T7 or T8 change
     is reintroduced.
   - The commit history outside `main` also lists T7's and T8's original commits (both were
     squash-merged). Their combined content equals the squash exactly, so they add nothing to
     the diff.
6. **Checks re-run on the merged code:** §5.00 and §8.

The original instructions, kept for reference:

T7 must merge first. Until it has, this branch's merge request compares against
`feature/dexter-t7-question-types`. After it merges:

1. **Fetch and merge.** Fetch, and merge the current `main` into this branch with a merge
   commit. Keep both histories: don't rebase, squash or force-push.
2. **Resolve each conflict by reviewing it.** Never take one side wholesale (`-X ours`,
   `-X theirs`, or "resolve everything to `main`").
   - Accepted upstream work stays: T7 as it was merged, including any changes made in its
     review, and anything else that has landed on `main`.
   - This branch's fixes stay too.
   - Where both touched the same lines (for example `gateway.py`, the two `GameLayout`s, the
     integration `conftest.py`, the context files), combine them.
   - After resolving, re-read each resolved hunk against both parents.
3. **If T7 was squash-merged,** `main` holds T7 as one new commit, while this branch still
   holds T7's original commits.
   - The merge can then conflict wherever T7's final state and this branch's changes meet.
     Resolve those in the same way.
   - T7's own lines should come out as merged on `main`, with this branch's fixes applied on
     top, not T7's pre-review lines.
4. **Check that the diff against `main` holds only this task's changes.**
   `git diff origin/main...HEAD --stat` should list this branch's files, plus the automatic
   `ai_log/` records.
   - Check each file's diff (`git diff origin/main...HEAD -- <file>`).
   - No T7 or other upstream change may appear, reverted or duplicated.
   - If any does, the resolution was wrong: fix it before going on.
5. **Retarget the merge request to `main`**, and keep its note that it required T7 first.
6. **Re-run the checks in §5 on the merged branch**, then review the diff against `main` again.

## 8. Targeted physical-phone checks: passed (user-reported, 2026-09-30)

**Result: P1–P9 all passed**, as reported by the user. They ran on the merged build (`547507d`
with this task's then-uncommitted changes, the same code as the commit), with the phone reaching
the laptop over the LAN. No per-check notes or device details were given.

These cover only what this change affects. Use the existing "T7 phone checklist" game (game 1166:
Q1 Sort It, 120 s; Q2 Spot It, 90 s).

**LAN setup** (T7 spec §4.7):

1. `npm run build` (already current).
2. Add `http://<LAN address>:8080` to `CORS_ORIGINS` in `.env`, and recreate only the backend:
   `docker compose up -d --no-deps --force-recreate backend`.
3. Host at `http://<LAN address>:8080/host/`, Play → the checklist course and game → Create Game
   Room. Join from the phone by QR.
4. **Afterwards, restore CORS:** remove the entry, leaving `http://localhost:8080`,
   `http://localhost:8000`, `http://localhost:5173` and `http://localhost:5174`. Recreate the
   backend again, and check that a localhost page's socket connects. Don't commit `.env`.

**The temporary CORS change: added for the checks, then removed (2026-09-30).**
- **Added:** `,http://192.168.1.91:8080` at the end of `.env` line 23, and only the backend
  recreated. The origin was accepted through the LAN address, and an unlisted one still refused.
- **Removed after the checks:** only that entry. `.env` then matched, byte for byte, the copy
  kept from before the change. Only the backend was recreated again
  (`docker compose up -d --no-deps --force-recreate backend`). The backend's `CORS_ORIGINS` then
  listed the four localhost origins alone.
- **Checked after removal:**
  - `/api/health` answers 200 on `localhost:8080` and `localhost:8000`;
  - a Socket.IO handshake is accepted from `localhost:8080`, `:5173` and `:5174`, and refused
    from `192.168.1.91:8080` (400);
  - `test_host_disconnect_locks_pauses_and_the_rejoin_resumes` passed over real sockets.
- `.env` stays git-ignored and untracked.

| # | Check | Expected |
|---|---|---|
| P1 | **Host drops out** (T7 checklist item 6's blocked part). Mid-question, place some cards (Q1) or select a cell (Q2), then close the host's browser tab | Within a few seconds the phone shows "Host disconnected" and its Submit / Lock in is disabled; the placements or selection stay |
| P2 | **Host comes back.** Reopen the host (Play → Rejoin) | The "Host disconnected" banner goes; the controls re-enable with nothing lost. Submit / Lock in → "Answer locked in!", scored at results |
| P3 | **A manual lock survives.** Host Lock → close the host tab → reopen | The banner clears, but the phone stays locked ("Answers locked"). Host Unlock re-enables it |
| P4 | **The host's own connection blips.** Turn the laptop's Wi-Fi off for a few seconds, then back on | The projector shows "Connection lost — reconnecting… Controls are paused until the game is back." and its Show Results / Next / Lock are disabled. After Wi-Fi returns, they re-enable only once the projector is back in the room, and nothing advances on its own |
| P5 | **The phone loses its connection.** Airplane mode (or Wi-Fi off) for about 10 s during a question | "Connection lost — reconnecting…" at the top; the question and selection stay on screen (no error screen) |
| P6 | **An answer given offline.** Still offline, tap Submit / Lock in, then reconnect | "Sending your answer…" while offline (never "Answer locked in!"). Within a few seconds of reconnecting: "Answer locked in!", and results show it scored |
| P7 | **A late tap.** Tap Lock in at the moment the host locks or the timer runs out | The phone keeps the game screen and shows "Too late — answers had already closed…" or "Answers locked"; never "Answer locked in!"; results say "No answer". The race is hard to hit by hand, and the browser checks cover it deterministically. It passes if hit and handled like that; it isn't a failure if the tap simply lands in time or after the lock is shown |
| P8 | **Answering straight away.** For a few questions, answer the moment each appears | Every answer goes to "Answer locked in!" at once, and results show it (none reads "No answer") |
| P9 | **Background and return** (T7 item 3, recheck). Background the browser for 30 s or more mid-question, then return | The phone reconnects: the banner clears, the selection is kept, and answering works |

Game 1166 and course 990 are kept after these checks too, for now. The checks' own game room
(one session and its score rows) is kept with them. Removing the game
(`python tests/browser/phone_checklist_setup.py --cleanup --game-id 1166`, which reports and keeps
course 990) waits until it is asked for.
