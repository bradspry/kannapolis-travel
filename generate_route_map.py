#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Generate an interactive route map of trains and flights from Kannapolis, NC.

Reads routes.json (produced by fetch_routes.py) and writes an HTML map
and/or a PNG. Run fetch_routes.py first to refresh the data.

Usage:
    python3 generate_route_map.py               # writes .html and .png
    python3 generate_route_map.py -o out.html   # custom output path(s)

HTML output needs only the standard library (Leaflet loads from a CDN).
PNG output needs matplotlib; contextily adds an optional basemap.
"""

import argparse
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Presentation settings (data itself lives in routes.json)
# ---------------------------------------------------------------------------
RAIL_COLORS = {"Carolinian": "#1f6feb", "Piedmont": "#0a9396",
               "Crescent": "#d1495b", "Silver Meteor": "#e09f3e"}
AIRLINE_COLORS = {"Avelo Airlines": "#7b2cbf", "Allegiant Air": "#f77f00"}
FALLBACK_COLORS = ["#2a9d8f", "#6a4c93", "#bc4749", "#577590", "#8d6e63"]


def build_payload(data: dict) -> dict:
    """Validate routes.json and shape it for both renderers."""
    stations = data["stations"]
    spare = iter(FALLBACK_COLORS * 4)  # colors for routes/airlines added upstream

    rail = []
    for r in data["rail"]:
        missing = [s for s in r["stops"] + r["termini"] if s not in stations]
        if missing:
            raise ValueError(f"{r['name']}: unknown stations {missing}")
        n, days = r["round_trips_per_day"], r["days_per_week"]
        freq = (f"{n} daily round trip{'s' if n != 1 else ''}" if days == 7
                else f"{n} round trip{'s' if n != 1 else ''}, {days} days a week")
        ends = " – ".join(stations[s]["name"] for s in r["termini"])
        if r["kind"] == "connection":
            via = stations[r["transfer"]]["name"]
            label, where = f"Amtrak {r['name']} (transfer at {via})", f"Transfer at {via}."
        else:
            label, where = f"Amtrak {r['name']}", "Stops at Kannapolis."
        rail.append({"label": label, "color": RAIL_COLORS.get(r["name"]) or next(spare),
                     "dashed": r["kind"] == "connection", "stops": r["stops"],
                     "note": f"{freq}, {ends}. {where}"})

    airlines = {}
    for f in data["flights"]:
        airlines.setdefault(f["airline"], AIRLINE_COLORS.get(f["airline"]) or next(spare))

    # Rail and flights may be fetched at different times (--flights-only).
    def src(word):
        return next((s for s in data["sources"] if word in s["name"]), {})
    gtfs, airport = src("GTFS"), src("Airport")
    footer = (f"Data: Amtrak GTFS {gtfs.get('version', '')} (fetched {gtfs.get('fetched', '?')[:10]}), "
              f"Concord-Padgett airport route list (fetched {airport.get('fetched', '?')[:10]}). "
              "Verify with Amtrak, Avelo and Allegiant before booking.")
    return {"origins": data["origins"], "stations": stations, "rail": rail,
            "airlines": airlines, "flights": data["flights"], "footer": footer}


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kannapolis Travel Routes</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<style>
  :root { --bg:#ffffff; --fg:#1b1f24; --muted:#5b6470; --panel:#ffffffee; --border:#d0d7de; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0d1117; --fg:#e6edf3; --muted:#9aa4af; --panel:#161b22ee; --border:#30363d; }
  }
  html, body { margin:0; height:100%; background:var(--bg); color:var(--fg);
               font:14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }
  #map { position:absolute; inset:0; }
  .panel { position:absolute; z-index:1000; top:12px; left:12px; max-width:300px;
           max-height:calc(100% - 24px); overflow:auto; background:var(--panel);
           border:1px solid var(--border); border-radius:10px; padding:12px 14px;
           box-shadow:0 4px 16px #0003; }
  .panel h1 { font-size:16px; margin:0 0 4px; }
  .panel p { margin:0 0 10px; color:var(--muted); font-size:12px; }
  .panel h2 { font-size:12px; text-transform:uppercase; letter-spacing:.06em;
              color:var(--muted); margin:12px 0 6px; }
  .row { display:flex; align-items:center; gap:8px; margin:4px 0; cursor:pointer; }
  .swatch { width:26px; height:0; border-top:4px solid; flex:none; }
  .swatch.dash { border-top-style:dashed; }
  .count { margin-left:auto; color:var(--muted); font-size:12px; }
  .key { font-size:12px; color:var(--muted); margin-top:10px; border-top:1px solid var(--border); padding-top:8px; }
  .leaflet-tooltip.code { font-weight:600; }
  @media (max-width: 600px) {
    .panel { top:auto; bottom:12px; left:16px; right:16px; max-width:none; max-height:40%; }
  }
</style>
</head>
<body>
<div id="map"></div>
<div class="panel" id="panel">
  <h1>Trains &amp; Planes from Kannapolis</h1>
  <p>Amtrak KAN &middot; Concord-Padgett (USA). Click lines and dots for details.</p>
  <h2>Rail</h2><div id="rail-list"></div>
  <h2>Air (nonstop from USA)</h2><div id="air-list"></div>
  <div class="key">
    Solid rail = boards at Kannapolis &middot; Dashed rail = transfer required<br>
    <span id="footer"></span>
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>
const DATA = __DATA__;

const map = L.map('map', { worldCopyJump: true }).setView([33.5, -80], 5);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap contributors', maxZoom: 19
}).addTo(map);
document.getElementById('footer').textContent = DATA.footer;

const allBounds = L.latLngBounds([]);
const ll = code => [DATA.stations[code].lat, DATA.stations[code].lon];

// Curved arc between two points (quadratic Bezier, bowed to one side)
function arc(a, b, bow = 0.18, n = 48) {
  const [lat1, lon1] = a, [lat2, lon2] = b;
  const mx = (lat1 + lat2) / 2, my = (lon1 + lon2) / 2;
  const dx = lat2 - lat1, dy = lon2 - lon1;
  const cx = mx - dy * bow, cy = my + dx * bow;   // control point, perpendicular offset
  const pts = [];
  for (let i = 0; i <= n; i++) {
    const t = i / n, u = 1 - t;
    pts.push([u*u*lat1 + 2*u*t*cx + t*t*lat2, u*u*lon1 + 2*u*t*cy + t*t*lon2]);
  }
  return pts;
}

// Legend row with a checkbox that shows/hides `layer`
function addToggleRow(container, label, color, dashed, count, layer) {
  const row = document.createElement('label');
  row.className = 'row';
  row.innerHTML = `<input type="checkbox" checked>
    <span class="swatch${dashed ? ' dash' : ''}" style="border-top-color:${color}"></span>
    <span>${label}</span><span class="count">${count}</span>`;
  row.querySelector('input').addEventListener('change', e => {
    e.target.checked ? layer.addTo(map) : map.removeLayer(layer);
  });
  container.appendChild(row);
}

// ---- Rail -----------------------------------------------------------------
const railList = document.getElementById('rail-list');
DATA.rail.forEach(route => {
  const group = L.layerGroup();
  L.polyline(route.stops.map(ll), {
    color: route.color, weight: 4, opacity: 0.85, dashArray: route.dashed ? '8 8' : null
  }).bindPopup(`<b>${route.label}</b><br>${route.note}<br><small>${route.stops.length} stops shown</small>`)
    .addTo(group);
  route.stops.forEach(s => {
    allBounds.extend(ll(s));
    L.circleMarker(ll(s), { radius: 4, color: route.color, fillColor: '#fff', fillOpacity: 1, weight: 2 })
      .bindTooltip(`${DATA.stations[s].name} (${s}) &mdash; ${route.label}`)
      .addTo(group);
  });
  group.addTo(map);
  addToggleRow(railList, route.label, route.color, route.dashed, route.stops.length, group);
});

// ---- Flights --------------------------------------------------------------
const airList = document.getElementById('air-list');
const usa = DATA.origins.USA;
Object.entries(DATA.airlines).forEach(([airline, color], i) => {
  const group = L.layerGroup();
  const flights = DATA.flights.filter(f => f.airline === airline);
  flights.forEach(f => {
    const dest = [f.lat, f.lon];
    const popup = `<b>${f.city} (${f.code})</b><br>${f.name}<br>${airline}<br><small>${f.note}</small>`;
    allBounds.extend(dest);
    // Bow airlines to opposite sides so overlapping Florida arcs stay distinct
    L.polyline(arc([usa.lat, usa.lon], dest, i % 2 ? -0.18 : 0.18), { color, weight: 2.5, opacity: 0.9 })
      .bindPopup(popup).addTo(group);
    L.circleMarker(dest, { radius: 6, color, fillColor: color, fillOpacity: 0.9, weight: 1 })
      .bindTooltip(f.code, { permanent: true, direction: 'right', className: 'code' })
      .bindPopup(popup).addTo(group);
  });
  group.addTo(map);
  addToggleRow(airList, airline, color, false, flights.length, group);
});

// ---- Origins --------------------------------------------------------------
Object.entries(DATA.origins).forEach(([code, o]) => {
  allBounds.extend([o.lat, o.lon]);
  L.circleMarker([o.lat, o.lon], { radius: 9, color: '#111', fillColor: '#ffd60a', fillOpacity: 1, weight: 2 })
    .bindTooltip(`<b>${code}</b>`, { permanent: true, direction: code === 'KAN' ? 'top' : 'bottom', className: 'code' })
    .bindPopup(`<b>${o.name}</b>`)
    .addTo(map);
});

map.fitBounds(allBounds, { padding: [30, 30] });
</script>
</body>
</html>
"""


def write_html(out: Path, payload: dict) -> None:
    """Write the interactive Leaflet map with route data inlined as JSON."""
    # Escape "</" so data can never close the <script> tag early.
    blob = json.dumps(payload, indent=1, ensure_ascii=False).replace("</", "<\\/")
    out.write_text(HTML_TEMPLATE.replace("__DATA__", blob), encoding="utf-8")


# ---------------------------------------------------------------------------
# PNG rendering (matplotlib + optional contextily basemap)
# ---------------------------------------------------------------------------
# Label placement by station/airport code.  Side = where the text sits
# relative to its dot: "r" right, "l" left, "t" top, "b" bottom.
# Unlisted stations are dots only; unlisted airports outside Florida get "r".
PNG_MAIN_STATION_LABELS = {
    "CLT": "l", "GRO": "t", "RGH": "b", "RMT": "r", "RVR": "r", "WAS": "r",
    "BAL": "l", "PHL": "r", "NYP": "b", "GRV": "b", "ATL": "t", "BHM": "t",
    "NOL": "r", "CHS": "r", "SAV": "r",
}
PNG_MAIN_AIRPORT_SIDES = {"HVN": "t", "ISP": "r"}
# Florida is drawn again, zoomed, in an inset; its airports are labeled there.
FLORIDA_EXTENT = (24.6, 31.0, -83.6, -79.4)   # lat_min, lat_max, lon_min, lon_max
PNG_INSET_STATION_LABELS = {"JAX": "l", "MIA": "b", "WPB": "r"}
PNG_INSET_AIRPORT_SIDES = {
    "SFB": "r", "MCO": "r", "LAL": "b", "TPA": "t",
    "PIE": "l", "PGD": "l", "RSW": "r", "FLL": "l",
}
_SIDE = {  # side -> (xytext offset in points, ha, va)
    "r": ((5, 0), "left", "center"), "l": ((-5, 0), "right", "center"),
    "t": ((0, 5), "center", "bottom"), "b": ((0, -5), "center", "top"),
}


def _mercator(lat: float, lon: float) -> tuple:
    """WGS84 lat/lon -> Web Mercator (EPSG:3857) x, y in metres."""
    import math
    r = 6378137.0
    x = math.radians(lon) * r
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * r
    return x, y


def _arc(a: tuple, b: tuple, bow: float = 0.18, n: int = 48) -> list:
    """Quadratic Bezier arc between two (lat, lon) points - mirrors the JS version."""
    (lat1, lon1), (lat2, lon2) = a, b
    mx, my = (lat1 + lat2) / 2, (lon1 + lon2) / 2
    dx, dy = lat2 - lat1, lon2 - lon1
    cx, cy = mx - dy * bow, my + dx * bow
    pts = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        pts.append((u * u * lat1 + 2 * u * t * cx + t * t * lat2,
                    u * u * lon1 + 2 * u * t * cy + t * t * lon2))
    return pts


def _in_florida(lat: float, lon: float) -> bool:
    """True if the point falls inside the Florida inset extent."""
    la0, la1, lo0, lo1 = FLORIDA_EXTENT
    return la0 <= lat <= la1 and lo0 <= lon <= lo1


def _draw_layers(ax, p: dict, station_labels: dict, airport_sides, scale: float = 1.0) -> list:
    """Draw rail, flights and origins onto `ax`. Returns legend handles.

    airport_sides: dict code->side to label only those airports, or None for
    the main map (PNG_MAIN_AIRPORT_SIDES, skipping airports in the inset).
    """
    import matplotlib.patheffects as pe
    from matplotlib.lines import Line2D

    halo = [pe.withStroke(linewidth=3, foreground="white")]
    stations = p["stations"]

    def label(x, y, text, side, **kw):
        off, ha, va = _SIDE[side]
        ax.annotate(text, (x, y), xytext=off, textcoords="offset points",
                    ha=ha, va=va, path_effects=halo, **kw)

    def xy(points):
        return zip(*(_mercator(lat, lon) for lat, lon in points))

    handles = []
    # Rail
    for route in p["rail"]:
        xs, ys = xy([(stations[s]["lat"], stations[s]["lon"]) for s in route["stops"]])
        ls = (0, (5, 3)) if route["dashed"] else "-"
        ax.plot(xs, ys, color=route["color"], lw=3.2 * scale, ls=ls, alpha=0.9,
                solid_capstyle="round", zorder=3)
        ax.scatter(xs, ys, s=16 * scale, facecolor="white", edgecolor=route["color"],
                   linewidth=1.3, zorder=4)
        handles.append(Line2D([], [], color=route["color"], lw=3.2, ls=ls, label=route["label"]))
    for code, side in station_labels.items():
        if code in stations:
            st = stations[code]
            x, y = _mercator(st["lat"], st["lon"])
            label(x, y, st["name"], side, fontsize=9 * scale, color="#222", zorder=6)

    # Flights
    usa = p["origins"]["USA"]
    for i, (airline, color) in enumerate(p["airlines"].items()):
        bow = -0.18 if i % 2 else 0.18
        for f in (f for f in p["flights"] if f["airline"] == airline):
            xs, ys = xy(_arc((usa["lat"], usa["lon"]), (f["lat"], f["lon"]), bow))
            ax.plot(xs, ys, color=color, lw=2 * scale, alpha=0.9, zorder=5)
            x, y = _mercator(f["lat"], f["lon"])
            ax.scatter([x], [y], s=60 * scale, color=color, alpha=0.95,
                       edgecolor="white", linewidth=1, zorder=7)
            if airport_sides is None:
                side = None if _in_florida(f["lat"], f["lon"]) \
                    else PNG_MAIN_AIRPORT_SIDES.get(f["code"], "r")
            else:
                side = airport_sides.get(f["code"])
            if side:
                label(x, y, f["code"], side, fontsize=10 * scale,
                      fontweight="bold", color=color, zorder=8)
        handles.append(Line2D([], [], color=color, lw=2, label=f"{airline} (from USA)"))

    # Origins: KAN and USA are ~13 km apart, so label them on opposite sides
    for code, o in p["origins"].items():
        x, y = _mercator(o["lat"], o["lon"])
        ax.scatter([x], [y], s=140 * scale, color="#ffd60a", edgecolor="#111",
                   linewidth=1.6, zorder=9)
        label(x, y, code, "t" if code == "KAN" else "b", fontsize=11 * scale,
              fontweight="bold", color="#111", zorder=10)
    return handles


def _set_extent(ax, lat0, lat1, lon0, lon1) -> None:
    """Clip `ax` to a lat/lon box (in Mercator) and hide the axis ticks."""
    x0, y0 = _mercator(lat0, lon0)
    x1, y1 = _mercator(lat1, lon1)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])


def _add_basemap(ax, attribution: bool) -> bool:
    """Add Esri gray tiles behind `ax`; fall back to a flat fill if unavailable."""
    try:
        import contextily as cx
        cx.add_basemap(ax, crs="EPSG:3857", source=cx.providers.Esri.WorldGrayCanvas,
                       attribution="Tiles © Esri — Esri, DeLorme, NAVTEQ" if attribution else False,
                       attribution_size=7)
        return True
    except Exception as exc:  # offline or contextily missing
        print(f"  (basemap skipped: {exc})")
        ax.set_facecolor("#eef3f7")
        return False


def write_png(out: Path, payload: dict, dpi: int = 200, basemap: bool = True) -> None:
    """Render a static map with a zoomed Florida inset."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    fig, ax = plt.subplots(figsize=(14, 14))

    # Main map
    handles = _draw_layers(ax, payload, PNG_MAIN_STATION_LABELS, airport_sides=None)
    _set_extent(ax, 17.0, 44.2, -92.5, -63.5)
    if basemap:
        _add_basemap(ax, attribution=True)

    # Florida inset (lower-left, over the Gulf of Mexico)
    inset = ax.inset_axes([0.01, 0.035, 0.36, 0.37])
    inset.set_anchor("SW")  # hug the lower-left corner despite equal aspect
    _draw_layers(inset, payload, PNG_INSET_STATION_LABELS, PNG_INSET_AIRPORT_SIDES, scale=0.9)
    _set_extent(inset, *FLORIDA_EXTENT)
    if basemap:
        _add_basemap(inset, attribution=False)
    inset.set_title("Florida detail", fontsize=11, fontweight="bold", loc="left")
    for spine in inset.spines.values():
        spine.set_edgecolor("#444")
        spine.set_linewidth(1.2)
    ax.indicate_inset_zoom(inset, edgecolor="#444", alpha=0.8)

    handles += [
        Line2D([], [], color="#555", lw=3, ls="-", label="Solid rail: board at Kannapolis"),
        Line2D([], [], color="#555", lw=3, ls=(0, (5, 3)), label="Dashed rail: transfer required"),
        Line2D([], [], marker="o", ls="", markersize=11, markerfacecolor="#ffd60a",
               markeredgecolor="#111", label="KAN = Amtrak  ·  USA = Concord airport"),
    ]
    ax.legend(handles=handles, loc="center right", fontsize=10, frameon=True,
              framealpha=0.95, title="Routes", title_fontsize=12)
    ax.set_title("Trains & Planes from Kannapolis, NC", fontsize=22,
                 fontweight="bold", loc="left", pad=12)
    ax.text(0.0, -0.015, payload["footer"], transform=ax.transAxes,
            fontsize=10, color="#555", va="top")

    fig.savefig(out, dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def main() -> None:
    """CLI entry point: write each requested output, picked by file extension."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("-d", "--data", default="routes.json",
                        help="route data from fetch_routes.py (default: routes.json)")
    parser.add_argument("-o", "--output", action="append",
                        help="output file; .html or .png (repeatable). "
                             "Default: kannapolis_routes.html and kannapolis_routes.png")
    parser.add_argument("--dpi", type=int, default=200, help="PNG resolution (default 200)")
    parser.add_argument("--no-basemap", action="store_true",
                        help="PNG without map tiles (works offline)")
    args = parser.parse_args()

    data_path = Path(args.data)
    if not data_path.exists():
        parser.error(f"{data_path} not found; run fetch_routes.py first")
    payload = build_payload(json.loads(data_path.read_text(encoding="utf-8")))

    outputs = [Path(p) for p in (args.output or
                                 ["kannapolis_routes.html", "kannapolis_routes.png"])]
    for out in outputs:
        ext = out.suffix.lower()
        if ext == ".html":
            write_html(out, payload)
        elif ext == ".png":
            write_png(out, payload, dpi=args.dpi, basemap=not args.no_basemap)
        else:
            parser.error(f"unsupported output type: {out} (use .html or .png)")
        print(f"Wrote {out.resolve()}")

    print(f"  {len(payload['rail'])} rail routes, {len(payload['stations'])} stations")
    print(f"  {len(payload['flights'])} nonstop flights across {len(payload['airlines'])} airlines")


if __name__ == "__main__":
    main()
