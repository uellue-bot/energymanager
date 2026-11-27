# Proposal

## Why

PV array health (requirements S6, issue uellue-bot/energymanager#4) needs two inputs the record lacks: measured irradiance at the site, and the inverter's own account of when it rescans for the maximum power point. Without the first, module temperature and expected power can only be computed from forecasts; without the second, a rescan looks like shading. Both are lost for every day they are not recorded: the inverter keeps no history of them, and Open-Meteo's satellite archive covers only the past.

Persona: P1, the pilot operator (design/stakeholders.md).

The principal asked for exactly these two ("1 sounds good! 2 Yes", issue #4, 2026-11-23, and "73 yes please go ahead!" in chat). They are the whole request for this step; module temperature, the working-point flag and the performance ratio are later steps. The form (an extra service in the forecast archive, three more objects in the collector) is the agent's proposal.

## What Changes

- The forecast archive also fetches Open-Meteo's satellite radiation for the site, once per hour, for the past day, and keeps each response as issued, like the forecasts (E13). Variables: global horizontal, direct normal and diffuse irradiance, and global tilted irradiance for the roof plane already configured for the weather forecast. Hourly values, each the mean of the preceding hour (the API offers no finer steps for this query; checked 2026-11-23).
- The collector also reads three inverter objects every minute, read only, and keeps their raw values like the existing state objects (E14):
  - `dc_conv.last_rescan`: time of the last global MPP rescan;
  - `dc_conv.dc_conv_struct[0].rescan_correction`: the MPP voltage correction of that rescan on input A, in V;
  - `dc_conv.dc_conv_struct[0].u_target`: the MPP voltage target on input A, in V.

Behaviour towards the user: none visible; the data is recorded for later steps. Nothing is written to devices.

## Capabilities

### New Capabilities
- `satellite-radiation-archive`: measured (satellite-derived) irradiance for the site, kept hourly as issued.
- `inverter-mpp-record`: the inverter's MPP target and rescan objects for input A, recorded every minute, raw.

### Modified Capabilities
(none: the related capabilities `forecast-archive` and `inverter-state-record` live in changes that are not archived yet, so `openspec/specs/` is empty. Their behaviour does not change.)

## Must-examples and premises

- Examples served: E13 and E14 (should, added with this change). No must-example; none without a scenario.
- Premises nobody has confirmed:
  - The three objects mean what their registry descriptions say ("Last global rescan", "Last global rescan MPP correction on input A", "MPP on input A"); not checked on the device.
  - A rescan shows as a change of `dc_conv.last_rescan` within a minute; how often the inverter rescans is not known.
  - Open-Meteo's satellite data for the site is accurate enough for health checks; its accuracy and licence terms are not checked.
  - Reading three more objects per minute stays within what the inverter tolerates next to the Home Assistant integration (the collector already reads five).

## Impact

- `archive/forecast_archive.py`: one more service, same storage; tests in `tests/`.
- `collector/collector.py`: three more names in the state objects; tests.
- `design/examples.md`: E13, E14.
- No new dependencies. Deployed with `tools/deploy.sh` after the principal's OK.
