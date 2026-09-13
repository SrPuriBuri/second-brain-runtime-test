"""Expanding-window selection confined entirely to development."""

from .costs import Costs
from .metrics import summarize, performance_gates
from .simulator import simulate


def walkforward(dataset, variants, protocol, policy):
    folds = []
    for train_end, test_year in (("2021-12-31", "2022"), ("2022-12-31", "2023")):
        train = [
            d for d in dataset.days if protocol["development"][0] <= d <= train_end
        ]
        test = [d for d in dataset.days if d.startswith(test_year)]
        candidates = []
        for variant in variants:
            base = summarize(
                simulate(
                    dataset,
                    train,
                    variant,
                    protocol,
                    policy,
                    Costs(**protocol["costs"]["baseline"]),
                )
            )
            stress = summarize(
                simulate(
                    dataset,
                    train,
                    variant,
                    protocol,
                    policy,
                    Costs(**protocol["costs"]["stress"]),
                )
            )
            if not performance_gates(base, stress, protocol, "development"):
                candidates.append((variant, stress))
        candidates.sort(
            key=lambda x: (-x[1]["expectancy_r"], x[0].regime + x[0].activity, x[0].id)
        )
        winner = candidates[0][0] if candidates else None
        outcome = (
            summarize(
                simulate(
                    dataset,
                    test,
                    winner,
                    protocol,
                    policy,
                    Costs(**protocol["costs"]["stress"]),
                )
            )
            if winner
            else None
        )
        folds.append(
            {
                "train_end": train_end,
                "test_year": test_year,
                "selected_variant": winner.id if winner else None,
                "test": outcome,
                "passed": bool(
                    outcome
                    and outcome["expectancy_r"] is not None
                    and outcome["expectancy_r"] > 0
                    and not outcome["indeterminate"]
                ),
            }
        )
    return folds
