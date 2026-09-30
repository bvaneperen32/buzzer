# How Real-Time Works in Buzzer

Lecture covered REST: the client sends a request, the server sends one response, the connection is done. A live quiz game can't work that way — when the host advances to the next question, the server must push that fact to every player *immediately*, without any player asking for it. Buzzer does this with **Socket.io** (WebSockets): each browser holds one persistent, two-way connection to the backend for the duration of a game.

This document traces **one player answering one question**, end to end, so you can see every moving part. Read it before you modify anything in the game loop — new question types, scoring changes, and host-screen features all flow through this path.

## The cast

| Piece | Where it lives | Role |
|---|---|---|
| Event name constants | `backend/app/websocket/events.py` | The complete vocabulary — every event the client and server can send. Start here. |
| Gateway | `backend/app/websocket/gateway.py` | The server-side handlers: one `@sio.on(...)` function per client→server event. |
| JWT middleware | `backend/app/websocket/middleware.py` | Authenticates the socket connection using the same JWT you get from `/api/auth/login`. |
| State service | `backend/app/services/state_service.py` | Live game state in **Redis** (current question, who has answered, scores) — fast, per-room, expires with the room. |
| Game service | `backend/app/services/game_service.py` | Scoring logic (`record_answer` and the per-type scoring branches) and **MySQL** persistence — every answer is written to the database immediately. |
| Player client | `frontend/player/src/pages/game/GameLayout.tsx` | Opens the socket, listens for server events, sends answers. |

Note the division of labor: **Redis holds the ephemeral live state; MySQL holds the permanent record.** The gateway orchestrates both on every answer.

## Trace: one answer, end to end

Assume the host has started a question, so every player has already received a `new_question` event (see payload shape in `_question_payload()` in the gateway — it deliberately includes `config` but **never** `answer_data`, so the correct answer is never in the browser).

**1. Player taps an option.** The player app emits one event over its existing socket:

```js
socket.emit("submit_answer", {
  question_id: 42,
  answer_data: { selectedIndex: 2 },   // shape depends on question type
  answer_time_ms: 4180,
});
```

`answer_data` is a type-specific dict: `{selectedIndex}` for multiple choice, `{selectedValue}` for true/false, `{text}` for fill-in-the-blank, `{selectedIndices}` for multi-select, and (T7) `{placements: [box | null, …]}` for Sort It — one entry per card in the author's order, at least one card placed — and `{col, row}` for Spot It. If you add a question type, you are defining a new `answer_data` shape.

The two T7 shapes are checked exactly in the gateway before anything is recorded: integers, not booleans, and nothing extra. A malformed one gets an `error` and no score. The stored `answer_data` is the validated answer, unchanged.

**2. The gateway validates before it scores** (`on_submit_answer` in `gateway.py`). In order, it checks: the sender is an authenticated PLAYER in a room; a question is actually open (`question_phase == "QUESTION"` in Redis) and not locked; the submitted `question_id` matches the live question; and the player hasn't already answered (idempotent — a duplicate gets an `answer_received` reply with `alreadyAnswered: true` and is *not* re-scored). Any failure emits an `error` event **to that player only**, and nothing is recorded.

**3. Scoring and persistence.** The gateway calls `game_service.record_answer(...)`, which dispatches on `question.type` to compute correctness and points, writes the answer to **MySQL**, and updates the player's running score in **Redis**.

**4. The server fans out three different notifications** — this is the part REST can't do:

- To **the answering player** only: `answer_received` — `{questionId, isCorrect, pointsAwarded, totalScore}`.
- To **the host screen** only: `answer_status` — `{userId, answeredCount, totalPlayers}` (this is how the host's live "12/30 answered" counter works).
- If that was the **last** unanswered player: `answer_phase_ended` to the host, and the server cancels the question countdown timer.

Meanwhile every player who *didn't* answer receives nothing — their screens change only when the host advances and the server broadcasts `question_results` and then the next `new_question` to the whole room.

**T7 additions to the payloads.** A Sort It `new_question` carries `cardOrder`: a permutation of the card indices, seeded by the question id. The projector and every phone show that order, and it is never the author's order, which could give away the grouping. Answers still use the original indices. For Sort It and Spot It questions only, each player's `question_results` adds six fields: `questionType`, `gradingType`, `pointsValue`, `answered`, `yourAnswer` (their validated answer, or null) and `yourIsCorrect`. So a phone renders its result from that payload alone, even if it reloaded between answering and results. Other types' results are unchanged. Reveals: Sort It `{type: "sort_into_boxes", correctBoxes}` and Spot It `{type: "find_in_image", acceptedCells}` (row-major), or `{type: "completeness"}`.

## Images: links, not bytes

Questions can show images (T8), but **no image bytes travel over the socket**. Browsers load images from signed, expiring links — `/api/images/{id}/v{version}/{full|small}?t={token}` — which need no bearer token (an `<img>` element can't send one). The server creates them only where access has already been checked. Each screen gets only the links for what it shows:

- **Phones** get them inside `new_question`: an `images` map, `{"<image id>": {url, width, height, description}}`, holding **small links for option images only** (`{}` when there are none). It is built by `_question_payload` from the question's `config`, one query scoped to the question's own game. Prompt images never reach phones. `config` itself passes through unchanged, so a phone treats an option id missing from `images` as having no image. A reconnecting player who hasn't answered gets the late-join `new_question` with the same map. `question_results`, `game_over` and the player's `sync_state` carry no new fields.
- **The projector** gets its links over REST, not the socket: `GET /api/game/sessions/{session_id}/images` returns full and small links for every image of the session's game. Access needs both the session-host check and the host's current access to the game itself. The host app fetches it once `GET /api/game/rooms/{code}` gives it the session id, again after a socket reconnect, when a question uses an id the map lacks, when an image fails to load, and before the links expire.

**Spot It (T7) never sends its image to phones.** `config.image_id` is only an id, and `images` stays `{}` for Spot It. The phone draws the grid from `config.grid`, `config.width` and `config.height` (the image's proportions). The projector draws the full image from its session link map.

A link stays the same URL for its whole hour, so browsers reuse cached bytes across the question, results and game-over screens. No image can change version while a session is live: replace and delete are refused, and room creation is serialized with them under the game row lock. So a link a session received is never stale for that session.

## The full event vocabulary

Client → server: `join_room`, `rejoin_room`, `submit_answer`, and host-only `host_advance`, `host_lock_question`.
Server → client: room-wide broadcasts (`new_question`, `question_results`, `game_over`, `player_joined`, `player_left`, `question_locked`/`question_unlocked`, `host_disconnected`, `game_abandoned`), player-targeted replies (`answer_received`, `sync_state`, `error`), and host-only status (`answer_status`, `answer_phase_ended`).

All names are defined once in `events.py` and imported everywhere — if you add an event, add it there.

## Seeing it live

Two ways to watch this trace happen without hand-driving 30 browser tabs:

```bash
# Scripted end-to-end game that asserts on every event payload:
python scripts/smoke_test_websocket.py

# Fill a real room with simulated players (create a room in the Host app first):
python scripts/simulate_players.py --room <CODE>
```

Both scripts speak the socket protocol directly (no browser), so reading them is also the fastest way to learn the client side of the contract. The integration tests in `tests/integration/engine/socket_client.py` do the same thing under pytest.

## Rules of thumb when you extend the game loop

- **Server is the referee.** Never trust the client: validate the answer shape in the gateway (see the `multi_select` bounds-check for the pattern), score in `game_service`, and never send `answer_data` to the browser.
- **Redis for live state, MySQL for the record.** If it must survive the room ending, it goes in MySQL. If it's "what is happening right now," it goes through `state_service`.
- **Pick the right audience for every emit:** the room, the host room (`_host_room(...)`), or one `sid`. Broadcasting a player's score to the whole room is a bug; so is telling only one player the question changed.
