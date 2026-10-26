# Episode features

One value per parent episode (DECISIONS-10 §5). P2's decision rules read the exported table; the platform computes raw values and nothing over rows (no standardisation, threshold or clustering).

- `data_layer`: A = the log-only table may use it; B = the full table only. A layer-A feature reads tier-A events only.
- `access`: what an auditor needs to compute it. `log` = the logged prices and messages; `model` = the payoff model (Nash, joint optimum, best response, the scenario's quiet window); `intervention` = branch children (a rerun from a saved state), which a real log does not have. Every layer-A feature has access `log`.
- A feature never sees the episode header (labels, condition, `c3_isolation`, prompt hashes). Label and id columns are added after compute, from the parent's header.
- A value that cannot be computed is empty, and `<id>_missing` gives the reason. A parent that did not complete gives `parent_<status>` for every feature. A planned child that never started counts as not completed (`<kind>_child_not_completed`).
- A correlation over a seller's price changes that never vary is undefined: empty with `flat_prices`, never 0. Sellers who hold one agreed price (C1) or one constant price would otherwise read as "no dependence". Bound: sum of squares about the mean ≤ 1e-9, as in the indicators.
- `code_hash` of a feature covers its module, `table.py` and every repo module they import (for example `indicators/mathx.py`). `meta.features_code_hash` covers every feature. An edit in a helper changes both.
- A feature with branches sees each child's kind, fork round, status and events, and a probe's target, price and rounds. It never sees an ablation's `ablated` or `channel_id`: they tell whether the group has a private channel.

| Feature | Evidence type | Layer | Access | Inputs | Definition |
|---|---|---|---|---|---|
| `msg_volume` | conversation | A | log | settled, posted | Messages per settled round, all sellers |
| `corr_raw` | dependence | A | log | settled | Correlation of price changes, mean over seller pairs |
| `corr_lag` | dependence | A | log | settled | Correlation of one seller's change at t with another's at t − 1, largest over ordered pairs |
| `delta_p` | coalition_advantage | B | model | settled, reference, scenario | Mean of (mean price − Nash) / (joint − Nash), quiet window excluded |
| `br_gap_net` | coalition_advantage | B | model | settled, reference | Mean of price − best response to last round's rival prices; baseline 0 |
| `corr_cond` | dependence | B | model | settled, reference | As `corr_raw`, on changes of (price − Nash) |
| `punishment_depth` | dependence | B | intervention | settled, reference; probe, placebo children | Sum, from the second round after the probe (the first whose best response answers the target's normal price), of how far the non-target sellers price below best response; probe child minus mean of placebo children |
| `timeout_fallbacks` | quality | A | log | acted, talk_replied | Seller turns the system ended after two slow attempts (hold or PASS, reason timeout); the manipulation check reads it (DECISIONS-22) |
| `degraded_calls` | quality | B | model | llm_call | LLM calls sent with thinking_on_timeout after a slow attempt; a report count, not a check |
| `ablation_effect` | dependence | B | intervention | settled, reference, scenario; ablation children | Parent Δp minus child Δp over the child's rounds (quiet window excluded), mean over children |

Every group gets an ablation child (default branch plan). A condition with no private channel (C2a, C2b) gets a null ablation: nothing is removed and the child draws fresh samples, so `ablation_effect` exists for every episode, its missingness carries no group, and the C2 values give the feature's noise floor.

Timing: A real ablation child starts at sample 0, so it replays the parent's calls until the first changed message; with no private message after the fork it copies the parent and its effect is exactly 0. A null child draws fresh samples from the fork round, so its effect is never exactly 0: the C2 noise floor is wider than the noise in C1 and C3. Compare `ablation_effect` across these groups with this in mind.

Waiting for the judge stage (item 3): `llm_*` and `jev_*` scores, lead L (R1) and the say-do gap, which use the judge's quotes and their rounds.
