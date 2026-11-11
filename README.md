# Buzzer

A real-time party quiz platform for the classroom, in the spirit of Jackbox. A **Host** shows the game on a projector or large screen while **Players** join from their phones and answer live. An **Admin** app manages courses, rosters, users and the question library.

- Six question types: multiple choice, true/false, fill in the blank, multi-select, **Sort It** (drag cards into boxes) and **Spot It** (find the right cell on a grid laid over an image)
- Two grading modes: accuracy-based and participation-based
- Images in questions and answer options, from a per-game image library. Uploads are validated, rotated upright, stripped of metadata and resized, with a smaller copy for phones
- Live answer distribution on the host screen, per-player scoring and results heatmaps
- Local accounts and guest players (no account needed to join)
- Canvas gradebook roster import, game import and export as JSON, and per-session score export as CSV
- Shared light and dark color theme across all three apps, with an automated WCAG contrast check
- Built for a full classroom: up to 50 concurrent rooms, with a player simulator and load tests

**Tech stack:** Python, FastAPI, Socket.IO, SQLAlchemy, Alembic, MySQL, Redis, React, TypeScript, Vite, Tailwind CSS, Nginx, Docker Compose

## Getting started

### Prerequisites

| Tool | Minimum version |
|---|---|
| Docker Engine / Docker Desktop | 25 / 4.x |
| Node.js | 20 |
| Python (optional, for the helper scripts) | 3.12 |

### 1. Configure the environment

```bash
git clone https://github.com/bvaneperen32/buzzer.git
cd buzzer
cp .env.template .env
```

Open `.env` and set:

| Variable | What to do |
|---|---|
| `MYSQL_ROOT_PASSWORD` | Choose a strong password |
| `MYSQL_PASSWORD` | Choose a strong password (used by the app) |
| `DATABASE_URL` | Change `change_me_app` to match `MYSQL_PASSWORD` |
| `ADMIN_USERNAME` | Username for the first admin account |
| `ADMIN_PASSWORD` | Password for the first admin account |
| `JWT_PRIVATE_KEY`, `JWT_PUBLIC_KEY` | See step 2 |

### 2. Generate JWT signing keys

The backend signs tokens with RS256. Generate a key pair with Docker (no local Python needed):

```bash
docker run --rm python:3.12-slim \
  sh -c "pip install cryptography -q && python" \
  < backend/scripts/generate_keys.py
```

Paste the two `JWT_*` lines it prints into `.env`.

### 3. Install and build the frontend

```bash
npm install            # root dev tooling
npm run install:all    # dependencies for the host, player and admin apps
npm run build          # builds the apps that Nginx serves on :8080
```

> If the build fails with `EACCES: permission denied` on a `dist` directory, Docker created it as root. Fix it with `sudo chown -R $(id -u):$(id -g) frontend/*/dist`.

### 4. Start the stack

```bash
docker compose up --build
```

This starts the backend, MySQL, Redis and Nginx. On the first run MySQL takes 30–45 seconds to initialize, and the backend waits (logging `admin_bootstrap_waiting_for_schema`) until the database schema exists. That's expected: run the next step.

### 5. Run database migrations

In a second terminal:

```bash
docker compose run --rm backend alembic upgrade head
```

Within a few seconds the backend finishes starting and creates the admin account from `ADMIN_USERNAME` / `ADMIN_PASSWORD`.

### 6. Open the apps

| Interface | URL |
|---|---|
| Player app (phones) | http://localhost:8080/player/ |
| Host app (projector) | http://localhost:8080/host/ |
| Admin app | http://localhost:8080/admin/ |
| API docs | http://localhost:8000/api/docs |

Log in to the Host or Admin app with your admin credentials. To try it out quickly, import one of the games in `sample_games/` from **Admin → Games → Import JSON**, start a room from the Host app, and join from a phone or a second browser tab with the room code.

### Optional: load the demo quiz

The helper scripts run on the host with a Python virtual environment (`scripts/setup_venv.sh` creates it and loads `.env` on activation):

```bash
bash scripts/setup_venv.sh
source .venv/bin/activate
python scripts/seed_demo.py
```

This creates a **Demo Course** with a game that covers the original question types and both grading modes. To fill a room with simulated players, run `python scripts/simulate_players.py --room <ROOM_CODE>`.

### Optional: frontend development with hot reload

```bash
npm run dev
```

This starts Vite dev servers for all three apps (typically ports 5173, 5174 and 5175; check the terminal output for the exact URLs). They proxy `/api` to the Docker backend.
