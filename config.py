import os

# ---- required ----
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)

# ---- optional tuning ----
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "5"))           # ESPN poll interval
FM_POLL_SECONDS = int(os.environ.get("FM_POLL_SECONDS", "2"))     # FotMob fast-lane interval
UEFA_POLL_SECONDS = int(os.environ.get("UEFA_POLL_SECONDS", "3")) # UEFA official lane interval
SCOREBOARD_SECONDS = int(os.environ.get("SCOREBOARD_SECONDS", "60"))  # fixture discovery interval
# Railway: use persistent volume at /data when mounted, else local file.
# Set DB_PATH explicitly to override (e.g. DB_PATH=/data/data.db with a volume at /data).
def _default_db():
    if os.environ.get("DB_PATH"):
        return os.environ["DB_PATH"]
    if os.path.isdir("/data"):
        return "/data/data.db"
    return "data.db"
DB_PATH = _default_db()
LEAGUE_SLUG = os.environ.get("LEAGUE_SLUG", "uefa.champions")
SHOW_STATS_BLOCK = os.environ.get("STATS_BLOCK", "1") == "1"      # stats table at FT
SHOW_LINEUPS = os.environ.get("LINEUPS", "1") == "1"              # auto lineups pre-match
FANTASY = os.environ.get("FANTASY", "1") == "1"                   # fantasy points blocks
POINTS_LIST_MAX = int(os.environ.get("POINTS_LIST_MAX", "14"))    # max lines in points block
KICKOFF_MSG = os.environ.get("KICKOFF_MSG", "1") == "1"

# ---- UCL Fantasy approximation ----
# Reproduces official sample match: Odegaard 7 (2app+4goal+1SoT),
# Dembele 16 (2app+8goals+4assists+2SoT), Fabian 7, Nuno Mendes 5.
# Tune here after comparing with the official app.
POINTS = {
    "app": 2,          # played 60+ minutes
    "app_less60": 1,   # played under 60 minutes
    "goal": 4,        # all positions
    "assist": 2,
    "sot": 1,
    # Official UCL Fantasy: every 3 saves = 1 pt (GK). NOT per-save.
    "saves_per3": 1,
    "conceded_per2": -1,  # GK/DEF, per 2 goals conceded
    "yellow": -1,
    "red": -3,
    "own_goal": -2,
    "clean_sheet_gk": 2,
    "clean_sheet_def": 2,
    "clean_sheet_mid": 1,
}

# Display-name overrides for event lines. If present, used VERBATIM;
# otherwise the default rule applies (uppercase last name).
NAME_OVERRIDES = {
    "Ferran Torres": "Ferran",
    "Fabián Ruiz": "Fabián",
    "Suleiman Camara": "Suleiman",
    "Ousmane Dembélé": "O.Dembélé",
    "Nuno Mendes": "N.Mendes",
    "Dro Fernández": "Dro",
    "Kylian Mbappé": "Mbappé",
    "Rafael Leão": "Leão",
}
