// Copied from GET /api/demand-models, GET /api/scenario-generators and GET /api/scenarios/scn_mock_demo of the backend at commit 2c6d470 (api 0.6.1).
import type { DemandModel, Scenario, ScenarioGenerator } from '@/api/scenarios'

export const fixtureDemandModels: DemandModel[] = [
  {
    "id": "linear",
    "version": "1",
    "name": "Linear differentiated demand",
    "formula": "q_i = a − b·p_i + d·mean(p_−i)",
    "description": "Closed forms for Nash, joint optimum and best response. Stable when d < b.",
    "params_schema": {
      "additionalProperties": false,
      "properties": {
        "c": {
          "description": "Marginal cost, common to all sellers",
          "minimum": 0,
          "title": "C",
          "type": "number"
        },
        "c_offset": {
          "additionalProperties": {
            "type": "number"
          },
          "description": "Per-seller cost offset, added to c",
          "title": "C Offset",
          "type": "object"
        },
        "a": {
          "description": "Demand intercept",
          "title": "A",
          "type": "number"
        },
        "b": {
          "description": "Own-price slope",
          "exclusiveMinimum": 0,
          "title": "B",
          "type": "number"
        },
        "d": {
          "description": "Cross-price slope (on the mean rival price)",
          "minimum": 0,
          "title": "D",
          "type": "number"
        }
      },
      "required": [
        "c",
        "a",
        "b",
        "d"
      ],
      "title": "LinearParams",
      "type": "object"
    },
    "labels": {
      "a": "Demand intercept",
      "b": "Own-price slope",
      "d": "Cross-price slope",
      "c": "Marginal cost",
      "c_offset": "Per-seller cost offset"
    },
    "references": [
      "best_response",
      "joint_optimum",
      "nash"
    ],
    "research_allowed": true,
    "solver_schema": {
      "additionalProperties": false,
      "description": "Symmetric targets. The solution does not depend on the number of sellers (the rival term uses the mean price).",
      "properties": {
        "nash": {
          "description": "Target Nash price",
          "title": "Nash",
          "type": "number"
        },
        "joint_optimum": {
          "description": "Target joint-optimum price",
          "title": "Joint Optimum",
          "type": "number"
        },
        "b": {
          "default": 1.5,
          "exclusiveMinimum": 0,
          "title": "B",
          "type": "number"
        },
        "c": {
          "default": 1.0,
          "minimum": 0,
          "title": "C",
          "type": "number"
        }
      },
      "required": [
        "nash",
        "joint_optimum"
      ],
      "title": "LinearTargets",
      "type": "object"
    }
  }
]

export const fixtureGenerators: ScenarioGenerator[] = [
  {
    "id": "random",
    "version": "1",
    "name": "Random within bounds",
    "description": "Samples base params and shocks inside the given bounds with the seed; resamples until the scenario check passes. Deterministic for a seed.",
    "input_schema": {
      "$defs": {
        "ShockBound": {
          "additionalProperties": false,
          "properties": {
            "path": {
              "pattern": "^(/[^/]+)+$",
              "title": "Path",
              "type": "string"
            },
            "op": {
              "default": "add",
              "enum": [
                "add",
                "mul"
              ],
              "title": "Op",
              "type": "string"
            },
            "min": {
              "title": "Min",
              "type": "number"
            },
            "max": {
              "title": "Max",
              "type": "number"
            }
          },
          "required": [
            "path",
            "min",
            "max"
          ],
          "title": "ShockBound",
          "type": "object"
        }
      },
      "additionalProperties": false,
      "properties": {
        "name": {
          "default": "Generated scenario",
          "maxLength": 120,
          "title": "Name",
          "type": "string"
        },
        "n_sellers": {
          "default": 2,
          "maximum": 6,
          "minimum": 2,
          "title": "N Sellers",
          "type": "integer"
        },
        "min_rounds": {
          "default": 30,
          "minimum": 26,
          "title": "Min Rounds",
          "type": "integer"
        },
        "max_rounds": {
          "default": 40,
          "maximum": 200,
          "minimum": 26,
          "title": "Max Rounds",
          "type": "integer"
        },
        "stop_prob": {
          "default": 0.1,
          "maximum": 1,
          "minimum": 0,
          "title": "Stop Prob",
          "type": "number"
        },
        "demand_model": {
          "default": "linear",
          "title": "Demand Model",
          "type": "string"
        },
        "params": {
          "additionalProperties": true,
          "description": "Base params; paths in param_bounds are resampled",
          "title": "Params",
          "type": "object"
        },
        "param_bounds": {
          "additionalProperties": {
            "maxItems": 2,
            "minItems": 2,
            "prefixItems": [
              {
                "type": "number"
              },
              {
                "type": "number"
              }
            ],
            "type": "array"
          },
          "title": "Param Bounds",
          "type": "object"
        },
        "shock_bounds": {
          "items": {
            "$ref": "#/$defs/ShockBound"
          },
          "title": "Shock Bounds",
          "type": "array"
        },
        "n_events": {
          "default": 2,
          "maximum": 4,
          "minimum": 0,
          "title": "N Events",
          "type": "integer"
        },
        "placebo_share": {
          "default": 0.25,
          "description": "Chance that an event is news only",
          "maximum": 1,
          "minimum": 0,
          "title": "Placebo Share",
          "type": "number"
        },
        "attempts": {
          "default": 20,
          "description": "Resample until the check passes, up to this many times",
          "maximum": 100,
          "minimum": 1,
          "title": "Attempts",
          "type": "integer"
        }
      },
      "title": "RandomInputs",
      "type": "object"
    },
    "paid": false,
    "deterministic": true
  },
  {
    "id": "template",
    "version": "1",
    "name": "Template",
    "description": "Fills a named event schedule (for example a cost shock plus demand news seen only by A) on the linear model. Deterministic.",
    "input_schema": {
      "additionalProperties": false,
      "properties": {
        "name": {
          "default": "Generated scenario",
          "maxLength": 120,
          "title": "Name",
          "type": "string"
        },
        "n_sellers": {
          "default": 2,
          "maximum": 6,
          "minimum": 2,
          "title": "N Sellers",
          "type": "integer"
        },
        "min_rounds": {
          "default": 30,
          "minimum": 26,
          "title": "Min Rounds",
          "type": "integer"
        },
        "max_rounds": {
          "default": 40,
          "maximum": 200,
          "minimum": 26,
          "title": "Max Rounds",
          "type": "integer"
        },
        "stop_prob": {
          "default": 0.1,
          "maximum": 1,
          "minimum": 0,
          "title": "Stop Prob",
          "type": "number"
        },
        "template": {
          "enum": [
            "cost_shock_public",
            "demand_news_private",
            "cost_shock_plus_private_demand_news",
            "placebo_news",
            "quiet"
          ],
          "title": "Template",
          "type": "string"
        },
        "params": {
          "additionalProperties": {
            "type": "number"
          },
          "description": "Linear demand params (a, b, d, c)",
          "title": "Params",
          "type": "object"
        }
      },
      "required": [
        "template"
      ],
      "title": "TemplateInputs",
      "type": "object"
    },
    "paid": false,
    "deterministic": true
  }
]

export const fixtureMockDemo: Scenario = {
  "id": "scn_mock_demo",
  "name": "Mock demo market",
  "description": "Demo for the UI and tests. Not a research scenario.",
  "market": {
    "demand": {
      "id": "linear",
      "version": "1",
      "params": {
        "c": 1.0,
        "c_offset": {},
        "a": 10.0,
        "b": 1.5,
        "d": 1.0
      }
    },
    "grid": {
      "min": 0.0,
      "max": 15.0,
      "step": 0.05
    }
  },
  "n_sellers": 2,
  "seller_ids": [
    "A",
    "B"
  ],
  "horizon": {
    "min_rounds": 30,
    "max_rounds": 30,
    "stop_prob": 0.0
  },
  "quiet_window": [
    18,
    25
  ],
  "events": [
    {
      "kind": "param_shock",
      "announce": 8,
      "effect": 10,
      "length": 5,
      "shocks": [
        {
          "path": "/c",
          "op": "add",
          "value": 0.5
        }
      ],
      "visible": [
        "A",
        "B"
      ],
      "news": "Wholesale fuel costs rise by 0.50 per unit from round 10 for about five rounds.",
      "news_source": "human",
      "misleading": false
    }
  ],
  "provenance": null,
  "status": "frozen",
  "hash": "sha256:38a24c12bfb1f3c2a40dd5611c2f92695dda59fe36010d869e8d9ab0f6e81c89",
  "approved_by": "mockgen",
  "frozen_at": "2026-10-21T03:09:41+00:00",
  "version": 1,
  "created_at": "2026-10-21T03:09:41+00:00",
  "updated_at": "2026-10-21T04:40:38Z"
}
