# Walk-through: the pilot operator evaluates data collection, models and optimizer candidates before the switchover

Persona: P1, the pilot operator (design/stakeholders.md): knows the heat pump, the house's physics and numerical optimisation; no longer codes by hand, works through coding agents; reads results on dashboards, not in notebooks. Confidence: answered by the principal on 2026-11-23, step by step in chat; the system walked does not exist yet (on paper).
What exists: the recording (S1: Home Assistant, forecast archive, collector), requirements S2–S6, research notes on the building, heat pump, PV and solver; no model or optimizer yet.

Phase: from now until the switchover at the turn of the year 2026/2027 (requirements S4). Advice only, nothing written to devices. A later walk covers daily use after the switchover (the quick glance at the 48 h plan).

## Steps
1. **Check data collection.** Does: looks at whether every source is complete (inverter, its logs, Onecta, forecasts, satellite irradiance, MPP objects; a room thermometer by then). Needs to know: gaps and failures per source. Goes wrong: a source silently incomplete. Would ask: "Is everything I'll judge the models on actually there?"
2. **Track A, house and unit model.** The model runs today's heating curve and schedules and must reproduce the house. Does: compares predicted with measured, one plot per pair: room temperature (thermometer), the heat pump's power draw, the shift or setpoint in effect. Needs to know: how far off, over time. "Good enough" for room temperature, a first guess: 0.2 °C. Would ask: "Does the house model fit reality well enough to trust its what-ifs?"
3. **Track B, shadow optimizer.** Does: compares what the candidate would have done (shifts, switching times) with what the current schedule did, one plot per pair; sees the room temperature it predicts under its own plan, and the predicted saving against the status quo in grid import (kWh) and the heat pump's electricity (kWh), side by side. Not in €: "€ loses its link to the physics, which we are working on." Goes wrong: the candidate keeps advising values the principal would not set. Would ask: "Would it have kept the house comfortable, and saved what?"
4. **Track D, PV forecast.** Does: compares each forecast source (forecast.solar, Open-Meteo, later a corrected forecast) with actual production, one plot per pair; satellite irradiance separates weather-forecast error from PV-model error. Both horizons matter equally: the next hours and tomorrow ("I make hour-to-hour and day-to-day decisions").
5. **Compare candidates and decide.** Several candidates run in parallel for each track. Does: sees all candidates of a track in one plot with overall figures (RMS error, electricity saving), to decide who is at the helm in 2027; opens a single candidate's predicted-against-measured pair for detailed analysis. Views are grouped per candidate, so that no single one gets crowded. Would ask: "Which one do I put in charge?"
- **A separate question:** is compressor cycling a problem, and what would raising the overshoot [9-04] from 2 to 4 °C change? Answered from the household load (starts per day, run lengths) and, once track A fits, by simulating both settings on the same weeks. Not a requirement on the model.
- Track C, PV array health, runs separately (S6) and is not part of this decision.

## Product hat
What P1 wants from this phase: evidence, not a demo. "Done" is a decision at the turn of the year with figures behind it: which candidate controls the heat pump in 2027. Complaints to expect: plots crowded with several candidates and quantities at once; a saving that cannot be traced to physical quantities; a candidate whose advice is outside the band the principal would accept.

## Developer hat
- Every model and candidate must record its predictions as issued (with issue time and horizon), like the forecast archive, or "predicted 24 h earlier" cannot be shown later.
- The shadow optimizer's saving is a prediction from track A's model; its credibility rests on track A's error. The views must show both together.
- Several candidates in parallel multiply computation and storage; acceptable on the automation PC at hourly re-planning (agent's estimate, not measured).
- Room temperature is measured by one thermometer, which stands for the operative temperature (requirements 2.7); its placement limits what 0.2 °C means.

## Questions for the principal
- How is "0.2 °C" meant: the typical (RMS) deviation of the prediction made 24 h earlier? (step 2)
- Does the fallback stay a criterion for the switchover: if the optimizer fails, the unit returns to today's schedule, tested once on purpose? (step 5)
- What share of time outside the acceptable band is still "the overwhelming majority" inside it? (step 3)

## Answers from the principal (P1, 2026-11-23)
- Evaluation on dashboards, no notebooks: "I'm not coding anymore pretty much at all since I use frontier agentic coding."
- Four tracks: A house and unit model with today's curve; B optimized control vs actual; C PV health, separately; D PV forecast quality.
- Measured against predicted, one plot per pair, "otherwise it gets hard to read".
- Room temperature "good enough": 0.2 °C (first guess).
- Saving: grid import and heat pump electricity in kWh, not €.
- Forecast horizons: next hours and tomorrow, equally.
- Safety is enforced by the unit and is not a criterion here; "consistently advising values we don't want to set is an issue", so plans stay within the band "for the overwhelming majority of time".
- Several candidates in parallel per track, grouped per candidate; not fixed on "dashboard" as the implementation, but on what is seen: all candidates in one plot with overall figures, and a predicted-against-measured pair for detail.
- Every view stays navigable ("not scroll down to bedrock"), and it is clearly recognizable which plot is what.
- Cycling and overshoot: "not sure if it is a question for the model per se, but a question I'd like to answer".
- Fallback: yes, a criterion; ideally the optimizer only modulates the shift like the time schedule and switches the unit on and off, "so that it resets automatically when the schedule advances, resp. it can be turned on easily in the app".
- Deferred: the 0.2 °C definition and the band share, to the first data (owner: principal).

## Gaps
- No record of predictions as issued for models and candidates (steps 2–5).
- No definition of the band of acceptable advice per quantity (step 3), beyond the unit's limits.
- No candidate registry: names, versions, what each is (step 5).

## Acceptance examples
E15, E16, E17, E18, E19 (added to design/examples.md).

## Questionnaire
Not run; the requirements record covers this phase's demand. Skipped: who operates the comparison after the pilot (P2 will not have several candidates).
