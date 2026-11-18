"""FotballLab Pro v2: automatisk oppdaterte odds + arbitrasje-skanner.

Kjør:   pip install streamlit pandas requests numpy
        streamlit run app.py
API-nøkkel: gratis på the-odds-api.com (sett ODDS_API_KEY eller lim inn i sidemenyen).
"""
import os
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

TZ = ZoneInfo("Europe/Oslo")
DB = os.getenv("FOTBALLLAB_DB", "fotballlab.db")
BASE = "https://api.the-odds-api.com/v4"

st.set_page_config(page_title="FotballLab Pro", page_icon="⚽", layout="wide")
st.title("⚽ FotballLab Pro")
st.caption("Live odds • Arbitrasje-skanner • Papirspill | Ingen ekte spill plasseres her")


# ---------- database ----------
def db():
    con = sqlite3.connect(DB)
    con.execute(
        """CREATE TABLE IF NOT EXISTS bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, match TEXT,
        bookmaker TEXT, selection TEXT, odds REAL, stake REAL,
        result TEXT DEFAULT 'pending')"""
    )
    return con


# ---------- API ----------
@st.cache_data(ttl=3600, show_spinner=False)
def list_leagues(key):
    r = requests.get(f"{BASE}/sports/", params={"apiKey": key}, timeout=15)
    r.raise_for_status()
    return {x["key"]: x["title"] for x in r.json()
            if x.get("active") and x["key"].startswith("soccer_")}


def fetch_odds(key, league_keys, regions, markets):
    """Henter odds. Ikke cachet her: auto-refresh styrer hyppigheten."""
    events, remaining = [], None
    for lk in league_keys:
        r = requests.get(
            f"{BASE}/sports/{lk}/odds/",
            params={"apiKey": key, "regions": regions, "markets": markets,
                    "oddsFormat": "decimal", "dateFormat": "iso"},
            timeout=20,
        )
        remaining = r.headers.get("x-requests-remaining", remaining)
        if r.ok:
            for e in r.json():
                e["_league"] = lk
                events.append(e)
        elif r.status_code in (401, 429):
            raise RuntimeError(f"API-feil {r.status_code}: {r.text[:150]}")
    return events, remaining


def to_prices(events):
    rows = []
    for e in events:
        ko = pd.to_datetime(e["commence_time"], utc=True).tz_convert(TZ)
        match = f"{e['home_team']} – {e['away_team']}"
        for b in e.get("bookmakers", []):
            for m in b.get("markets", []):
                for o in m.get("outcomes", []):
                    point = o.get("point")
                    rows.append(dict(
                        event_id=e["id"], kickoff=ko, league=e["_league"], match=match,
                        market=m["key"], point=point if point is not None else 0.0,
                        outcome=o["name"], bookmaker=b["title"], odds=o["price"],
                        updated=m.get("last_update") or b.get("last_update"),
                    ))
    return pd.DataFrame(rows)


# ---------- analyse ----------
def find_arbs(df, min_profit):
    """En arbitrasje finnes når summen av 1/beste_odds for alle utfall < 1."""
    out = []
    if df.empty:
        return pd.DataFrame()
    for (eid, market, point), g in df.groupby(["event_id", "market", "point"]):
        best = g.loc[g.groupby("outcome")["odds"].idxmax()]
        n_expected = 3 if market == "h2h" else 2
        # h2h uten uavgjort (andre sporter) hoppes over; her kreves komplett marked
        if len(best) != n_expected:
            continue
        inv = (1 / best["odds"]).sum()
        profit = (1 / inv - 1) * 100
        if profit >= min_profit:
            out.append(dict(
                kamp=best.iloc[0]["match"], avspark=best.iloc[0]["kickoff"],
                marked=market if market == "h2h" else f"{market} {point}",
                profitt_pct=round(profit, 2), inv=inv,
                legs=[(r.outcome, r.bookmaker, r.odds) for r in best.itertuples()],
            ))
    return pd.DataFrame(out).sort_values("profitt_pct", ascending=False) if out else pd.DataFrame()


def stakes(legs, total):
    inv = sum(1 / o for _, _, o in legs)
    return [(name, bk, o, round(total * (1 / o) / inv, 2)) for name, bk, o in legs]


# ---------- sidemeny ----------
with st.sidebar:
    st.header("Innstillinger")
    key = st.text_input("The Odds API-nøkkel", value=os.getenv("ODDS_API_KEY", ""), type="password")
    regions = st.multiselect("Regioner", ["eu", "uk", "us", "au"], default=["eu", "uk"])
    markets = st.multiselect("Markeder", ["h2h", "totals"], default=["h2h", "totals"])
    auto = st.toggle("Oppdater automatisk", value=True)
    interval = st.select_slider("Intervall (sekunder)", [60, 120, 300, 600], value=300,
                                help="Hver oppdatering koster API-kreditter per liga og marked.")
    min_profit = st.slider("Min. arbitrasje-profitt (%)", 0.0, 5.0, 0.5, 0.1)
    only_today = st.checkbox("Bare kamper som starter innen 24 t", value=True)

if not key:
    st.info("Legg inn API-nøkkelen din i sidemenyen for å starte.")
    st.stop()

try:
    leagues = list_leagues(key)
except Exception as exc:
    st.error(f"Kunne ikke hente ligaer: {exc}")
    st.stop()

with st.sidebar:
    chosen = st.multiselect("Ligaer", list(leagues), format_func=lambda k: leagues[k],
                            default=[k for k in ("soccer_norway_eliteserien", "soccer_epl") if k in leagues])

if not chosen or not regions or not markets:
    st.info("Velg minst én liga, region og marked.")
    st.stop()


# ---------- selve visningen (kjører på nytt automatisk) ----------
@st.fragment(run_every=interval if auto else None)
def live_view():
    try:
        events, remaining = fetch_odds(key, chosen, ",".join(regions), ",".join(markets))
    except Exception as exc:
        st.error(str(exc))
        return
    df = to_prices(events)
    if not df.empty and only_today:
        limit = pd.Timestamp.now(TZ) + pd.Timedelta(hours=24)
        df = df[(df.kickoff >= pd.Timestamp.now(TZ)) & (df.kickoff <= limit)]

    c1, c2, c3 = st.columns(3)
    c1.metric("Sist oppdatert", datetime.now(TZ).strftime("%H:%M:%S"))
    c2.metric("Kamper", df.event_id.nunique() if not df.empty else 0)
    c3.metric("API-kreditter igjen", remaining or "?")

    t1, t2, t3 = st.tabs(["💰 Arbitrasje", "📊 Beste odds", "📒 Papirspill"])

    with t1:
        st.warning(
            "Arbitrasje er den eneste typen spill som matematisk kan gi garantert profitt, "
            "men marginene er små, forsvinner på sekunder, og odds kan være utdaterte. "
            "Bookmakere kan annullere spill eller begrense kontoer. Ingen spill er helt «safe»."
        )
        arbs = find_arbs(df, min_profit) if not df.empty else pd.DataFrame()
        if arbs.empty:
            st.info(f"Ingen arbitrasjer ≥ {min_profit}% akkurat nå. Det er normalt: de er sjeldne.")
        else:
            total = st.number_input("Total fiktiv innsats", 10.0, 100000.0, 1000.0, 10.0)
            for i, a in arbs.iterrows():
                with st.expander(f"{a.profitt_pct:+.2f}% | {a.kamp} | {a.marked} | "
                                 f"{a.avspark:%d.%m %H:%M}"):
                    sp = pd.DataFrame(stakes(a.legs, total),
                                      columns=["Utfall", "Bookmaker", "Odds", "Innsats"])
                    st.dataframe(sp, hide_index=True)
                    st.caption(f"Utbetaling uansett utfall ≈ {total / a.inv:.2f}")
                    if st.button("Lagre som papirspill", key=f"save{i}"):
                        with db() as con:
                            for name, bk, o, s in stakes(a.legs, total):
                                con.execute(
                                    "INSERT INTO bets(created,match,bookmaker,selection,odds,stake) "
                                    "VALUES(?,?,?,?,?,?)",
                                    (datetime.now(TZ).isoformat(), a.kamp, bk, f"{a.marked}: {name}", o, s))
                        st.success("Lagret.")

    with t2:
        if df.empty:
            st.info("Ingen kamper funnet.")
        else:
            best = df.loc[df.groupby(["match", "market", "point", "outcome"])["odds"].idxmax()]
            st.dataframe(best[["kickoff", "match", "market", "point", "outcome", "bookmaker", "odds"]]
                         .sort_values(["kickoff", "match"]), hide_index=True, use_container_width=True)

    with t3:
        with db() as con:
            bets = pd.read_sql_query("SELECT * FROM bets ORDER BY id DESC", con)
        if bets.empty:
            st.info("Ingen papirspill ennå.")
        else:
            st.dataframe(bets, hide_index=True, use_container_width=True)
            bid = st.selectbox("Spill", bets.id.tolist())
            res = st.selectbox("Resultat", ["pending", "win", "loss", "void"])
            if st.button("Lagre resultat"):
                with db() as con:
                    con.execute("UPDATE bets SET result=? WHERE id=?", (res, int(bid)))
                st.rerun()


live_view()
st.divider()
st.caption("Kun til læring og papirspill. Gambling kan gi tap. Hjelp: Hjelpelinjen for spilleavhengige 800 800 40.")
