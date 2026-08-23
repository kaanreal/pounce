"""build the list of "real word" 4 letter names from a system dictionary,
with a small curated fallback so it works before installing wamerican."""
import re
from pathlib import Path

from .common import DATA_DIR, WORDS_PATH, log

DICT_CANDIDATES = [
    "/usr/share/dict/words",
    "/usr/share/dict/american-english",
    "/usr/local/share/dict/words",
]

# fallback seed, only used if no system dictionary exists.
# anything that is not exactly 4 letters gets filtered out anyway.
CURATED = """
wolf moon star fire rain snow leaf wave echo nova apex luna gold iron coal jade
ruby opal onyx rose iris lily fern pine sage mint haze mist glow dawn dusk noon
moss vine root seed bark twig tide foam surf dune reef hawk dove wren lynx puma
bear lion deer hare mole toad newt carp byte neon aqua vibe aura myth lore epic
tale rune omen luck jazz funk soul punk rock halo fury calm bold wild pure true
free king lord mage monk blade helm mail bolt dart bomb trap lock keys gate door
wall roof beam nail wood clay sand dust soot mine vein lime kelp fang claw hoof
pelt mane tail snout fin scale gill nest roost den cave peak ridge mesa canyon
river lake pond marsh swamp delta basin shore beach coast reef isle cape bay gulf
peak hill vale glen dell heath moor tundra taiga steppe plain field grove park
yard path road lane trail route track rail bridge tunnel gate fence hedge wall
"""


def _read_dict(path):
    try:
        text = Path(path).read_text(errors="ignore")
        return set(re.findall(r"[A-Za-z]+", text))
    except OSError:
        return None


def find_dictionary(explicit=None):
    if explicit:
        words = _read_dict(explicit)
        if words is None:
            raise FileNotFoundError(f"dictionary not found at {explicit}")
        return explicit, words
    for p in DICT_CANDIDATES:
        words = _read_dict(p)
        if words:
            return p, words
    return None, None


def extract_four_letter(words):
    return sorted({w.lower() for w in words if len(w) == 4})


def build_wordlist(dict_path=None):
    """returns (path_written, count, used_system_dict)"""
    dict_used, raw = find_dictionary(dict_path)
    if raw:
        four = extract_four_letter(raw)
    else:
        log().warning("no system dictionary found, using built-in curated list")
        log().warning("on a raspberry pi run: sudo apt install wamerican")
        four = [w for w in CURATED.split() if len(w) == 4]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WORDS_PATH.write_text("\n".join(four) + "\n")
    return WORDS_PATH, len(four), bool(raw)


def load_good_words():
    if WORDS_PATH.exists():
        return {w.strip().lower() for w in WORDS_PATH.read_text().split() if w.strip()}
    # fall back to curated without writing anything
    return {w for w in CURATED.split() if len(w) == 4}
