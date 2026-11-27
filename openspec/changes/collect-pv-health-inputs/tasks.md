# Tasks

## 1. Satellite irradiance (E13)

- [x] 1.1 Add the service `open-meteo-satellite` to the archive's services (P1–P3): URL from the `.env` location and roof plane, no issue time. Unit tests with a recorded-shape response (synthetic values, neutral coordinates) cover a normal fetch kept unchanged, a failed fetch recorded while the forecasts are still fetched, and one call per hour after restarts. Verify: tests cite E13; `python3 -m unittest` passes.
- [x] 1.2 A reader function for the satellite irradiance of a given hour from the latest response covering it (standard library only), with a test where a later response revises an hour. Verify: test cites E13.

## 2. Inverter MPP objects (E14)

- [x] 2.1 Add the three objects to the collector's state objects (P4, P5). Extend the existing fake-inverter tests: all eight objects stored per minute; a missing object recorded as a failure as before; no write frame sent. Verify: tests cite E14 and E2; the write-guard test still passes.

## 3. Checks before and after deployment

- [x] 3.1 Read Open-Meteo's terms for the satellite API and note in `design/research/pv-battery-model.md` whether one call per hour for a private site is allowed and what attribution it needs. Verify: the note cites the page and date.
- [ ] 3.2 After the principal's OK, deploy with `tools/deploy.sh`. After 24 h on the automation PC: 24 satellite fetches or recorded failures; the three objects stored every minute; the Home Assistant integration's unavailable count compared with the 24 h before. Verify: the numbers in the hand-over.

## 4. Review and documentation

- [x] 4.1 README: the archive's and the collector's sections name the new data. Verify: the private-data check passes; read as someone who has not seen this change.
- [x] 4.2 Independent review of the changes to the collector and the archive (they read devices and write data on their own; standing instructions). Verify: findings fixed or listed in the hand-over.

## 5. Hallway test

- [ ] 5.1 Hallway test (kaizen hallway-test) for E13 and E14: a fresh agent with only the README, the requirements and the examples finds the satellite irradiance of one hour and the last rescan time in the stores and reads them from Python. Deferred like 6.1 of `collect-device-details` unless the principal asks for it. Verify: a record in `design/hallway/`, findings fixed or filed.
