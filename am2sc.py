"""am2sc — sincronizza la libreria Apple Music scaricando i brani da SoundCloud con scdl.

Va eseguito con il Python del venv di scdl (lo fa il launcher `am2sc`),
perché usa la libreria `soundcloud` (soundcloud-v2) installata insieme a scdl.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from soundcloud import SoundCloud

APP_DIR = Path.home() / "Library" / "Application Support" / "am2sc"
STATE_FILE = APP_DIR / "state.json"
ARCHIVE_FILE = APP_DIR / "scdl-archive.txt"
DEFAULT_OUT = Path.home() / "Music" / "am2sc"
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".aiff", ".aif", ".flac", ".opus", ".ogg"}

# Parole che indicano una versione "diversa" dall'originale.
VARIANT_WORDS = [
    "remix", "edit", "bootleg", "cover", "live", "acoustic", "instrumental",
    "sped up", "speed up", "slowed", "nightcore", "reverb", "8d", "mashup",
    "flip", "rework", "vip", "karaoke", "extended", "radio edit", "dub",
]

# ---------------------------------------------------------------- Apple Music

JXA_EXPORT = r"""
function run(argv) {
  const M = Application("Music");
  let sources;
  if (argv.length) {
    sources = argv.map(n => {
      const p = M.playlists.whose({name: n});
      if (!p.length) throw new Error("Playlist non trovata: " + n);
      return p[0];
    });
  } else {
    sources = [M.libraryPlaylists[0]];
  }
  const out = [];
  sources.forEach(pl => {
    const t = pl.tracks;
    const id = t.persistentID(), name = t.name(), artist = t.artist(),
          album = t.album(), dur = t.duration(), cs = t.cloudStatus(),
          kind = t.mediaKind(), added = t.dateAdded();
    for (let i = 0; i < id.length; i++) {
      out.push({id: id[i], title: name[i], artist: artist[i], album: album[i],
                duration: dur[i], cloud: cs[i], kind: kind[i],
                added: added[i] ? added[i].toISOString() : ""});
    }
  });
  return JSON.stringify(out);
}
"""

JXA_IMPORT = r"""
function run(argv) {
  const M = Application("Music");
  const plName = argv[0];
  let pl = M.userPlaylists.whose({name: plName});
  pl = pl.length ? pl[0] : M.make({new: "userPlaylist", withProperties: {name: plName}});
  argv.slice(1).forEach(p => M.add(Path(p), {to: pl}));
  return "ok";
}
"""


def osa(script: str, *args: str) -> str:
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", script, *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"Errore osascript: {r.stderr.strip()}")
    return r.stdout.strip()


def load_library(playlists: list[str]) -> list[dict]:
    tracks = json.loads(osa(JXA_EXPORT, *playlists))
    return [t for t in tracks if t.get("kind") in (None, "song", "music video") or "music" in str(t.get("kind"))]


# ---------------------------------------------------------------- matching

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_title(title: str) -> str:
    """Rimuove feat./remaster/parentesi non significative dal titolo."""
    t = re.sub(r"[\(\[][^\)\]]*(feat|ft\.|with |remaster|deluxe|version|mono|stereo|explicit|clean)[^\)\]]*[\)\]]",
               "", title or "", flags=re.I)
    t = re.sub(r"\s-\s.*(remaster|version|mono|stereo).*$", "", t, flags=re.I)
    t = re.sub(r"\s(feat|ft)\.?\s.*$", "", t, flags=re.I)
    return t.strip()


def main_artist(artist: str) -> str:
    return re.split(r"\s*(?:,|&| x | feat\.?| ft\.?| featuring | and )\s*", artist or "", flags=re.I)[0].strip()


def sim(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return max(0.85, SequenceMatcher(None, a, b).ratio())
    return SequenceMatcher(None, a, b).ratio()


def variants(s: str) -> set[str]:
    n = f" {norm(s)} "
    return {w for w in VARIANT_WORDS if f" {w} " in n}


def score(am: dict, sc) -> tuple[float, str]:
    policy = getattr(sc, "policy", "") or ""
    if policy in ("SNIP", "BLOCK"):
        return 0.0, f"policy {policy}"
    if not getattr(sc, "streamable", True):
        return 0.0, "non streamable"
    media = getattr(sc, "media", None)
    protos = [x.format.protocol for x in getattr(media, "transcodings", None) or []]
    if protos and all("encrypted" in p for p in protos):
        return 0.0, "protetto da DRM"

    title = clean_title(am["title"])
    artist = main_artist(am["artist"])
    sc_title = sc.title or ""
    sc_user = getattr(sc.user, "username", "") if getattr(sc, "user", None) else ""

    # Molti upload hanno "Artista - Titolo" nel titolo.
    t_sim = max(sim(title, sc_title), sim(f"{artist} {title}", sc_title))
    a_sim = max(sim(artist, sc_user), 1.0 if norm(artist) and norm(artist) in norm(sc_title) else 0.0)

    am_dur = float(am.get("duration") or 0)
    sc_dur = (getattr(sc, "full_duration", None) or sc.duration or 0) / 1000
    if am_dur and sc_dur:
        diff = abs(am_dur - sc_dur)
        if diff > 20:
            return 0.0, f"durata diversa ({diff:.0f}s)"
        d_score = 1.0 - diff / 20
    else:
        d_score = 0.5

    extra = variants(sc_title) - variants(am["title"]) - variants(am["album"])
    penalty = 0.25 if extra else 0.0

    s = 0.45 * t_sim + 0.30 * a_sim + 0.25 * d_score - penalty
    return s, f"t={t_sim:.2f} a={a_sim:.2f} d={d_score:.2f}" + (f" variante:{','.join(extra)}" if extra else "")


def find_best(client: SoundCloud, am: dict, max_results: int = 10):
    query = f"{main_artist(am['artist'])} {clean_title(am['title'])}".strip()
    best, best_s, best_info = None, 0.0, ""
    rejected: dict[str, int] = {}
    n = 0
    try:
        for n, sc in enumerate(client.search_tracks(query), 1):
            if n > max_results:
                break
            s, info = score(am, sc)
            if s == 0.0:
                reason = info.split(" (")[0]
                rejected[reason] = rejected.get(reason, 0) + 1
            elif s > best_s:
                best, best_s, best_info = sc, s, info
    except Exception as e:  # rete / rate limit
        return None, 0.0, f"errore ricerca: {e}"
    if not best:
        best_info = "nessun risultato" if not n else "scartati: " + ", ".join(f"{k}×{v}" for k, v in rejected.items())
    return best, best_s, best_info


# ---------------------------------------------------------------- download

def audio_files(d: Path) -> set[Path]:
    return {p for p in d.rglob("*") if p.suffix.lower() in AUDIO_EXT}


def download(url: str, out: Path, extra_args: list[str]) -> list[Path]:
    before = audio_files(out)
    cmd = ["scdl", "-l", url, "--path", str(out), "-c", "--hide-progress",
           "--download-archive", str(ARCHIVE_FILE), "--no-playlist", *extra_args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    new = sorted(audio_files(out) - before)
    log = f"{r.stdout}\n{r.stderr}"
    if not new:
        if re.search(r"already (been recorded in the archive|downloaded|exists)", log, re.I):
            return []
        errs = [l for l in log.splitlines() if "ERROR" in l] or log.strip().splitlines()[-1:] or ["nessun file scaricato"]
        raise RuntimeError(errs[-1].strip())
    return new


# ---------------------------------------------------------------- state

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    tmp.replace(STATE_FILE)


def key_of(t: dict) -> str:
    return f"{norm(main_artist(t['artist']))}|{norm(clean_title(t['title']))}"


# ---------------------------------------------------------------- commands

def cmd_sync(a) -> None:
    if not shutil.which("scdl") and not a.dry_run:
        sys.exit("scdl non trovato nel PATH")
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    APP_DIR.mkdir(parents=True, exist_ok=True)

    print("Leggo la libreria di Apple Music…")
    tracks = load_library(a.playlist or [])
    if a.recent:
        tracks = sorted(tracks, key=lambda t: t.get("added") or "", reverse=True)[: a.recent]
    if a.cloud:
        allowed = {c.strip() for c in a.cloud.split(",")}
        tracks = [t for t in tracks if t["cloud"] in allowed]

    state = load_state()
    todo, seen = [], set()
    for t in tracks:
        k = key_of(t)
        if k in seen or not k.strip("|"):
            continue
        seen.add(k)
        st = state.get(k, {}).get("status")
        if st == "downloaded" or (st == "notfound" and not a.retry) or (st == "skipped" and not a.retry):
            continue
        todo.append(t)
    if a.limit:
        todo = todo[: a.limit]
    print(f"{len(tracks)} brani selezionati, {len(todo)} da elaborare.\n")

    client = SoundCloud()
    extra = []
    if a.mp3:
        extra.append("--onlymp3")
    if a.only_original:
        extra.append("--only-original")

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True) or print("\n⏹  Interrompo dopo il brano corrente…"))

    ok = nf = err = 0
    imported: list[Path] = []
    for i, t in enumerate(todo, 1):
        if stop["flag"]:
            break
        label = f"{t['artist']} – {t['title']}"
        print(f"[{i}/{len(todo)}] {label}")
        k = key_of(t)
        sc, s, info = find_best(client, t)
        entry = {"artist": t["artist"], "title": t["title"], "am_id": t["id"], "ts": int(time.time())}

        if not sc or s < a.min_score:
            print(f"   ✗ nessun match affidabile" + (f" (migliore {s:.2f}: {sc.title!r} {info})" if sc else f" ({info})"))
            entry.update(status="notfound", best=getattr(sc, "permalink_url", None), score=round(s, 3))
            nf += 1
        elif a.dry_run:
            print(f"   ≈ {s:.2f}  {sc.user.username} – {sc.title}\n     {sc.permalink_url}  [{info}]")
            continue
        else:
            print(f"   ↓ {s:.2f}  {sc.user.username} – {sc.title}")
            try:
                files = download(sc.permalink_url, out, extra)
                entry.update(status="downloaded", url=sc.permalink_url, score=round(s, 3),
                             files=[str(f) for f in files])
                imported += files
                ok += 1
                print(f"   ✓ {', '.join(f.name for f in files) or '(già presente in archivio)'}")
            except Exception as e:
                entry.update(status="error", url=sc.permalink_url, error=str(e))
                err += 1
                print(f"   ! errore download: {e}")

        if not a.dry_run:
            state[k] = entry
            save_state(state)
        time.sleep(a.delay)

    if a.import_music and imported:
        print(f"\nImporto {len(imported)} file in Music (playlist “{a.import_playlist}”)…")
        for j in range(0, len(imported), 50):
            osa(JXA_IMPORT, a.import_playlist, *map(str, imported[j:j + 50]))

    if not a.dry_run:
        print(f"\nFatto: {ok} scaricati, {nf} non trovati, {err} errori. File in {out}")


def cmd_status(a) -> None:
    state = load_state()
    counts: dict[str, int] = {}
    for v in state.values():
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    print(f"Stato: {STATE_FILE}")
    for k, v in sorted(counts.items()):
        print(f"  {k:<11} {v}")
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["status", "artist", "title", "score", "url_or_best", "error"])
            for v in state.values():
                w.writerow([v["status"], v["artist"], v["title"], v.get("score", ""),
                            v.get("url") or v.get("best") or "", v.get("error", "")])
        print(f"Report scritto in {a.csv}")


def cmd_reset(a) -> None:
    state = load_state()
    if a.status:
        state = {k: v for k, v in state.items() if v["status"] != a.status}
    else:
        state = {}
    save_state(state)
    print("Stato aggiornato.")


def main() -> None:
    p = argparse.ArgumentParser(prog="am2sc", description="Sincronizza Apple Music → SoundCloud (scdl)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="cerca e scarica i brani della libreria")
    s.add_argument("-p", "--playlist", action="append", help="limita a una playlist (ripetibile)")
    s.add_argument("-o", "--out", default=str(DEFAULT_OUT), help=f"cartella di download (default {DEFAULT_OUT})")
    s.add_argument("-n", "--dry-run", action="store_true", help="mostra i match senza scaricare")
    s.add_argument("-l", "--limit", type=int, help="elabora al massimo N brani")
    s.add_argument("-r", "--recent", type=int, metavar="N", help="solo gli ultimi N brani aggiunti alla libreria")
    s.add_argument("--cloud", help="filtra per cloud status, es. subscription,matched,uploaded")
    s.add_argument("--retry", action="store_true", help="riprova i brani non trovati/in errore")
    s.add_argument("--min-score", type=float, default=0.70, help="soglia di confidenza (default 0.70)")
    s.add_argument("--delay", type=float, default=1.0, help="pausa tra un brano e l'altro (s)")
    s.add_argument("--mp3", action="store_true", help="solo mp3 (scdl --onlymp3)")
    s.add_argument("--only-original", action="store_true",
                   help="solo brani con download originale offerto dall'artista")
    s.add_argument("--import", dest="import_music", action="store_true", help="importa i file scaricati in Music")
    s.add_argument("--import-playlist", default="SoundCloud", help="playlist di destinazione per --import")
    s.set_defaults(func=cmd_sync)

    st = sub.add_parser("status", help="riepilogo dello stato")
    st.add_argument("--csv", help="esporta un report CSV")
    st.set_defaults(func=cmd_status)

    r = sub.add_parser("reset", help="azzera lo stato (tutto o solo uno status)")
    r.add_argument("--status", choices=["downloaded", "notfound", "error"])
    r.set_defaults(func=cmd_reset)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
