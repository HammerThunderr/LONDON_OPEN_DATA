#!/usr/bin/env python3
"""
cities.py — the multi-city registry.

Each city the app supports is defined here by the ONS Combined Authority code
(CAUTH) that groups its local-authority districts, OR (for London) by a code
prefix. geography.py uses this to build one spine covering every city; the app
reads the city list and lets the user pick.

Adding a city = add one entry here. The national scrapers (council tax,
schools, rent, air quality, density) need no change — they already cover the
whole country and filter to whatever codes the spine contains. Crime is the
exception (police-force specific); see crime.py.

CAUTH codes (ONS LAD->CAUTH lookup):
  E47000001 Greater Manchester    E47000003 West Yorkshire
  E47000007 West Midlands         E47000004 Liverpool City Region
  E47000008 South Yorkshire       E47000010 North East (incl. Tyne & Wear)
London has no CAUTH in that lookup; its boroughs are the E09 prefix.
"""

from __future__ import annotations

# id: stable key used in JSON + app. Order = display order in the picker.
CITIES = [
    {
        "id": "london",
        "name": "London",
        "match": {"type": "prefix", "value": "E09"},
        "police_force": "metropolitan",   # + City of London handled in crime.py
        "has_ptal": True,
    },
    {
        "id": "greater_manchester",
        "name": "Greater Manchester",
        "match": {"type": "cauth", "value": "E47000001"},
        "police_force": "greater-manchester",
        "has_ptal": False,
    },
    {
        "id": "west_midlands",
        "name": "West Midlands",
        "match": {"type": "cauth", "value": "E47000007"},
        "police_force": "west-midlands",
        "has_ptal": False,
    },
    {
        "id": "west_yorkshire",
        "name": "West Yorkshire",
        "match": {"type": "cauth", "value": "E47000003"},
        "police_force": "west-yorkshire",
        "has_ptal": False,
    },
]

CITY_BY_ID = {c["id"]: c for c in CITIES}
