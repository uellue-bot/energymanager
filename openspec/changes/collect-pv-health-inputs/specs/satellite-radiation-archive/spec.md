# Spec Delta

## Purpose

Keeps measured, satellite-derived irradiance for the site, hour by hour and exactly as fetched, so that the PV array's expected power and module temperature can be computed from what the sun actually did rather than from a forecast (requirements S6).

## ADDED Requirements

### Requirement: Hourly fetch of the past day
The archive SHALL fetch satellite-derived irradiance for the site once per hour, covering at least the past 24 hours: global horizontal, direct normal and diffuse irradiance, and global tilted irradiance for the site's configured roof plane. It SHALL run independently of Home Assistant, like the forecast fetches.

#### Scenario: A day of fetches
- **WHEN** the archive runs for 24 h with the service reachable (E13)
- **THEN** it holds 24 satellite responses, and together they give an irradiance value for every hour of that day for each of the four quantities, or mark the hour as missing where the service had no value

### Requirement: Kept as fetched
The archive SHALL keep each satellite response unchanged, with the time it was fetched, in the same store as the forecasts and distinguishable from them. It SHALL never overwrite or delete a kept response.

#### Scenario: A later fetch revises an hour (assumed)
- **WHEN** the service returns a different value for an hour it had already reported
- **THEN** both responses are kept, each with its fetch time, so that either can be read back

### Requirement: Failed fetch recorded
When a satellite fetch fails, the archive SHALL record the failure with its time and reason, keep running, and try again at the next hour, without affecting the forecast fetches.

#### Scenario: Satellite service down, forecasts fine (assumed)
- **WHEN** the satellite service is unreachable at one fetch and the forecast services are reachable
- **THEN** that hour has a failure entry for the satellite service and the forecasts of that hour are kept as usual

### Requirement: Readable from Python
The kept satellite responses SHALL be readable from Python with the standard library only, selected by service and fetch time, like the forecasts.

#### Scenario: Read the irradiance of one hour (assumed)
- **WHEN** a script asks for the satellite irradiance of a given hour
- **THEN** it gets the four values from the latest response that covers that hour, with that response's fetch time
