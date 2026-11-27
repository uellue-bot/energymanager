# Design

## Context

- The forecast archive (`archive/forecast_archive.py`) fetches each configured service once per hour (`attempted_this_hour`), keeps the response body unchanged in its `fetch` table with the fetch time and failures, and reads the site's location and roof plane from `.env` (`EM_LATITUDE`, `EM_LONGITUDE`, `EM_DECLINATION`, `EM_AZIMUTH`).
- The collector (`collector/collector.py`) reads `STATE_OBJECTS` once a minute over one short connection, stores each raw value in `state(read_at, object, value)`, records a failure when objects are missing, and reports the latest values on its local endpoint.
- Open-Meteo's satellite API, checked on 2026-11-23 with a query for neutral coordinates: endpoint `https://satellite-api.open-meteo.com/v1/archive`; `hourly=shortwave_radiation,direct_normal_irradiance,diffuse_radiation,global_tilted_irradiance` with `tilt`, `azimuth` and `past_days=1` returns hourly values; queries for 10- and 15-minute steps returned no data. The documentation names DWD's EUMETSAT MTG product for Europe, 10-minutely at the source, 20 minutes delay; hourly values are means of the preceding hour.
- The three inverter objects exist in `rctclient`'s registry (checked on the host 2026-11-23): `dc_conv.last_rescan` (0xF87A2A1E, "Last global rescan"), `dc_conv.dc_conv_struct[0].rescan_correction` (0xDB45ABD0, V), `dc_conv.dc_conv_struct[0].u_target` (0x226A23A4, "MPP on input A", V). The pilot uses input A only (input B is unused, principal 2026-11-22).

## Goals / Non-Goals

Goals: E13 and E14 as specified.

Non-goals:
- Using the data (module temperature, working-point flag, performance ratio): later steps of S6.
- Input B: unused on the pilot; added when a site uses it.
- Showing anything in Home Assistant.
- Sub-hourly irradiance: the query offers none (see Context); revisited if Open-Meteo adds it.

## Decisions

Each line gives who decided and whether the decision is reversible.

- **P1 The satellite data is a third service of the forecast archive**, `open-meteo-satellite`, with its own URL, the same hourly cadence, storage and failure handling. Agent's choice, reversible. Alternative: a separate service or table; rejected, because the archive already does exactly "fetch hourly, keep as fetched" and its readers select by service.
- **P2 Query: `past_days=1`, `forecast_days=0`, `timezone=UTC`, the four variables, `tilt` and `azimuth` from the same `.env` values as the weather forecast.** Each hour's fetch overlaps the previous 23; the overlap is kept (spec: kept as fetched) and lets late corrections show. About 3 kB per fetch, about 25 MB per year. Agent's choice, reversible.
- **P3 No issue time** for this service, like the Open-Meteo forecast (`no_issue_time`); the fetch time is the reference. Agent's choice, reversible.
- **P4 The three objects are added to the collector's state objects**, so they are read in the same connection, stored in the same table, and covered by the same failure handling and endpoint. Agent's choice, reversible. Alternative: a separate list with its own cadence; rejected until a reason for a different cadence exists.
- **P4a Only the five original objects count for the state's last success** (and so for the "state not read" alert); a NaN from one of the three new objects is a gap without a failure row. Whether the inverter answers them at night is unknown. Principal, 2026-11-23 ("sounds good"); reversible.
- **P2a The satellite query asks for the satellite product alone (`models=dwd_sis_europe_africa_v4`), yesterday and today (`forecast_days=1`).** The default model fills hours not yet observed with forecast values; this one leaves them null, and today's observed hours arrive within about an hour (checked 2026-11-23, neutral coordinates). The reader skips a newer response that has only nulls for an hour. Principal, 2026-11-23; reversible. Replaces `forecast_days=0` of P2.
- **P5 Raw values, no interpretation.** `dc_conv.last_rescan` is stored as the inverter returns it (an integer, presumably a UNIX timestamp); interpreting it belongs to the later step. Agent's choice, reversible.

## Risks / Trade-offs

- [Three more reads per minute disturb the Home Assistant integration] → The connection already carries five reads; watch the integration's unavailable count for a day after deployment (task 3.2).
- [The registry descriptions are wrong or the values meaningless] → Raw storage costs little; the later step checks them against the string voltage before relying on them.
- [Open-Meteo's satellite data is inaccurate at the site under broken clouds] → Expected; calibration uses clear-sky periods (research in issue #4). Kept as fetched, so a better source can replace it later without losing the comparison.
- [The satellite API's licence or limits do not allow this use] → One call per hour is well within the free tier's limits as stated for the forecast API; the licence is checked before deployment (task 3.1).

## Migration Plan

Deploy with `tools/deploy.sh` after the principal's OK. No schema change: both use existing tables. Rollback: deploy the previous main; the extra rows stay and do no harm.

## Open Questions

- What the three objects show during a rescan, and how often the inverter rescans: answered by the first sunny days after deployment.
