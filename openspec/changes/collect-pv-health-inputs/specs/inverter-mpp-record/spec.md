# Spec Delta

## Purpose

Records the inverter's maximum-power-point target and its global rescans for the PV input, raw, so that a rescan can be told apart from shading or a defect when the array's working point is checked (requirements S6).

## ADDED Requirements

### Requirement: MPP objects recorded every minute
The collector SHALL read the inverter's time of the last global MPP rescan (`dc_conv.last_rescan`), the MPP voltage correction of that rescan on input A (`dc_conv.dc_conv_struct[0].rescan_correction`) and the MPP voltage target on input A (`dc_conv.dc_conv_struct[0].u_target`) at least once a minute, together with the existing state objects, and keep each raw value with the time it was read. It SHALL only read; it SHALL send no write to the inverter for these objects.

#### Scenario: A rescan recorded
- **WHEN** the inverter runs a global MPP rescan while the array produces (E14)
- **THEN** within a minute the record holds a new value of the last-rescan time, with the correction and the target voltage read in the same minute, raw

#### Scenario: Night (assumed)
- **WHEN** the array produces nothing
- **THEN** the three objects are still read every minute and kept as returned; a value the store cannot hold (NaN) is a gap, recorded as no failure, and the state still counts as read

#### Scenario: Inverter unreachable (assumed)
- **WHEN** the inverter does not answer for 10 minutes
- **THEN** those minutes have no values for these objects either (a gap, E2), and the failure is recorded once, as for the existing state objects
