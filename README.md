# 🐾 Pounce

Pounce watches Minecraft names and claims one for you the second it becomes available.

Minecraft names stop being taken exactly 37 days after their owner last changed them. Pounce keeps a quiet watchlist of rare names, notices when one loses its owner, waits for that exact moment, and pounces.

It is built to run unattended on something small. Mine lives on a Raspberry Pi next to the homeserver.

## How it works

- a watcher sweeps the whole watchlist through Mojang's batch lookup endpoint, so the full fleet (every three letter name included) is re-checked roughly every 45 minutes from a single IP, politely under their request budget
- the moment a name stops resolving it has dropped, but a name freed by a rename-away sits in Mojang's 37 day hold and is not claimable yet. pounce books the exact day that hold ends (from the old owner's name history when it has the uuid) and fires a precision snipe at it then
- with a known exact drop time, it syncs its clock against Mojang's `Date` headers and sends a short timed burst instead
- after any win, claiming pauses itself until you run `resume`, so one lucky catch never turns into a rename loop

Everything it learns lives in `data/`: a small SQLite database, your tokens, and an event log. Nothing leaves your machine except Mojang API calls.

## Run it

You need Python 3.11+.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python -m pounce login      # opens your browser once, then never again
.venv/bin/python -m pounce add3l      # watch every three letter name
.venv/bin/python -m pounce add4l      # watch four letter words from your dictionary
.venv/bin/python -m pounce run        # start watching
```

`login` walks through the same Microsoft device flow the official launcher uses. The token refreshes itself forever; you log in exactly once.

Names are watched passively by default. To arm a name for auto claiming:

```sh
.venv/bin/python -m pounce prioritize moon
```

It can reach you when something happens. Optional `ntfy` block in `data/config.json`:

```json
"ntfy": { "topic": "your-secret-topic" }
```

It pushes when a name drops, when one is won or lost, and once when it comes online. Pick a topic nobody can guess, that is the only thing standing between a random stranger and your drip feed of dropped name alerts. Self-hosted ntfy works too via `"url": "https://ntfy.example.com"`.

## Docker

```sh
docker compose up -d --build
```

The first login needs a browser, so do it once before going headless:

```sh
docker compose run --rm pounce python -m pounce login
```

Your tokens and database survive in `./data`.

## Honesty

Claiming has a real cost: changing your name releases the old one to the public after 30 days. Pounce will not hide that from you, so claiming stays off until you explicitly arm names, and stops again after the first win.

## License

[MIT](LICENSE)
