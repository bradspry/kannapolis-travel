#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Fetch current train and flight routes from Kannapolis, NC into routes.json.

Sources:
  Rail     Amtrak GTFS schedule feed (stops, trips, service calendar)
  Air      Concord-Padgett Regional Airport "Airlines" page (current routes)
  Airports OurAirports open data (airport names and coordinates)

Avelo and Allegiant block automated requests, so the airport's own route
list is used instead; it links to each airline for booking.

Usage:
    python3 fetch_routes.py                # writes routes.json
    python3 fetch_routes.py -o other.json
    python3 fetch_routes.py --flights-only # refresh flights, keep rail data

Standard library only.
"""

import argparse
import csv
import datetime as dt
import html
import io
import json
import re
import statistics
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

AMTRAK_GTFS_URL = "https://content.amtrak.com/content/gtfs/GTFS.zip"
AIRPORT_URL = "https://concordairportnc.com/Airline-Information/Airlines"
OURAIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
USER_AGENT = "kannapolis-route-map/1.0"

ORIGIN_STATION = "KAN"   # Amtrak code for Kannapolis
ORIGIN_AIRPORT = "USA"   # IATA code for Concord-Padgett
# Routes reached by transferring from a train that stops at Kannapolis.
CONNECTING_ROUTES = ["Crescent", "Silver Meteor"]
# Airline website domain (as printed on the airport page) -> airline name.
AIRLINES = {"allegiantair.com": "Allegiant Air", "aveloair.com": "Avelo Airlines"}

# GTFS stop names are long ("Raleigh Union Station"); trim to the city.
NAME_OVERRIDES = {"NYP": "New York", "RVR": "Richmond", "BWI": "BWI Airport",
                  "DLD": "DeLand"}
NAME_SUFFIXES = (" Amtrak Station", " Amtrak", " Union Passenger Terminal",
                 " Union Station", " Penn Station")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read()


def station_name(stop_id: str, raw: str) -> str:
    if stop_id in NAME_OVERRIDES:
        return NAME_OVERRIDES[stop_id]
    for suffix in NAME_SUFFIXES:
        raw = raw.replace(suffix, "")
    return raw.strip()


# ---------------------------------------------------------------------------
# Rail (Amtrak GTFS)
# ---------------------------------------------------------------------------
class Gtfs:
    """Minimal in-memory reader for the GTFS tables we need."""

    def __init__(self, data: bytes):
        self.zip = zipfile.ZipFile(io.BytesIO(data))
        self.feed = self.table("feed_info.txt")[0]
        self.routes = {r["route_id"]: r for r in self.table("routes.txt")}
        self.stops = {s["stop_id"]: s for s in self.table("stops.txt")}
        self.trips = self.table("trips.txt")
        self.calendar = {c["service_id"]: c for c in self.table("calendar.txt")}
        self.exceptions = defaultdict(dict)  # service_id -> {date: added?}
        for e in self.table("calendar_dates.txt"):
            self.exceptions[e["service_id"]][e["date"]] = e["exception_type"] == "1"
        seqs = defaultdict(list)
        for st in self.table("stop_times.txt"):
            seqs[st["trip_id"]].append((int(st["stop_sequence"]), st["stop_id"]))
        self.trip_stops = {t: [s for _, s in sorted(v)] for t, v in seqs.items()}

    def table(self, name: str) -> list:
        if name not in self.zip.namelist():
            return []
        with self.zip.open(name) as f:
            return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))

    def runs_on(self, service_id: str, day: dt.date) -> bool:
        key = day.strftime("%Y%m%d")
        if key in self.exceptions[service_id]:
            return self.exceptions[service_id][key]
        c = self.calendar.get(service_id)
        return bool(c) and c["start_date"] <= key <= c["end_date"] \
            and c[day.strftime("%A").lower()] == "1"

    def route_trips(self, name: str) -> list:
        return [t for t in self.trips
                if self.routes[t["route_id"]]["route_long_name"] == name]

    def frequency(self, trips: list) -> tuple:
        """(round trips on a typical running day, days per week) over the feed's first week."""
        start = dt.datetime.strptime(self.feed["feed_start_date"], "%Y%m%d").date()
        per_day = []
        for i in range(7):
            day = start + dt.timedelta(days=i)
            dirs = defaultdict(int)
            for t in trips:
                if self.runs_on(t["service_id"], day):
                    dirs[t["direction_id"]] += 1
            per_day.append(min(dirs.get("0", 0), dirs.get("1", 0)))
        running = [n for n in per_day if n]
        return (round(statistics.median(running)) if running else 0), len(running)


def connection_segment(stops: list, served: set) -> list:
    """Longest stretch of `stops` not served by a direct train, starting at the
    transfer station where it leaves the direct network."""
    best, run = [], []
    for i, s in enumerate(stops + [None]):
        if s is not None and s not in served:
            run.append(i)
            continue
        if run:
            lo, hi = run[0], run[-1]
            seg = stops[lo:hi + 1]
            if hi + 1 < len(stops):        # junction after the run
                seg = list(reversed(stops[lo:hi + 2]))
            elif lo > 0:                   # junction before the run
                seg = stops[lo - 1:hi + 1]
            if len(seg) > len(best):
                best = seg
            run = []
    return best


def fetch_rail() -> dict:
    print(f"Fetching {AMTRAK_GTFS_URL}")
    g = Gtfs(fetch(AMTRAK_GTFS_URL))
    routes, used = [], set()

    def describe(name, trips, kind, stops, transfer=None):
        longest = max((g.trip_stops[t["trip_id"]] for t in trips), key=len)
        per_day, days = g.frequency(trips)
        routes.append({
            "name": name, "kind": kind, "transfer": transfer, "stops": stops,
            "termini": [longest[0], longest[-1]],
            "round_trips_per_day": per_day, "days_per_week": days,
            "url": g.routes[trips[0]["route_id"]]["route_url"],
        })
        used.update(stops)
        used.update(longest[::len(longest) - 1])

    # Direct: any route with a trip stopping at the origin station.
    direct = sorted({g.routes[t["route_id"]]["route_long_name"] for t in g.trips
                     if ORIGIN_STATION in g.trip_stops.get(t["trip_id"], [])})
    served = set()
    for name in direct:
        trips = g.route_trips(name)
        stops = max((g.trip_stops[t["trip_id"]] for t in trips), key=len)
        served.update(stops)
        describe(name, trips, "direct", stops)

    # Connections: draw only the part beyond the direct network.
    for name in CONNECTING_ROUTES:
        trips = g.route_trips(name)
        if not trips:
            raise SystemExit(f"Route not found in GTFS: {name}")
        full = max((g.trip_stops[t["trip_id"]] for t in trips), key=len)
        seg = connection_segment(full, served)
        if not seg or seg[0] not in served:
            raise SystemExit(f"{name} does not connect to the direct routes")
        describe(name, trips, "connection", seg, transfer=seg[0])

    stations = {s: {"name": station_name(s, g.stops[s]["stop_name"]),
                    "lat": float(g.stops[s]["stop_lat"]),
                    "lon": float(g.stops[s]["stop_lon"])} for s in sorted(used)}
    source = {"name": "Amtrak GTFS", "url": AMTRAK_GTFS_URL,
              "version": g.feed.get("feed_version", ""), "fetched": now()}
    return {"stations": stations, "routes": routes, "source": source}


# ---------------------------------------------------------------------------
# Air (airport route list + OurAirports coordinates)
# ---------------------------------------------------------------------------
def parse_airport_page(page: str) -> list:
    """Extract (airline, IATA code, status note) from the airport's Airlines page.

    The page has one <hr>-separated block per airline, each with a
    "Current Routes:" list and optional "New Routes (Month Year)" lists.
    """
    content = page.split('id="dnn_ContentPane"', 1)[-1]
    found = []
    for block in re.split(r"<hr\s*/?>", content):
        text = html.unescape(re.sub(r"<[^>]+>", " ", block))
        airline = next((n for d, n in AIRLINES.items() if d in text), None)
        if not airline:
            continue
        note = "Current route"
        for m in re.finditer(r"(Current Routes|New Routes[^:\n]*?\))|\(\s*([A-Z]{3})\s*\)", text):
            if m.group(1):
                note = "Current route" if m.group(1).startswith("Current") else m.group(1).strip()
            else:
                found.append((airline, m.group(2), note))
    missing = set(AIRLINES.values()) - {a for a, _, _ in found}
    if missing:
        raise SystemExit(f"No routes parsed for {sorted(missing)}; page layout may have changed")
    return found


def fetch_air() -> dict:
    print(f"Fetching {AIRPORT_URL}")
    routes = parse_airport_page(fetch(AIRPORT_URL).decode("utf-8", "replace"))
    print(f"Fetching {OURAIRPORTS_URL}")
    rows = csv.DictReader(io.StringIO(fetch(OURAIRPORTS_URL).decode("utf-8")))
    wanted = {code for _, code, _ in routes} | {ORIGIN_AIRPORT}
    airports = {}
    for r in rows:
        if r["iata_code"] in wanted:
            region = r["iso_region"].split("-")[1] if r["iso_country"] == "US" else r["iso_country"]
            airports[r["iata_code"]] = {"name": r["name"], "city": f"{r['municipality']}, {region}",
                                        "lat": float(r["latitude_deg"]), "lon": float(r["longitude_deg"])}
    unknown = wanted - airports.keys()
    if unknown:
        raise SystemExit(f"Airports not in OurAirports: {sorted(unknown)}")

    flights = [{"airline": a, "code": c, "note": n, **airports[c]} for a, c, n in routes]
    sources = [{"name": "Concord-Padgett Regional Airport", "url": AIRPORT_URL, "fetched": now()},
               {"name": "OurAirports", "url": OURAIRPORTS_URL, "fetched": now()}]
    return {"origin": airports[ORIGIN_AIRPORT], "flights": flights, "sources": sources}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("-o", "--output", default="routes.json", help="default: routes.json")
    parser.add_argument("--flights-only", action="store_true",
                        help="refresh flights only; keep rail data from the existing output file")
    args = parser.parse_args()
    out = Path(args.output)

    if args.flights_only:
        # Reuse rail data from the previous run instead of re-downloading GTFS.
        if not out.exists():
            parser.error(f"--flights-only needs an existing {out}; run without it first")
        old = json.loads(out.read_text(encoding="utf-8"))
        gtfs = next((s for s in old.get("sources", []) if "GTFS" in s["name"]), None)
        if not gtfs or ORIGIN_STATION not in old.get("stations", {}):
            parser.error(f"{out} has no rail data to reuse; run without --flights-only")
        rail = {"stations": old["stations"], "routes": old["rail"], "source": gtfs}
        print(f"Keeping rail data from {out} (fetched {gtfs.get('fetched', 'unknown')})")
    else:
        rail = fetch_rail()
    air = fetch_air()

    data = {
        "sources": [rail["source"]] + air["sources"],
        "origins": {
            ORIGIN_STATION: {"name": f"{rail['stations'][ORIGIN_STATION]['name']} Amtrak ({ORIGIN_STATION})",
                             **{k: rail["stations"][ORIGIN_STATION][k] for k in ("lat", "lon")}},
            ORIGIN_AIRPORT: {"name": f"{air['origin']['name']} ({ORIGIN_AIRPORT})",
                             "lat": air["origin"]["lat"], "lon": air["origin"]["lon"]},
        },
        "stations": rail["stations"],
        "rail": rail["routes"],
        "flights": air["flights"],
    }
    out.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {out.resolve()}")
    print(f"  {len(data['rail'])} rail routes, {len(data['stations'])} stations")
    print(f"  {len(data['flights'])} nonstop flights")


if __name__ == "__main__":
    main()
