<div align="center">
<img src="assets/logo.webp" alt="PleXMLTV: a TV guide grid turning into an XMLTV file" width="75%">

**Plex's TV guide data, exported as standard XMLTV**

<p>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0--only-1e5aa8?style=flat-square" alt="license: AGPL-3.0-only"></a>
<img src="https://img.shields.io/badge/Python-%E2%89%A5%203.10-3776AB?logo=python&logoColor=white&style=flat-square" alt="Python 3.10 or newer">
<img src="https://img.shields.io/badge/Plex-Plex%20Pass%20required-E5A00D?logo=plex&logoColor=white&style=flat-square" alt="Plex: Plex Pass required">
<img src="https://img.shields.io/badge/XMLTV-DTD%20compliant-6b4fbb?style=flat-square" alt="XMLTV: DTD compliant">
</p>

<p>
<a href="https://ko-fi.com/bretteroo"><img src="https://img.shields.io/badge/-Support%20PleXMLTV-13C3FF?style=flat-square&labelColor=555555&logo=data:image/svg%2Bxml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZmlsbD0iI2ZmZmZmZiIgZD0iTTEyIDIxcy03LjQtNC41LTkuNC05LjFDMS4xIDguMyAzLjMgNC42IDcgNC42YzIgMCAzLjYgMS4xIDUgMi45IDEuNC0xLjggMy0yLjkgNS0yLjkgMy43IDAgNS45IDMuNyA0LjQgNy4zQzE5LjQgMTYuNSAxMiAyMSAxMiAyMXoiLz48L3N2Zz4=" alt="Support PleXMLTV"></a>
</p>

---

_Because good data deserves open standards_

[🧭 How it works](#-how-it-works) | [🚀 One-time setup](#-one-time-setup) | [📺 Usage](#-usage) | [🔧 Troubleshooting](#-troubleshooting) | [🔒 License](#-license)

</div>

---

# PleXMLTV

PleXMLTV is a Python script that pulls the guide data _you're already paying for_ out of Plex's SQLite databases and converts it to standard XMLTV format.

This script will only work for Plex Pass subscribers.

If you don't want to pay _anyone_ for guide data, there are [other projects out there](https://github.com/shuaiscott/zap2xml) that will do what you want.

---

## 🧭 How it works

PleXMLTV is a single, standalone Python script that runs on your Plex server. It needs Python 3.10 or newer.

It finds Plex's data directory on its own, extracts what it needs out of Plex's guide data, and outputs it to an XML file that is fully compliant with the [XMLTV DTD specification](https://github.com/XMLTV/xmltv/blob/master/xmltv.dtd).

The script runs entirely locally.  It doesn't send or receive any data over the network.

## 🚀 One-time setup

### Grab the script

```
curl -fLO https://raw.githubusercontent.com/Bretteroo/PleXMLTV/main/plexmltv.py
chmod +x plexmltv.py
```

### Configure Plex
You'll first need to manually select which channels you want to appear in your exported guide data.

- Visit your Plex server's settings, then click to the _Live TV & DVR_ page under "Manage".
- Under the "Channel Sources" column, click the "X enabled" link.
- Enable each tuner channel that you'd like to appear in the exported guide data and select its corresponding EPG channel in the drop-down alongside it.

## 📺 Usage
It could not be simpler.

```
./plexmltv.py                   # writes xmltv.xml beside the script.  one shot.  done.
```

Your Plex server stores approximately 14 days of guide data.  By default, this script exports all of it.

### Options:

It's unlikely you'll ever need to use these, but you never know.

```
--list-dvrs          see which DVRs and guide databases exist
--list-channels      see what will be exported, with the ids used
--data-dir DIR       Plex's "Plex Media Server" directory when it is not in a standard place (env PLEX_DATA_DIR)
--db FILE            a specific tv.plex.providers.epg.*.db to read
--dvr UUID           only export this DVR when Plex has several
--out FILE, -o FILE  output path; .gz compresses, - writes to stdout (env XMLTV_OUT, default xmltv.xml beside the script)
--days N             only export N days starting today, 1 to 21 (env XMLTV_DAYS; default is everything Plex has, about 14 days)
--past-days N        with --days, also include N days before today
--id-mode MODE       what to use as the XMLTV channel id: number (default), callsign, plex
--include-disabled   include channels you disabled in Plex, if Plex still has data for them
--channels LIST      comma-separated channel numbers or call signs to export
--language CODE      lang attribute for titles and descriptions (default en)
--version            print the version and project URL, then exit
-v / -q              debug logging / warnings only
```

### Keeping it current

Plex refreshes its guide daily, so you may want to run the exporter on a schedule.

You can do this with a simple `cron` entry or with something needlessly elaborate like a `systemd` service.

## 🔧 Troubleshooting

- "no Plex data directory found; pass --data-dir or --db"
  - The script is not running on the Plex server, or Plex keeps its data somewhere unusual. Feed it `--data-dir`.

- "cannot open ... Permission denied"
  - You are not running as the Plex user.

- "No programs were found for any channel; nothing written"
  - Usually means Plex itself has no guide data yet. Check the guide in the Plex app - if it's empty there too, use "Refresh Guide" in Plex's DVR settings.

- Channels are missing from the output file
  - Plex doesn't store guide data for disabled channels (why would it?) so there's nothing to export until you enable them and the guide refreshes.  See "Configure Plex" above.

## 🔒 License

GNU Affero General Public License, version 3 only. See [LICENSE](LICENSE).
