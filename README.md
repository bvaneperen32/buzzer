## Your Team

- Brody Van Eperen (bvaneperen@wisc.edu)
- Dexter Lehman (dllehman2@wisc.edu)

Reach your partner directly by emailing the address above — it's their @wisc.edu NetID email.
---

# Buzzer Game

A Jackbox-inspired real-time party quiz platform built for classroom use. A **Host** displays game state on a large screen while **Players** join and answer questions from their mobile browsers.

- Up to 250 players per room, 50 concurrent rooms
- Multiple question types: multiple choice, true/false, fill in the blank, multi-select
- Two grading modes: accuracy-based and participation-based (completeness)
- Local accounts and guest players (no account required)
- Live answer distribution, per-player scoring

## Prerequisites

| Tool | Minimum version | Purpose |
|---|---|---|
| Docker Engine / Docker Desktop | 25 / 4.x | Running the full stack |
| Node.js | 20 | Frontend development servers |
| Python | 3.12 | Integration tests (optional if using Docker only) |

---

## First-time setup

### 1. Copy and configure environment

```bash
cp .env.template .env
```

Open `.env` and set:

| Variable | What to do |
|---|---|
| `MYSQL_ROOT_PASSWORD` | Choose a strong password |
| `MYSQL_PASSWORD` | Choose a strong password (used by the app) |
| `DATABASE_URL` | Update `change_me_app` to match `MYSQL_PASSWORD` |
| `ADMIN_USERNAME` | Username for the first admin account |
| `ADMIN_PASSWORD` | Password for the first admin account |
| `JWT_PRIVATE_KEY` | See step 2 |
| `JWT_PUBLIC_KEY` | See step 2 |

### 2. Create the Python virtual environment

Run the included setup script once after cloning. It creates `.venv`, installs all dependencies, and patches the activate script so your `.env` variables load automatically whenever the environment is activated:

```bash
bash scripts/setup_venv.sh
```

Then activate the environment (do this at the start of every terminal session):

```bash
source .venv/bin/activate        # macOS / Linux
```

Your shell prompt will show `(.venv)` when the environment is active, and variables like `ADMIN_USERNAME` and `ADMIN_PASSWORD` will already be set from your `.env`. All `python`, `pip`, `pytest`, and `alembic` commands below assume the environment is active.

> **Tip:** Most IDEs (VS Code, PyCharm) can detect `.venv` automatically and activate it for you in integrated terminals. In VS Code, open the Command Palette → *Python: Select Interpreter* and choose the `.venv` entry.

> **Windows:** The setup script targets bash (macOS/Linux). On Windows, create the venv manually (`python3.12 -m venv .venv`), install dependencies (`pip install -r backend/requirements.txt -r tests/integration/requirements.txt`), and load `.env` variables in your shell before running any scripts.

### 3. Generate JWT signing keys

The backend signs tokens with RS256. Run the included helper to generate a key pair and print the values ready to paste into `.env`:

```bash
# With the virtual environment active (cryptography is already installed):
python backend/scripts/generate_keys.py

# Or via Docker (no local Python required):
docker run --rm python:3.12-slim \
  sh -c "pip install cryptography -q && python" \
  < backend/scripts/generate_keys.py
```

Paste the two `JWT_*` lines into `.env`.

### 4. Install frontend dependencies and build

A root-level `package.json` manages a `concurrently` dev tool and provides convenience scripts:

```bash
npm install            # installs concurrently (root dev tool)
npm run install:all    # installs dependencies in all three frontend apps
```

> **Note:** Running `npm run build` is only needed if you want the pre-built apps to be served by Nginx at `http://localhost:8080`. If you plan to use the Vite dev servers (the typical workflow), you can skip the build step entirely.

> **Note:** If the build fails with `EACCES: permission denied` on the `dist` directory, Docker likely created it as root before the build ran. Fix with: `sudo chown -R $(id -u):$(id -g) frontend/*/dist`

### 5. Start the Docker stack

```bash
docker compose up
```

On first run, Docker builds the backend image and pulls MySQL, Redis, and Nginx. MySQL takes ~30 seconds to become healthy before the backend starts.

Use `--build` any time you change `requirements.txt` or the `Dockerfile`:

```bash
docker compose up --build
```

> **Image support (T8) added Pillow** to `backend/requirements.txt`, so an existing checkout must rebuild the backend image once (`docker compose up --build`) and then apply migration `005` (next step). The same change pins `redis<8`: redis-py 8 caps each connection pool at 100 by default, which a full class answering at once exceeds.

> **First run note:** On initial startup, MySQL takes 30–45 seconds to initialize the database user, and the backend will **wait — logging `admin_bootstrap_waiting_for_schema` every few seconds — until you run the migrations in the next step**, because its startup needs the database schema, which doesn't exist yet. Both behaviors are normal.

### 6. Run database migrations

Apply the Alembic schema migrations (this runs in its own one-off container, so it works while the backend is still waiting):

```bash
docker compose run --rm backend alembic upgrade head
```

This creates all tables. Within a few seconds the waiting backend picks up the new schema, finishes starting, and bootstraps the admin account from `ADMIN_USERNAME`/`ADMIN_PASSWORD` — no manual step needed.

---

## Accessing the application

| Interface | URL | Notes |
|---|---|---|
| Player app | http://localhost:8080/player/ | Mobile-optimised |
| Host app | http://localhost:8080/host/ | Large-screen display |
| Admin app | http://localhost:8080/admin/ | Course/game/user management |
| API (direct) | http://localhost:8000 | Bypasses Nginx |
| Interactive API docs | http://localhost:8000/api/docs | Swagger UI (dev only) |
| Health check | http://localhost:8000/api/health | Service status |

Log in to the Host or Admin app with the `ADMIN_USERNAME` / `ADMIN_PASSWORD` you set in `.env`.

---

## Development workflow

### Starting a session

Run these commands at the beginning of every development session.

**1. Activate the Python virtual environment** in any terminal where you will run Python tools (tests, migrations, scripts):

```bash
source .venv/bin/activate
```

If you ran `scripts/setup_venv.sh` during first-time setup, this also loads all your `.env` variables automatically (e.g. `ADMIN_PASSWORD`). This is not needed for the Docker stack itself — the backend runs inside its own container. It is needed whenever you run `pytest`, `alembic`, or any Python script directly in your terminal.

**2. Start the Docker stack** (backend, MySQL, Redis, Nginx):

```bash
docker compose up
```

The backend mounts `./backend` and runs with `--reload`, so any Python change takes effect immediately — no restart needed. Logs stream to the terminal.

**3. Start all frontend dev servers** in a single terminal from the repo root:

```bash
npm run dev
```

This starts all three Vite dev servers concurrently with color-coded, labeled output. **Check the terminal output for the exact URLs** — Vite picks the next available port if the default is already in use, so the numbers may differ from the typical defaults shown below:

```
[host]   VITE ready  →  http://localhost:5173
[player] VITE ready  →  http://localhost:5174
[admin]  VITE ready  →  http://localhost:5175
```

Press `Ctrl+C` once to stop all three.

To start a single app instead:

```bash
npm run dev:host    # typically http://localhost:5173
npm run dev:player  # typically http://localhost:5174
npm run dev:admin   # typically http://localhost:5175
```

In each case, check the terminal output for the actual URL Vite is listening on.

The Vite dev servers watch your source files and hot-reload the browser instantly on every save — no manual rebuild step. They proxy `/api` traffic to the Docker backend, so the full stack works at these ports during development.

> **Backend only?** If you are only changing Python code, you can skip the frontend dev servers and use the pre-built apps at `http://localhost:8080/host/`, `http://localhost:8080/player/`, and `http://localhost:8080/admin/` instead.

### During a session

- **Backend changes** — saved automatically; uvicorn reloads the process within a second.
- **Frontend changes** — saved automatically; the browser hot-reloads via the Vite dev server.
- **Database model changes** — generate and apply a migration:

```bash
docker compose exec backend alembic revision --autogenerate -m "describe change"
docker compose exec backend alembic upgrade head
```

### Ending a session

When you are done, stop all running processes:

- Press `Ctrl+C` in each terminal running a Vite dev server.
- Stop the Docker stack:

```bash
docker compose down
```

If you made frontend changes during the session and want `localhost:8080` to reflect them (e.g. for integration testing), run a final build before stopping:

```bash
npm run build
```

### Other migration commands

```bash
# Apply all pending migrations
docker compose exec backend alembic upgrade head

# Revert the last migration
docker compose exec backend alembic downgrade -1
```

---

## Testing

### Integration test suite

Tests run against the live Docker stack (start it first). Dependencies are already installed if you followed the virtual environment setup above. Activate the environment, then run:

```bash
python -m pytest tests/integration/ -q
```

The suite deletes what its tests create: every course, game, room, guest and local user a test creates is recorded by exact id and deleted when the test ends, whether it passed or failed. Records it didn't create are never touched. Because `/api/auth/guest` hands back an existing guest for a known email, a test creates guests only with emails from `conftest.mint_guest_email()` (random, claimed before the request); a guest made with any other email is reported and kept, never deleted. A run killed outright leaves its records listed in `tests/.ledgers/`; `python tests/cleanup.py` deletes them (details in [tests/browser/README.md](tests/browser/README.md), "Test data").

Include timer-expiry edge cases (sets `time_limit_seconds=2` so tests finish quickly):

```bash
python -m pytest tests/integration/ -q --fast
```

The image tests (`test_t8_images.py`) include **lock tests** that hold a game's row lock from their own MySQL connection, on the published port `3306`, as the backend's account. They read `MYSQL_PASSWORD` from `.env`; override with `BUZZER_TEST_DB_HOST` / `_PORT` / `_NAME` / `_USER` / `_PASSWORD`. If they skip, image verification is incomplete, not passed. The test-data tests (`test_data_hygiene.py`) use the same connection, and so does the suite's own cleanup of the courses its tests create. The live-session tests (`test_live_session.py`) also read the room from Redis and the backend's log, through `docker compose` from the repo root (override with `BUZZER_TEST_REDIS_CLI` / `BUZZER_TEST_BACKEND_LOGS`). Two opt-in modules need extra flags:

```bash
# Per-route request body limits, through nginx (after npm run build, with the stack up)
python -m pytest tests/integration/test_t8_nginx_limits.py -q --nginx-url http://localhost:8080

# 150 players fetching option images, plus a burst of large uploads (prints its measurements)
python -m pytest tests/integration/test_t8_image_load.py -q --image-load -s
```

### Unit tests

No stack needed. They import the backend as `app`, so put `backend/` on the path:

```bash
cd tests/unit && PYTHONPATH=../../backend python -m pytest -q
```

### Browser checks (Sort It and Spot It)

Playwright scripts that play both question types on a projector page and phone-sized pages, and
exercise the question editor, through nginx at `:8080` against the production builds (run
`npm run build` first). Setup, configuration (base URL, admin credentials, browser) and how to
read the results are in [tests/browser/README.md](tests/browser/README.md):

```bash
pip install -r tests/browser/requirements.txt
python tests/browser/bc_editor.py && python tests/browser/bc_sort.py && python tests/browser/bc_spot.py
```

### WebSocket smoke test

A quick end-to-end script that creates seed data, plays through a full game, and verifies every event payload (dependencies are included in `backend/requirements.txt`):

```bash
python scripts/smoke_test_websocket.py
```

Override credentials or the target URL with environment variables:

```bash
ADMIN_USERNAME=admin ADMIN_PASSWORD=changeme123 BASE_URL=http://localhost:8000 \
  python scripts/smoke_test_websocket.py
```

### Theme check

No stack needed. Checks the shared colour theme (`frontend/shared/theme/`): WCAG contrast for every allowed colour pair in light and dark, that every token name the Tailwind preset uses exists, and that no old palette class, focus-suppressing class or hex colour is left in the three apps. Exits 1 on any failure:

```bash
python scripts/check_theme.py
```

### Simulate players

`scripts/simulate_players.py` populates a live room with realistic fake players so you can test the full game loop without real students. It creates temporary local accounts via the Admin API, connects them over WebSocket, and has them answer automatically. All dependencies are already installed by `scripts/setup_venv.sh`.

**Workflow:**

1. Start the Docker stack and open the Host app.
2. Create a room and note the room code shown in the lobby.
3. In a separate terminal (with the virtual environment active), run:

```bash
python scripts/simulate_players.py --room ABCDE1
```

4. Press **Start** / **Next** on the host screen to advance through questions. Players answer automatically. Late players join after their randomised delay and print a message when they arrive.
5. When the game ends, temporary accounts are deleted automatically.

**Player profiles** and default mix:

| Profile | Default % | Behaviour |
|---------|-----------|-----------|
| `fast` | 25 % | Answers in 0.5–3 s; rarely skips |
| `normal` | 35 % | Answers in 3–8 s; occasionally skips |
| `slow` | 20 % | Answers in 8–18 s; more likely to skip |
| `absent` | 10 % | Never submits an answer |
| `late` | 10 % | Waits 25–70 s before joining, then answers at a normal pace |

**Common options:**

```bash
# 40 players with a custom profile mix (remainder becomes "normal")
python scripts/simulate_players.py --room ABCDE1 --players 40 \
    --fast-pct 20 --slow-pct 20 --absent-pct 10 --late-pct 10

# Pass the exported game JSON so fast/normal players lean toward correct answers
python scripts/simulate_players.py --room ABCDE1 --players 30 \
    --game-json sample_games/my_quiz.json

# Custom fill-in-the-blank word pool (repeat a word to weight it higher)
python scripts/simulate_players.py --room ABCDE1 --players 25 \
    --fitb-words "spring,spring,summer,summer,summer,autumn,winter"

# Leave temporary accounts in the DB after the run (useful for inspecting scores)
python scripts/simulate_players.py --room ABCDE1 --keep
```

Admin credentials default to the `ADMIN_USERNAME` / `ADMIN_PASSWORD` environment variables loaded automatically by the virtual environment. Override them with `--admin-user` and `--admin-pass`, or point at a different backend with `--base-url http://localhost:8000`.

---

## Common operations

### Load the demo quiz

After running migrations, seed the database with a demo game that covers every question type and grading mode:

```bash
python scripts/seed_demo.py
```

This creates a **Demo Course** and a **Buzzer Demo** game with 9 questions (1 point each):

| # | Type | Grading | Question |
|---|------|---------|---------|
| 1 | Multiple choice | Accuracy | Which planet has the most moons? |
| 2 | True/False | Accuracy | A group of flamingos is called a 'flamboyance'. |
| 3 | Multiple choice | Completeness | What is your go-to comfort food? |
| 4 | Multiple choice | Accuracy | What is the largest land animal on Earth? |
| 5 | True/False | Accuracy | The Great Wall of China is visible from space. |
| 6 | True/False | Completeness | Do you prefer coffee over tea? |
| 7 | Fill in the blank | Accuracy (exact) | Chemical symbol for Gold? |
| 8 | Fill in the blank | Accuracy (fuzzy) | Who wrote 'Romeo and Juliet'? |
| 9 | Fill in the blank | Completeness | What country would you visit? |

### Sample games with images

Two games in `sample_games/` are generated, images included, by `scripts/generate_t6_games.py`: **Read the Graph: Data Literacy Essentials** (`read_the_graph_data_literacy.json`, classroom) and **Game Night Gauntlet** (`game_night_gauntlet.json`, party). Both are version 2 bundles that use every question type, Sort It and Spot It included, and both grading modes. Import them like any other game file (see [Quiz import and export](#quiz-import-and-export)).

Change these games in the script, not in the JSON, then regenerate from the repo root with the venv active:

```bash
python scripts/generate_t6_games.py           # rewrite both files
python scripts/generate_t6_games.py --check   # exit 1 if either file is out of date
```

The script draws every image with Pillow from fixed data and seeds, and checks each answer that depends on a picture (the correlations, mean vs median, the Spot It cells) against the data it drew. Identical PNG bytes need the same Pillow build, so run it from the repo's venv.

### Admin interface

The Admin app (`http://localhost:8080/admin/`) provides a full management UI:

| Section | What you can do |
|---|---|
| **Courses** | Create courses; navigate to roster management |
| **Roster** | Upload a Canvas gradebook CSV or a basic CSV roster; activate or deactivate individual entries |
| **Users** | Create local accounts; click any user to grant or revoke course access (HOST/PLAYER) and game access |
| **Guests** | View guest accounts; merge a guest's scores into a real netid account; delete stale guests |
| **Games** | Create games; import a game from a JSON file; delete games |
| **Questions** | Add, edit, delete, and reorder questions within a game; export the game as a JSON file |
| **Sessions** | View completed game sessions and download score exports |

Log in with the `ADMIN_USERNAME` / `ADMIN_PASSWORD` from your `.env`.

### Quiz import and export

Games (metadata + all questions including answer data) can be exported and re-imported as JSON files.

**Export** — from the Admin UI, open a game's question editor and click **Export JSON**. Or via the API:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/admin/games/{game_id}/export" \
  -o my_quiz.json
```

**Import** — from the Admin UI, click **Import JSON** on the Games page and pick a `.json` file. This always creates a *new* game — it never overwrites an existing one. Or via the API:

```bash
curl -X POST "http://localhost:8000/api/admin/games/import" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@my_quiz.json"
```

The file format is:

```json
{
  "format": "buzzer/game",
  "version": 1,
  "game": { "title": "...", "description": "...", "max_players": 150 },
  "questions": [
    {
      "type": "multiple_choice",
      "grading_type": "ACCURACY",
      "prompt": "...",
      "time_limit_seconds": 30,
      "points_value": 1000,
      "config": { "options": ["A", "B", "C", "D"] },
      "answer_data": { "answer_points": [1000, 0, 0, 0] }
    }
  ]
}
```

Question order is determined by array position; `order_index` is assigned automatically on import.

**Games with images export as version 2**, which adds the whole image library as base64. Questions refer to images by the bundle's own keys, and import re-derives everything else from the bytes:

```json
{
  "format": "buzzer/game",
  "version": 2,
  "game": { "title": "...", "description": "...", "max_players": 150 },
  "images": [
    { "key": "img-1", "filename": "tower.jpg", "description": "Stone tower on a hill", "data": "<base64>" }
  ],
  "questions": [
    {
      "type": "multiple_choice", "grading_type": "ACCURACY", "prompt": "Which city is this?",
      "config": { "prompt_image_id": "img-1", "options": ["Paris", "Rome"], "option_images": [null, null] },
      "answer_data": { "answer_points": [1, 0] }, "time_limit_seconds": 30, "points_value": 1
    }
  ]
}
```

A game with no images still exports as version 1, byte for byte as before, and every version 1 file still imports. Each image entry has exactly those four fields. An import is checked in full (count, entries, references, every image through the pipeline, the library budget) before anything is created, and its export size is measured on the game as stored before anything is committed, so a refused import leaves nothing behind. Exporting, importing and exporting again gives identical bytes.

**Sort It and Spot It questions** (see below) export like this. A Sort It question needs no image (version 1 unless the game has images); a Spot It question always refers to its image by key, so its game exports as version 2:

```json
{"type": "sort_into_boxes", "grading_type": "ACCURACY", "prompt": "Sort by class",
 "config": {"boxes": ["Mammal", "Reptile", "Amphibian"],
            "cards": ["Whale", "Gecko", "Frog", "Bat", "Newt", "Tortoise"]},
 "answer_data": {"correct_boxes": [0, 1, 2, 0, 2, 1]},
 "time_limit_seconds": 45, "points_value": 1000}
```

```json
{"type": "find_in_image", "grading_type": "ACCURACY", "prompt": "Find the lighthouse",
 "config": {"image_id": "img-1", "grid": {"cols": 6, "rows": 4}},
 "answer_data": {"accepted_cells": [{"col": 4, "row": 1}, {"col": 4, "row": 2}], "confirmed": true},
 "time_limit_seconds": 30, "points_value": 1000}
```

- A Spot It bundle never carries the image's `width`/`height`: import rebuilds them from the imported image.
- `"confirmed": true` is written only when the accepted cells were confirmed against the exported image and grid. An imported question marked confirmed is stamped against its new image. Its grid must fit that image, or the import is refused. An unconfirmed question imports needing review, with its grid and cells unchanged. A bundle can't carry `confirmed_for`, the stamp the server stores internally.

### Sort It and Spot It

Two question types added in T7 (design: `docs/plans/t7-question-types.md`):

- **Sort It** (`sort_into_boxes`): players place 2–4-box, 4–10-card boards on their phones by dragging or tapping a card and then a box, and can leave a card out. ACCURACY scores each card: right +1, wrong −1/(boxes − 1), left out 0. The question is floored at 0, then scaled to the points value. The projector shows the boxes and the cards in a shared shuffled order, then per-card results.
- **Spot It** (`find_in_image`): the projector shows an image under a labelled grid. Players pick the matching cell on their phones, which show only the grid (never the image). ACCURACY is all or nothing on an accepted cell, and results show a heatmap.

A Spot It question's accepted cells must be **confirmed** against its image and grid (the checkbox in the editor). The grid must also fit the image: every cell at least 28 px on a 360 px-wide phone, so a 16:9 image allows up to 10×6. Replacing an image, or changing the grid, makes the question **need review**: rooms can't be created until it's fixed and reconfirmed. While a session of a game is live, its Sort It and Spot It questions are frozen except for their prompt, time limit and (Sort It) prompt image.

### Images in questions

Every game has its own **image library**, stored in MySQL. Upload, replace, re-describe and delete images from the **Images** panel at the top of a game's question editor, or pick and upload them straight from a question's image pickers. Any question can show an image below its prompt (on the projector only). Multiple-choice and multi-select options can be images, with or without a caption. Every image needs a description, which is its alt text.

Uploads are checked and normalized. PNG, JPEG, WebP and GIF are accepted; anything else, including SVG, is refused. Photos are rotated upright, stripped of metadata, and scaled to at most 1600 px. Phones get a 400 px WebP copy. The limits:

| Limit | Value |
|---|---|
| One uploaded or imported image | 10 MiB, 40 megapixels |
| A stored image | 1600 px on its longest side, 4 MiB |
| Images per game | 50 |
| Library budget (stored images) | 24 MiB per game |
| Export size, and import file size | 40 MiB |
| Images processed at once | 2, with a 20 s wait for a slot before a 503 |

A write that would push a game's export past 40 MiB is refused, so every game's export re-imports. Images can't be replaced or deleted while a session of the game is live, or deleted while a question uses them. Browsers load images through short-lived signed links (2–3 hours).

**nginx enforces the request body limits per route** (`nginx/nginx.dev.conf`): 11 MiB for image upload and replace, 41 MiB for the two import routes, and nginx's 1 MiB default for everything else. FastAPI reads a whole body before any handler runs, so nginx is the only real size gate. A new route that accepts large bodies needs its own `location` there. Requests sent straight to port 8000 bypass these limits.

### Export session scores

After a game session completes, the host (or an admin) can download a CSV of all player scores:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/game/sessions/{session_id}/export" \
  -o scores.csv
```

Columns: `Player, Q1, Q2, …, Qn, Total`.

### Import a course roster

Roster uploads expect either a minimal CSV roster or a **Canvas gradebook export CSV**. When importing, the user can match columns to necessary fields for the players.

See `sample_rosters/*sample_roster_from_gradebook*.csv` for a working examples. Upload via the Admin UI (Courses → Roster → Upload CSV), or via the API:

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"changeme123"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -X POST "http://localhost:8000/api/admin/courses/{course_id}/roster" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@sample_rosters/sample_roster_from_gradebook.csv"
```

### View structured logs

```bash
docker compose logs -f backend       # stream backend logs
```

### Reset the database (destructive)

```bash
docker compose down -v               # removes the MySQL volume
docker compose up --build
docker compose run --rm backend alembic upgrade head   # the waiting backend finishes startup on its own
```

---

## Project structure

```
buzzer/
├── backend/
│   ├── app/
│   │   ├── routers/        HTTP endpoints  (auth, game, admin, health)
│   │   ├── services/       Business logic  (auth, game, state, roster, export)
│   │   ├── websocket/      Socket.io gateway and JWT middleware
│   │   ├── models/         SQLAlchemy ORM models
│   │   ├── schemas/        Pydantic request/response schemas
│   │   └── migrations/     Alembic schema migrations
│   ├── scripts/
│   │   └── generate_keys.py   RS256 key-pair generator
│   └── requirements.txt
├── frontend/
│   ├── host/               React app — host/display screen  (typically port 5173 in dev)
│   ├── player/             React app — player mobile browser (typically port 5174 in dev)
│   ├── admin/              React app — admin management UI   (typically port 5175 in dev)
│   └── shared/theme/       Colour tokens, Tailwind preset and light/dark logic used by all three
├── nginx/
│   └── nginx.dev.conf      Reverse-proxy config
├── mysql-init/             SQL scripts run by MySQL on first startup
├── scripts/
│   ├── smoke_test_websocket.py   End-to-end WebSocket smoke test
│   ├── simulate_players.py       Multi-profile async player simulator
│   ├── seed_demo.py              Seeds a demo course and game
│   ├── check_theme.py            Contrast and leftover-colour check for the frontend theme
│   └── generate_t6_games.py      Generates two sample games and their images
├── tests/
│   ├── integration/        Pytest integration test suite
│   ├── unit/               Pytest unit tests (no stack needed)
│   └── browser/            Playwright browser checks for Sort It and Spot It
├── sample_games/           Example game JSON files for import
├── sample_rosters/         Example Canvas gradebook CSV files for roster import
├── ai_log/                 Claude Code session logs (auto-generated, committed by hook)
├── docker-compose.yml
├── .env.template           Copy to .env and fill in before first run
└── .claude/                Claude Code hook configuration
```

---

## Architecture overview

```
Browser (HTTP)
      │
      ▼
   Nginx :8080
   ├── /api/*      → FastAPI backend :8000
   ├── /socket.io/ → FastAPI backend :8000  (WebSocket upgrade)
   ├── /host/      → Host React app   (static)
   ├── /player/    → Player React app (static)
   └── /admin/     → Admin React app  (static)
                          │
                     FastAPI :8000
                     ├── SQLAlchemy → MySQL :3306
                     └── redis-py   → Redis :6379
```

Real-time communication uses **Socket.io** (python-socketio on the server, socket.io-client in the browser). Game state is stored in Redis for low-latency reads; every player answer is immediately persisted to MySQL.
