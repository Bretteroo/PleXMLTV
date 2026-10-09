#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
#
# PleXMLTV: export the Plex Live TV guide as XMLTV.
# Copyright (C) 2026 Bretteroo
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, version 3 of the License only.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero
# General Public License for details: https://www.gnu.org/licenses/agpl-3.0.html
#
# Source: https://github.com/Bretteroo/PleXMLTV
"""PleXMLTV: export the Live TV guide from a Plex Media Server as XMLTV.

Plex Pass subscribers get Gracenote guide data through their Plex Media
Server, where it sits in Plex's own undocumented SQLite databases. Run on
the server, this tool reads that guide straight out of those files and
writes it in the open XMLTV format. The output goes to xmltv.xml beside this
script unless told otherwise.

Where the data comes from (all under Plex's data directory, typically
/var/lib/plexmediaserver/Library/Application Support/Plex Media Server):

    Plug-in Support/Databases/tv.plex.providers.epg.<type>-<dvr uuid>.db
        One SQLite database per guide-backed DVR. Each row of media_items
        is one airing: begins_at and ends_at in epoch seconds plus a JSON
        extra_data blob naming the channel (call sign, number, logo and
        Plex's channel identifier) and whether the airing is a premiere.
        metadata_items holds the programs: type 1 is a movie, 4 an
        episode whose parent is a season (3) whose parent is a show (2).
        Plex only stores guide data for channels you enabled, and keeps
        roughly two weeks of it.

    Plug-in Support/Databases/com.plexapp.plugins.library.db
        media_provider_resources lists the DVRs and tuners. A tuner row's
        extra_data carries pv:channelMappingByKey, which maps the number
        the tuner reports to Plex's channel identifier. That number is
        what any other program using the same tuner will see, so it
        becomes the XMLTV channel number and id.

These schemas are Plex's own and undocumented, but they have been stable
for years. The databases use write-ahead logging, so they are read live in
read-only mode; if that is not possible the EPG database is copied to a
temporary directory first.

Needs Python 3.10 or newer and nothing outside the standard library.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from xml.etree import ElementTree as ET

__version__ = "1.0.0"

log = logging.getLogger("plexmltv")

GENERATOR_NAME = "PleXMLTV"
PROJECT_URL = "https://github.com/Bretteroo/PleXMLTV"
LOGO = r"""
 _____ _     __ __ _____ __  _____ _____
|  _  | |___|  |  |     |  ||_   _|  |  |
|   __| | -_|-   -| | | |  |__| | |  |  |
|__|  |_|___|__|__|_|_|_|_____|_|  \___/
""".strip("\n")
BANNER = f"{LOGO}\n\nversion {__version__}\n{PROJECT_URL}"
# Left-to-right gradient for the wordmark on color terminals: Plex gold through
# orange and pink to the purple used for the XMLTV badge in the README.
GRADIENT = ((0xE5, 0xA0, 0x0D), (0xF0, 0x60, 0x3C), (0xD9, 0x47, 0x9B), (0x6B, 0x4F, 0xBB))
RESET = "\x1b[0m"


def gradient_color(t: float) -> tuple[int, int, int]:
    """Interpolate GRADIENT at t in [0, 1]."""
    t = min(max(t, 0.0), 1.0) * (len(GRADIENT) - 1)
    i = min(int(t), len(GRADIENT) - 2)
    f = t - i
    a, b = GRADIENT[i], GRADIENT[i + 1]
    return tuple(round(x + (y - x) * f) for x, y in zip(a, b))  # type: ignore[return-value]


def colorize(text: str) -> str:
    """Paint each column of a block of text with the gradient; spaces stay unpainted."""
    lines = text.split("\n")
    width = max(len(line) for line in lines)
    out = []
    for line in lines:
        pieces, current = [], None
        for x, ch in enumerate(line):
            if ch == " ":
                pieces.append(ch)
                continue
            color = gradient_color(x / max(width - 1, 1))
            if color != current:
                pieces.append("\x1b[38;2;%d;%d;%dm" % color)
                current = color
            pieces.append(ch)
        pieces.append(RESET if current else "")
        out.append("".join(pieces))
    return "\n".join(out)


def stderr_wants_color() -> bool:
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    try:
        return sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def print_banner() -> None:
    # Always on stderr so it never lands in the XML when --out is -.
    logo = colorize(LOGO) if stderr_wants_color() else LOGO
    print(f"{logo}\n\nversion {__version__}\n{PROJECT_URL}\n", file=sys.stderr, flush=True)


class ProblemCounter(logging.Handler):
    """Counts warnings and errors so the closing line can say whether the run was clean."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.warnings = 0
        self.errors = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            self.errors += 1
        else:
            self.warnings += 1

    def closing_line(self) -> str:
        if not self.warnings and not self.errors:
            return f"{GENERATOR_NAME} finished successfully with no errors."
        parts = []
        if self.errors:
            parts.append(f"{self.errors} error{'s' if self.errors != 1 else ''}")
        if self.warnings:
            parts.append(f"{self.warnings} warning{'s' if self.warnings != 1 else ''}")
        return f"{GENERATOR_NAME} finished with {' and '.join(parts)}; see above."

NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?$")
EPG_DB_RE = re.compile(r"^tv\.plex\.providers\.epg\.([A-Za-z0-9_]+)-([0-9a-fA-F-]{36})\.db$")
CHANNEL_TITLE_RE = re.compile(r"^\s*\S+\s+\S+\s+\((.+)\)\s*$")
PLEX_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")

ID_MODES = ("number", "callsign", "plex")

SCRIPT_DIR = Path(__file__).resolve().parent


def default_output_path(script_dir: Path | None = None) -> Path:
    """xmltv.xml beside the script, unless the script lives somewhere a
    guide file does not belong (a pip install) or that is not writable."""
    here = script_dir or SCRIPT_DIR
    parts = {part.lower() for part in here.parts}
    if parts & {"site-packages", "dist-packages"} or not os.access(here, os.W_OK):
        return Path.cwd() / "xmltv.xml"
    return here / "xmltv.xml"


DEFAULT_OUT = default_output_path()

PLEX_DIR_NAME = "Plex Media Server"
PREFERENCES_NAME = "Preferences.xml"
DATABASES_SUBDIR = Path("Plug-in Support") / "Databases"
LIBRARY_DB_NAME = "com.plexapp.plugins.library.db"

# Where Plex keeps its data directory on the platforms it ships for. The
# PLEX_MEDIA_SERVER_APPLICATION_SUPPORT_DIR variable, when set, wins.
PLEX_DATA_ROOTS = (
    "/var/lib/plexmediaserver/Library/Application Support",  # deb / rpm packages
    "/var/snap/plexmediaserver/common/Library/Application Support",  # snap
    "/config/Library/Application Support",  # official and linuxserver docker images
    "~/Library/Application Support",  # macOS
    "/usr/local/plexdata",  # FreeBSD / FreeNAS plugin
    "/usr/local/plexdata-plexpass",
    "/volume1/Plex/Library/Application Support",  # Synology DSM 7
    "/volume1/PlexMediaServer/AppData",  # Synology DSM 6
    "/share/CACHEDEV1_DATA/.qpkg/PlexMediaServer/Library",  # QNAP
    "/opt/plexmediaserver/Library/Application Support",
)

METADATA_TYPES = {1: "movie", 2: "show", 3: "season", 4: "episode"}


class PlexError(RuntimeError):
    """Raised when the Plex data cannot be used."""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def text(value: Any) -> str:
    """Return value as a stripped string with control characters removed."""
    if value is None:
        return ""
    s = str(value).strip()
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)


def to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return text(value).lower() in ("1", "true", "yes")


def key_tail(value: str) -> str:
    """Plex channel keys look like <lineup>-<channel>; some places carry
    only the channel half. Compare on that half."""
    return value.rsplit("-", 1)[-1] if value else ""


def xmltv_time(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def parse_plex_date(value: Any) -> date | None:
    """Plex stores dates as epoch seconds in the guide database and as
    'YYYY-MM-DD[ HH:MM:SS]' text elsewhere; accept both."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    s = text(value)
    if not s:
        return None
    for fmt in PLEX_DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def split_tags(value: Any) -> list[str]:
    """Plex stores lists such as genres as 'Drama|Romance'."""
    return [t.strip() for t in text(value).split("|") if t.strip()]


def add_text(parent: ET.Element, tag: str, value: Any, **attrs: str) -> ET.Element | None:
    s = text(value)
    if not s:
        return None
    el = ET.SubElement(parent, tag, {k: v for k, v in attrs.items() if v})
    el.text = s
    return el


def sanitize_id(value: str) -> str:
    return re.sub(r"\s+", "-", text(value))


def decode_pv(value: Any) -> str:
    """Plex base64-encodes the pv: values that hold key=value pairs (the
    channel mappings) and leaves simple ones plain. A short plain value such
    as 'eng' is also valid base64, so only accept a decoding that yields
    printable key=value text."""
    raw = text(value)
    if not raw or not re.fullmatch(r"[A-Za-z0-9+/]+=*", raw):
        return raw
    try:
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return raw
    if "=" in decoded and decoded.isprintable():
        return decoded
    return raw


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class EpgSource:
    """One guide database and what the library database says about its DVR."""

    dvr_key: str  # the DVR uuid, which is also the database file name suffix
    db_path: Path
    lineup: str = ""
    language: str = ""

    @property
    def name(self) -> str:
        return self.db_path.name


@dataclass
class Channel:
    plex_id: str
    grid_key: str
    title: str = ""
    call_sign: str = ""
    vcn: str = ""
    thumb: str = ""
    tuner_number: str = ""
    enabled: bool | None = None
    source: EpgSource | None = None
    xmltv_id: str = ""

    @property
    def number(self) -> str:
        """The number the tuner itself reports, when Plex tells us it."""
        return self.tuner_number or self.vcn

    @property
    def label(self) -> str:
        return self.call_sign or self.title or self.number or self.plex_id


@dataclass
class Config:
    data_dir: str | None = None
    db: str | None = None
    out: str = str(DEFAULT_OUT)
    days: int | None = None  # None exports everything Plex has
    past_days: int | None = None
    id_mode: str = "number"
    dvr: str | None = None
    include_disabled: bool = False
    channels: list[str] = field(default_factory=list)
    language: str = "en"


@dataclass
class GuideStats:
    channels: int = 0
    programs: int = 0
    databases: int = 0
    failed_databases: int = 0
    empty_channels: list[str] = field(default_factory=list)


Airing = tuple[Channel, dict, dict]  # channel, program, airing


# --------------------------------------------------------------------------
# Locating Plex's files
# --------------------------------------------------------------------------


def plex_data_dir_candidates() -> list[Path]:
    paths: list[Path] = []
    env_dir = os.environ.get("PLEX_MEDIA_SERVER_APPLICATION_SUPPORT_DIR")
    if env_dir:
        paths.append(Path(env_dir).expanduser() / PLEX_DIR_NAME)
    for root in PLEX_DATA_ROOTS:
        paths.append(Path(root).expanduser() / PLEX_DIR_NAME)
    local_app = os.environ.get("LOCALAPPDATA")  # Windows
    if local_app:
        paths.append(Path(local_app) / PLEX_DIR_NAME)
    return paths


def find_plex_data_dir(explicit: str | None = None) -> Path | None:
    """The 'Plex Media Server' directory, which holds Preferences.xml and
    the Plug-in Support/Databases folder."""
    if explicit:
        p = Path(explicit).expanduser()
        if p.name != PLEX_DIR_NAME and (p / PLEX_DIR_NAME).is_dir():
            p = p / PLEX_DIR_NAME
        return p
    for candidate in plex_data_dir_candidates():
        try:
            if (candidate / PREFERENCES_NAME).is_file() or (candidate / DATABASES_SUBDIR).is_dir():
                return candidate
        except OSError:
            continue
    return None


def find_epg_databases(data_dir: Path) -> list[Path]:
    db_dir = data_dir / DATABASES_SUBDIR
    try:
        return sorted(p for p in db_dir.iterdir() if EPG_DB_RE.match(p.name))
    except OSError:
        return []


# --------------------------------------------------------------------------
# Reading Plex's databases
# --------------------------------------------------------------------------


def sqlite_uri(path: Path, **params: str) -> str:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"file:{urllib.parse.quote(str(path))}" + (f"?{query}" if query else "")


class DatabaseReader:
    """Opens Plex's SQLite databases read-only, once each, copying a file
    aside when the live one cannot be opened (for example when the WAL
    index is not writable by this user). close() closes every connection
    and removes the copies."""

    def __init__(self) -> None:
        self._tmpdir: str | None = None
        self._connections: dict[Path, sqlite3.Connection] = {}

    def close(self) -> None:
        for con in self._connections.values():
            try:
                con.close()
            except sqlite3.Error:
                pass
        self._connections.clear()
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    def __enter__(self) -> "DatabaseReader":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _try_open(self, uri: str) -> sqlite3.Connection:
        con = sqlite3.connect(uri, uri=True, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("select count(*) from sqlite_master")  # forces the file to really open
        return con

    def open(self, path: Path, *, copy_on_failure: bool = True) -> sqlite3.Connection:
        """A connection to path, shared by later calls for the same path."""
        path = Path(path)
        cached = self._connections.get(path)
        if cached is not None:
            return cached
        if not path.is_file():
            raise PlexError(f"database not found: {path}")
        con = self._open_uncached(path, copy_on_failure)
        self._connections[path] = con
        return con

    def _open_uncached(self, path: Path, copy_on_failure: bool) -> sqlite3.Connection:
        try:
            return self._try_open(sqlite_uri(path, mode="ro"))
        except sqlite3.Error as exc:
            first_error = exc
        if copy_on_failure:
            try:
                return self._try_open(sqlite_uri(self._copy(path)))
            except (sqlite3.Error, OSError) as exc:
                raise PlexError(f"cannot open {path} ({first_error}); copying it failed too: {exc}") from None
        try:
            # immutable=1 skips the WAL index; fine for a small, rarely written table
            return self._try_open(sqlite_uri(path, mode="ro", immutable="1"))
        except sqlite3.Error as exc:
            raise PlexError(f"cannot open {path}: {exc}") from None

    def _copy(self, path: Path) -> Path:
        if self._tmpdir is None:
            self._tmpdir = tempfile.mkdtemp(prefix="plexmltv_")
        log.info("Copying %s to %s", path.name, self._tmpdir)
        target = Path(self._tmpdir) / path.name
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(path) + suffix)
            if src.exists():
                shutil.copy2(src, Path(str(target) + suffix))
        return target


@dataclass
class TunerMapping:
    """What the main library database knows about one DVR's tuner(s)."""

    dvr_uuid: str
    dvr_id: str = ""
    lineup: str = ""
    lineup_title: str = ""
    language: str = ""
    number_by_key: dict[str, str] = field(default_factory=dict)  # Plex channel key -> tuner number
    enabled_numbers: set[str] = field(default_factory=set)
    devices: int = 0


def parse_provider_extra(raw: Any) -> dict[str, str]:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}


def load_tuner_mappings(con: sqlite3.Connection) -> dict[str, TunerMapping]:
    """DVRs keyed by uuid, with channel key -> tuner number from their tuners."""
    try:
        rows = con.execute(
            "select id, parent_id, type, identifier, uuid, extra_data from media_provider_resources"
        ).fetchall()
    except sqlite3.Error as exc:
        raise PlexError(f"cannot read media_provider_resources: {exc}") from None
    dvrs: dict[str, TunerMapping] = {}
    by_id: dict[str, TunerMapping] = {}
    for r in rows:
        if text(r["identifier"]) != "tv.plex.harvesters.dvr":
            continue
        extra = parse_provider_extra(r["extra_data"])
        m = TunerMapping(
            dvr_uuid=text(r["uuid"]).lower(),
            dvr_id=text(r["id"]),
            lineup=text(extra.get("pv:lineup")),
            lineup_title=urllib.parse.unquote(text(extra.get("pv:lineupTitle"))),
            language=text(extra.get("pv:language")),
        )
        dvrs[m.dvr_uuid] = m
        by_id[m.dvr_id] = m
    for r in rows:
        parent = text(r["parent_id"])
        if parent not in by_id or "pv:channelMappingByKey" not in (r["extra_data"] or ""):
            continue
        m = by_id[parent]
        extra = parse_provider_extra(r["extra_data"])
        m.devices += 1
        for number, key in urllib.parse.parse_qsl(decode_pv(extra.get("pv:channelMappingByKey")), keep_blank_values=True):
            m.number_by_key.setdefault(text(key), text(number))
        enabled = text(extra.get("pv:channelsEnabled"))  # plain comma-separated list
        m.enabled_numbers.update(n.strip() for n in enabled.split(",") if n.strip())
    return dvrs


def channel_title_from_label(label: str, call_sign: str, vcn: str) -> str:
    """Plex labels channels '2.1 KCBSDT (CBS)'; the network name is what
    people recognize."""
    m = CHANNEL_TITLE_RE.match(label)
    if m:
        return m.group(1).strip()
    stripped = label
    for prefix in (vcn, call_sign):
        if prefix and stripped.startswith(prefix):
            stripped = stripped[len(prefix):].strip()
    return stripped or call_sign or label


def parse_airing_extra(raw: Any) -> dict[str, str]:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return {k: text(v) for k, v in data.items()} if isinstance(data, dict) else {}


def db_channels(con: sqlite3.Connection, source: EpgSource, mapping: TunerMapping | None) -> list[Channel]:
    """Channels are not a table of their own; every airing names its
    channel, so take one airing per channel."""
    try:
        rows = con.execute(
            "select channel_id, extra_data, count(*) as airings from media_items "
            "where deleted_at is null and extra_data is not null group by channel_id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise PlexError(f"cannot read media_items in {source.name}: {exc}") from None
    channels: list[Channel] = []
    for r in rows:
        extra = parse_airing_extra(r["extra_data"])
        plex_id = extra.get("at:channelIdentifier") or ""
        grid_key = extra.get("at:gridKey") or plex_id
        if not plex_id and not grid_key:
            continue
        call_sign = extra.get("at:channelCallSign") or ""
        vcn = extra.get("at:channelVcn") or ""
        label = extra.get("at:channelTitle") or ""
        ch = Channel(
            plex_id=plex_id or grid_key,
            grid_key=grid_key,
            title=channel_title_from_label(label, call_sign, vcn),
            call_sign=call_sign,
            vcn=vcn,
            thumb=extra.get("at:channelThumb") or "",
            source=source,
        )
        if mapping and mapping.number_by_key:
            number = mapping.number_by_key.get(ch.plex_id) or mapping.number_by_key.get(key_tail(ch.plex_id), "")
            if number and NUMBER_RE.match(number):
                ch.tuner_number = number
            if number and mapping.enabled_numbers:
                ch.enabled = number in mapping.enabled_numbers
        channels.append(ch)
    return channels


AIRINGS_SQL = """
select mi.channel_id, mi.begins_at, mi.ends_at, mi.extra_data as airing_extra, mi.height,
       e.metadata_type, e.guid, e.title, e.summary, e.content_rating, e."index" as episode_index,
       e.originally_available_at, e.year, e.tags_genre, e.tags_director, e.tags_writer, e.tags_star,
       e.user_thumb_url as thumb, e.user_art_url as art, e.rating, e.audience_rating,
       s."index" as season_index,
       sh.title as show_title, sh.user_thumb_url as show_thumb, sh.user_art_url as show_art,
       sh.tags_genre as show_genre, sh.content_rating as show_rating
from media_items mi
join metadata_items e on e.id = mi.metadata_item_id
left join metadata_items s on s.id = e.parent_id
left join metadata_items sh on sh.id = s.parent_id
where mi.deleted_at is null
  and (:start is null or mi.ends_at > :start)
  and (:end is null or mi.begins_at < :end)
order by mi.channel_id, mi.begins_at
"""


def db_row_to_airing(row: sqlite3.Row) -> tuple[dict, dict]:
    """Split a joined row into the program (what is on) and the airing
    (when and where it is on)."""
    kind = METADATA_TYPES.get(row["metadata_type"], "")
    extra = parse_airing_extra(row["airing_extra"])
    item: dict[str, Any] = {
        "type": kind,
        "guid": row["guid"],
        "title": row["title"],
        "summary": row["summary"],
        "contentRating": row["content_rating"] or row["show_rating"],
        "index": row["episode_index"],
        "parentIndex": row["season_index"],
        "originallyAvailableAt": row["originally_available_at"],
        "year": row["year"],
        "thumb": row["thumb"],
        "art": row["art"],
        "rating": row["rating"],
        "audienceRating": row["audience_rating"],
        "genres": split_tags(row["tags_genre"]) or split_tags(row["show_genre"]),
        "directors": split_tags(row["tags_director"]),
        "writers": split_tags(row["tags_writer"]),
        "actors": split_tags(row["tags_star"]),
    }
    if kind == "episode":
        item["grandparentTitle"] = row["show_title"]
        item["grandparentThumb"] = row["show_thumb"]
        item["grandparentArt"] = row["show_art"]
    media = {
        "beginsAt": row["begins_at"],
        "endsAt": row["ends_at"],
        "premiere": extra.get("at:premiere"),
        "channelIdentifier": extra.get("at:channelIdentifier"),
        "gridKey": extra.get("at:gridKey"),
        "channelVcn": extra.get("at:channelVcn"),
        "channelCallSign": extra.get("at:channelCallSign"),
        "videoResolution": row["height"],
    }
    return item, media


class ChannelIndex:
    """Route an airing's channel details to one of our channels."""

    def __init__(self, channels: list[Channel]) -> None:
        self.by_id: dict[str, Channel] = {}
        self.by_tail: dict[str, Channel] = {}
        self.by_vcn_call_sign: dict[tuple[str, str], Channel] = {}
        for ch in channels:
            for key in (ch.plex_id, ch.grid_key):
                if key:
                    self.by_id.setdefault(key, ch)
                    self.by_tail.setdefault(key_tail(key), ch)
            if ch.vcn and ch.call_sign:
                self.by_vcn_call_sign.setdefault((ch.vcn, ch.call_sign), ch)

    def lookup(self, media: dict[str, Any]) -> Channel | None:
        keys = [text(media.get("channelIdentifier")), text(media.get("gridKey"))]
        for key in keys:
            if key and key in self.by_id:
                return self.by_id[key]
        for key in keys:
            if key and key_tail(key) in self.by_tail:
                return self.by_tail[key_tail(key)]
        return self.by_vcn_call_sign.get((text(media.get("channelVcn")), text(media.get("channelCallSign"))))


def db_airings(con: sqlite3.Connection, index: ChannelIndex, start: int | None, end: int | None) -> Iterator[Airing]:
    """Airings overlapping [start, end) in epoch seconds; either bound may
    be None to leave that side open."""
    try:
        cursor = con.execute(AIRINGS_SQL, {"start": start, "end": end})
    except sqlite3.Error as exc:
        raise PlexError(f"guide query failed: {exc}") from None
    for row in cursor:
        item, media = db_row_to_airing(row)
        ch = index.lookup(media)
        if ch is not None:
            yield ch, item, media


def discover_db_sources(config: Config) -> tuple[Path, list[EpgSource]]:
    """The EPG databases to read, from --db or the Plex data directory."""
    if config.db:
        p = Path(config.db).expanduser()
        if p.is_file():
            data_dir = p.parent.parent.parent if p.parent.name == "Databases" else p.parent
            dbs = [p]
        else:
            data_dir = find_plex_data_dir(str(p)) or p
            dbs = find_epg_databases(data_dir)
    else:
        found = find_plex_data_dir(config.data_dir)
        if found is None:
            raise PlexError("no Plex data directory found; pass --data-dir or --db")
        data_dir = found
        dbs = find_epg_databases(data_dir)
    if not dbs:
        raise PlexError(f"no EPG database (tv.plex.providers.epg.*.db) under {data_dir / DATABASES_SUBDIR}")
    sources: list[EpgSource] = []
    for db in dbs:
        m = EPG_DB_RE.match(db.name)
        uuid = m.group(2).lower() if m else ""
        if config.dvr and config.dvr.lower() not in (uuid, db.name):
            continue
        sources.append(EpgSource(dvr_key=uuid, db_path=db))
    if not sources:
        raise PlexError(f"no EPG database matches --dvr {config.dvr}")
    return data_dir, sources


def load_library_mappings(data_dir: Path, reader: DatabaseReader) -> dict[str, TunerMapping]:
    """Tuner mappings from the main library database, or nothing with a
    warning when it cannot be read. The export still works without it."""
    library_db = data_dir / DATABASES_SUBDIR / LIBRARY_DB_NAME
    if not library_db.is_file():
        log.warning("No %s next to the EPG database; using Plex's channel numbers", LIBRARY_DB_NAME)
        return {}
    try:
        return load_tuner_mappings(reader.open(library_db, copy_on_failure=False))
    except PlexError as exc:
        log.warning("Tuner mapping unavailable (%s); using Plex's channel numbers", exc)
        return {}


def load_db_channels(config: Config, reader: DatabaseReader) -> tuple[list[Channel], dict[str, TunerMapping]]:
    data_dir, sources = discover_db_sources(config)
    mappings = load_library_mappings(data_dir, reader)
    channels: list[Channel] = []
    for source in sources:
        mapping = mappings.get(source.dvr_key)
        if mapping:
            source.lineup = mapping.lineup
            source.language = mapping.language
        log.info("Guide database %s%s", source.name, f" ({mapping.lineup_title})" if mapping and mapping.lineup_title else "")
        found = db_channels(reader.open(source.db_path), source, mapping)
        log.info("%s: %d channels with guide data", source.name, len(found))
        channels.extend(found)
    return channels, mappings


# --------------------------------------------------------------------------
# Channel selection
# --------------------------------------------------------------------------


def assign_xmltv_ids(channels: list[Channel], mode: str) -> None:
    used: dict[str, int] = {}
    for ch in channels:
        if mode == "callsign":
            base = ch.call_sign or ch.number or ch.plex_id
        elif mode == "plex":
            base = ch.plex_id
        else:
            base = ch.number or ch.call_sign or ch.plex_id
        base = sanitize_id(base)
        n = used.get(base, 0)
        used[base] = n + 1
        ch.xmltv_id = base if n == 0 else f"{base}-{n + 1}"


def channel_selected(ch: Channel, wanted: list[str]) -> bool:
    if not wanted:
        return True
    keys = {ch.number.lower(), ch.vcn.lower(), ch.call_sign.lower(), ch.xmltv_id.lower(), ch.plex_id.lower()}
    return any(w.strip().lower() in keys for w in wanted)


def channel_sort_key(ch: Channel) -> tuple:
    """Order by channel number; ties and unnumbered channels keep Plex's order."""
    parts = ch.number.split(".") if ch.number else []
    nums = tuple(int(p) if p.isdigit() else 0 for p in parts)
    return (0 if nums else 1, nums)


def finish_channels(channels: list[Channel], config: Config) -> list[Channel]:
    unique: list[Channel] = []
    seen: set[str] = set()
    for ch in channels:
        if ch.plex_id in seen:
            continue
        seen.add(ch.plex_id)
        unique.append(ch)
    channels = unique
    if not channels:
        raise PlexError("Plex has no guide channels")
    if not config.include_disabled:
        skipped = [c for c in channels if c.enabled is False]
        if skipped:
            log.info("Skipping %d channels disabled in Plex (use --include-disabled to keep them)", len(skipped))
        channels = [c for c in channels if c.enabled is not False]
    assign_xmltv_ids(channels, config.id_mode)
    if config.channels:
        channels = [c for c in channels if channel_selected(c, config.channels)]
        if not channels:
            raise PlexError("no channels matched --channels")
    channels.sort(key=channel_sort_key)
    return channels


# --------------------------------------------------------------------------
# XMLTV elements
# --------------------------------------------------------------------------


def usable_url(url: str) -> str:
    """Guide artwork is absolute (metadata-static.plex.tv). A relative path
    would need the server address and a Plex token to fetch, so leave it out
    rather than write a URL nothing else can load."""
    return url if url.startswith(("http://", "https://")) else ""


def pick_images(item: dict[str, Any], is_episode: bool) -> tuple[str, str, str]:
    """Return (poster, still, backdrop) URLs, any of which may be empty.
    An episode's own thumb is a still from it; the show's is the poster."""
    if is_episode:
        poster = text(item.get("grandparentThumb"))
        still = text(item.get("thumb"))
    else:
        poster = text(item.get("thumb"))
        still = ""
    backdrop = text(item.get("art")) or text(item.get("grandparentArt"))
    return poster, still, backdrop


def categories(item: dict[str, Any], is_movie: bool) -> list[str]:
    cats: list[str] = []
    if is_movie:
        cats.append("Movie")  # the conventional XMLTV marker for a film
    cats.extend(text(g) for g in item.get("genres") or [])
    seen: set[str] = set()
    out = []
    for c in cats:
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out


def content_rating(item: dict[str, Any]) -> tuple[str, str]:
    """Return (value, system). Plex gives things like TV-14 or us/PG-13."""
    value = text(item.get("contentRating")).split("/")[-1].strip()
    if not value:
        return "", ""
    upper = value.upper()
    if upper.startswith("TV-"):
        return value, "VCHIP"
    if upper in ("G", "PG", "PG-13", "R", "NC-17"):
        return value, "MPAA"
    return value, ""


CREDIT_TAGS = (("directors", "director"), ("actors", "actor"), ("writers", "writer"))


def add_credits(parent: ET.Element, item: dict[str, Any]) -> None:
    credits: list[tuple[str, str]] = []
    for item_key, xmltv_tag in CREDIT_TAGS:
        credits.extend((xmltv_tag, text(name)) for name in item.get(item_key) or [] if text(name))
    if not credits:
        return
    el = ET.SubElement(parent, "credits")
    for tag, name in credits:
        add_text(el, tag, name)


def programme_element(
    item: dict[str, Any], media: dict[str, Any], channel: Channel, *, language: str
) -> ET.Element | None:
    begins = to_int(media.get("beginsAt"))
    ends = to_int(media.get("endsAt"))
    if begins is None or ends is None or ends <= begins:
        return None

    kind = text(item.get("type")).lower()
    is_episode = kind == "episode"
    is_movie = kind == "movie"
    show_title = text(item.get("grandparentTitle"))
    item_title = text(item.get("title"))
    title = (show_title if is_episode and show_title else item_title) or "Untitled"
    sub_title = item_title if is_episode and show_title and item_title != show_title else ""

    p = ET.Element(
        "programme",
        {"start": xmltv_time(begins), "stop": xmltv_time(ends), "channel": channel.xmltv_id},
    )
    # Children follow the order the XMLTV DTD prescribes.
    add_text(p, "title", title, lang=language)
    add_text(p, "sub-title", sub_title, lang=language)
    add_text(p, "desc", item.get("summary"), lang=language)
    add_credits(p, item)

    orig = parse_plex_date(item.get("originallyAvailableAt"))
    year = to_int(item.get("year"))
    if orig:
        add_text(p, "date", orig.strftime("%Y%m%d"))
    elif year:
        add_text(p, "date", str(year))

    for c in categories(item, is_movie):
        add_text(p, "category", c, lang=language)

    poster, still, backdrop = (usable_url(u) for u in pick_images(item, is_episode))
    primary = poster or still
    if primary:
        ET.SubElement(p, "icon", {"src": primary})

    season = to_int(item.get("parentIndex"))
    episode = to_int(item.get("index"))
    if is_episode and episode is not None and episode >= 1:
        season_part = str(season - 1) if season is not None and season >= 1 else ""
        add_text(p, "episode-num", f"{season_part}.{episode - 1}.", system="xmltv_ns")
        if season is not None and season >= 1:
            add_text(p, "episode-num", f"S{season:02d}E{episode:02d}", system="onscreen")

    is_new = truthy(media.get("premiere"))
    air_day = datetime.fromtimestamp(begins, tz=timezone.utc).date()
    if is_episode and not is_new and orig and orig < air_day - timedelta(days=1):
        ET.SubElement(p, "previously-shown", {"start": orig.strftime("%Y%m%d") + "000000 +0000"})
    if is_new:
        ET.SubElement(p, "new")

    rating_value, rating_system = content_rating(item)
    if rating_value:
        r = ET.SubElement(p, "rating", {"system": rating_system} if rating_system else {})
        add_text(r, "value", rating_value)

    score = to_float(item.get("rating"))
    if score is None:
        score = to_float(item.get("audienceRating"))
    if score is not None and 0 < score <= 10:
        sr = ET.SubElement(p, "star-rating")
        add_text(sr, "value", f"{score:.1f}/10")

    for kind_name, url in (("poster", poster), ("still", still), ("backdrop", backdrop)):
        if url:
            add_text(p, "image", url, type=kind_name)
    return p


def channel_element(ch: Channel, language: str) -> ET.Element:
    el = ET.Element("channel", {"id": ch.xmltv_id})
    # By XMLTV convention the first display-name is the channel's name and
    # a bare numeric display-name is its number, which is what readers use
    # to match tuner channels to guide channels.
    names: list[str] = []
    for candidate in (ch.title, ch.call_sign):
        if candidate and candidate not in names:
            names.append(candidate)
    for n in names:
        add_text(el, "display-name", n, lang=language)
    if ch.number and ch.number not in names:
        add_text(el, "display-name", ch.number)
    if not names and not ch.number:
        add_text(el, "display-name", ch.xmltv_id)
    if ch.thumb:
        ET.SubElement(el, "icon", {"src": ch.thumb})
    return el


# --------------------------------------------------------------------------
# Guide assembly
# --------------------------------------------------------------------------


def day_window(day: date) -> tuple[int, int]:
    """Plex's guide days run midnight to midnight UTC."""
    start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    return start, start + 86400


def guide_window(config: Config, today: date | None = None) -> tuple[int | None, int | None]:
    """Epoch bounds for the export. With neither --days nor --past-days
    given, both are None and everything in the database is exported."""
    if config.days is None and config.past_days is None:
        return None, None
    today = today or datetime.now(timezone.utc).date()
    start = day_window(today - timedelta(days=config.past_days or 0))[0]
    end = day_window(today + timedelta(days=config.days - 1))[1] if config.days else None
    return start, end


def describe_window(start: int | None, end: int | None) -> str:
    if start is None and end is None:
        return "everything Plex has stored"
    if end is None:
        return f"from {xmltv_time(start)} onward"
    return f"between {xmltv_time(start)} and {xmltv_time(end)}"


class GuideBuilder:
    """Accumulates airings per channel, dropping duplicates, and renders the
    XMLTV tree."""

    def __init__(self, channels: list[Channel], config: Config) -> None:
        self.channels = channels
        self.config = config
        self.buckets: dict[str, dict[int, ET.Element]] = {ch.xmltv_id: {} for ch in channels}

    def add(self, ch: Channel, item: dict, media: dict) -> bool:
        begins = to_int(media.get("beginsAt"))
        bucket = self.buckets[ch.xmltv_id]
        if begins is None or begins in bucket:
            return False
        el = programme_element(item, media, ch, language=self.config.language)
        if el is None:
            return False
        bucket[begins] = el
        return True

    def render(self, stats: GuideStats) -> ET.Element:
        root = ET.Element(
            "tv",
            {
                "date": datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S +0000"),
                "source-info-name": "Plex Media Server",
                "generator-info-name": f"{GENERATOR_NAME}/{__version__}",
            },
        )
        for ch in self.channels:
            root.append(channel_element(ch, self.config.language))
        for ch in self.channels:
            bucket = self.buckets[ch.xmltv_id]
            if not bucket:
                stats.empty_channels.append(ch.label)
            for begins in sorted(bucket):
                root.append(bucket[begins])
                stats.programs += 1
        stats.channels = len(self.channels)
        return root


def build_guide(config: Config, channels: list[Channel], reader: DatabaseReader) -> tuple[ET.Element, GuideStats]:
    stats = GuideStats()
    builder = GuideBuilder(channels, config)
    index = ChannelIndex(channels)
    start, end = guide_window(config)
    sources = {ch.source.name: ch.source for ch in channels if ch.source}
    log.info("Reading airings from %d database(s), %s", len(sources), describe_window(start, end))
    for source in sources.values():
        stats.databases += 1
        try:
            con = reader.open(source.db_path)
            added = sum(builder.add(ch, item, media) for ch, item, media in db_airings(con, index, start, end))
        except PlexError as exc:
            stats.failed_databases += 1
            log.warning("%s: %s", source.name, exc)
            continue
        log.info("%s: %d airings", source.name, added)
    return builder.render(stats), stats


def write_xmltv(root: ET.Element, out: str) -> None:
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    if out == "-":
        tree.write(sys.stdout.buffer, encoding="utf-8", xml_declaration=True)
        sys.stdout.buffer.write(b"\n")
        return
    path = Path(out)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".gz":
            with gzip.open(tmp, "wb") as fh:
                tree.write(fh, encoding="utf-8", xml_declaration=True)
        else:
            tree.write(tmp, encoding="utf-8", xml_declaration=True)
        os.replace(tmp, path)
    except OSError as exc:
        raise PlexError(f"cannot write {path}: {exc.strerror or exc}") from None
    finally:
        if tmp.exists():
            tmp.unlink()


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def print_channels(channels: list[Channel]) -> None:
    rows = [("xmltv_id", "number", "callsign", "title", "enabled", "plex_id")]
    for ch in channels:
        enabled = "" if ch.enabled is None else ("yes" if ch.enabled else "no")
        rows.append((ch.xmltv_id, ch.number, ch.call_sign, ch.title, enabled, ch.plex_id))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())


def print_dvrs(config: Config, reader: DatabaseReader) -> None:
    data_dir, sources = discover_db_sources(config)
    print(f"Plex data directory: {data_dir}")
    mappings = load_library_mappings(data_dir, reader)
    for source in sources:
        m = mappings.get(source.dvr_key)
        detail = f"  {m.lineup_title or m.lineup} ({m.devices} tuner(s), {len(m.number_by_key)} mapped channels)" if m else "  (no DVR record in the library database)"
        print(f"DVR {source.dvr_key}: {source.db_path}{detail}")


class ExitAfterBanner(argparse.Action):
    """--version: the banner with the version and URL has already been printed, so just exit."""

    def __call__(self, parser, namespace, values, option_string=None):
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="plexmltv",
        description="PleXMLTV: export the Plex Live TV guide from Plex's databases as XMLTV.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    src = p.add_argument_group("where to read the guide")
    src.add_argument("--data-dir", default=os.environ.get("PLEX_DATA_DIR") or None, help="Plex's 'Plex Media Server' data directory when it is not in a standard place (env PLEX_DATA_DIR)")
    src.add_argument("--db", default=None, help="a specific tv.plex.providers.epg.*.db file to read")
    src.add_argument("--dvr", default=None, help="only export this DVR, by uuid; see --list-dvrs")

    out = p.add_argument_group("what to write")
    out.add_argument("--out", "-o", default=os.environ.get("XMLTV_OUT", str(DEFAULT_OUT)), help="output file; .gz compresses, - writes to stdout (env XMLTV_OUT)")
    out.add_argument("--days", type=int, default=argparse.SUPPRESS, help="only export this many days starting today, 1-21 (env XMLTV_DAYS; default: everything Plex has, about 14 days)")
    out.add_argument("--past-days", type=int, default=None, help="with --days, also include this many days before today")
    out.add_argument("--id-mode", choices=ID_MODES, default="number", help="what to use as the XMLTV channel id")
    out.add_argument("--include-disabled", action="store_true", help="include channels you disabled in Plex")
    out.add_argument("--channels", default="", help="comma-separated channel numbers or call signs to export")
    out.add_argument("--language", default="en", help="lang attribute for titles and descriptions")

    p.add_argument("--list-dvrs", action="store_true", help="print DVRs and their guide databases, then exit")
    p.add_argument("--list-channels", action="store_true", help="print the channel table, then exit")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("-q", "--quiet", action="store_true", help="warnings only")
    p.add_argument("--version", action=ExitAfterBanner, nargs=0, help="print the banner, with the version and project URL, then exit")
    return p


def env_int(name: str, default: int | None) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise PlexError(f"{name} must be a whole number, not {raw!r}") from None


def config_from_args(args: argparse.Namespace) -> Config:
    days = getattr(args, "days", None)
    if days is None:
        days = env_int("XMLTV_DAYS", None)
    if days is not None and not 1 <= days <= 21:
        raise PlexError("--days must be between 1 and 21")
    return Config(
        data_dir=args.data_dir,
        db=args.db,
        out=args.out,
        days=days,
        past_days=None if args.past_days is None else max(0, args.past_days),
        id_mode=args.id_mode,
        dvr=args.dvr,
        include_disabled=args.include_disabled,
        channels=[c for c in args.channels.split(",") if c.strip()],
        language=args.language,
    )


def finish(root: ET.Element, stats: GuideStats, config: Config) -> int:
    if stats.programs == 0:
        log.error("No programs were found for any channel; nothing written")
        return 2
    write_xmltv(root, config.out)
    log.info(
        "Wrote %s: %d channels, %d programs%s",
        config.out,
        stats.channels,
        stats.programs,
        f" ({stats.failed_databases}/{stats.databases} databases unreadable)" if stats.failed_databases else "",
    )
    if stats.empty_channels:
        log.warning("No programs for: %s", ", ".join(stats.empty_channels))
    return 0


def run(config: Config, *, list_channels: bool = False, list_dvrs: bool = False) -> int:
    with DatabaseReader() as reader:
        if list_dvrs:
            print_dvrs(config, reader)
            return 0
        raw_channels, _ = load_db_channels(config, reader)
        channels = finish_channels(raw_channels, config)
        if list_channels:
            print_channels(channels)
            return 0
        root, stats = build_guide(config, channels, reader)
    return finish(root, stats, config)


def main(argv: list[str] | None = None) -> int:
    # Before parsing, so --help, --version and option errors show it too.
    print_banner()
    args = build_parser().parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    counter = ProblemCounter()
    logging.getLogger().addHandler(counter)
    try:
        config = config_from_args(args)
        rc = run(config, list_channels=args.list_channels, list_dvrs=args.list_dvrs)
    except PlexError as exc:
        log.error("%s", exc)
        return 1
    except OSError as exc:
        log.error("%s: %s", exc.strerror or "error", exc.filename or exc)
        return 1
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130
    finally:
        logging.getLogger().removeHandler(counter)
    if rc == 0:
        print(f"\n{counter.closing_line()}", file=sys.stderr, flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
