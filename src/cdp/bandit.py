"""Contextual multi-armed bandit: linear Thompson sampling with one arm per image theme.

Each arm keeps a belief about how a person's context affects its click rate. A decision samples
from every arm's belief and uses the highest sample, so uncertain content keeps getting tested
and strong content gets used more. Learning runs in batches from recorded feedback.
"""
import math

# Fixed scaling keeps each input in a similar range. The first value of a context is the intercept.
SCALES = {
    "tenure_months": (60.0, 35.0),
    "app_sessions_30d": (5.0, 5.0),
    "days_since_active": (60.0, 35.0),
    "email_open_rate": (0.3, 0.15),
    "wellness_member": (0.3, 0.45),
    "touches": (1.5, 1.2),
    "opens": (0.5, 0.8),
    "clicks": (0.2, 0.5),
    "is_direct_mail": (0.3, 0.45),
}
PRIOR_PRECISION = 1.0
EXPLORATION_SCALE = 0.25  # Roughly the spread of a click outcome around its rate.


def context(features):
    values = [1.0]
    for name, (mean, scale) in SCALES.items():
        value = features.get(name)
        values.append(0.0 if value is None else (float(value) - mean) / scale)
    return values


def new_stats(arms):
    size = len(SCALES) + 1
    return {
        arm: {
            "a": [[PRIOR_PRECISION if i == j else 0.0 for j in range(size)] for i in range(size)],
            "b": [0.0] * size,
            "pulls": 0,
            "rewards": 0,
        }
        for arm in arms
    }


def add(stats, arm, features, reward):
    if arm not in stats:
        return
    x = context(features)
    entry = stats[arm]
    for i, row in enumerate(entry["a"]):
        xi = x[i]
        for j in range(len(row)):
            row[j] += xi * x[j]
    if reward:
        for i, xi in enumerate(x):
            entry["b"][i] += xi
    entry["pulls"] += 1
    entry["rewards"] += 1 if reward else 0


def _inverse(matrix):
    size = len(matrix)
    work = [list(row) + [1.0 if i == j else 0.0 for j in range(size)] for i, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(work[row][column]))
        work[column], work[pivot] = work[pivot], work[column]
        scale = work[column][column]
        work[column] = [value / scale for value in work[column]]
        for row in range(size):
            if row != column and work[row][column]:
                factor = work[row][column]
                work[row] = [value - factor * other for value, other in zip(work[row], work[column])]
    return [row[size:] for row in work]


def _cholesky(matrix):
    size = len(matrix)
    lower = [[0.0] * size for _ in range(size)]
    for i in range(size):
        for j in range(i + 1):
            total = matrix[i][j] - sum(lower[i][k] * lower[j][k] for k in range(j))
            lower[i][j] = math.sqrt(max(total, 1e-12)) if i == j else total / lower[j][j]
    return lower


def to_model(stats, version):
    arms = {}
    for arm, entry in stats.items():
        covariance = _inverse(entry["a"])
        arms[arm] = {
            "mean": [round(sum(c * b for c, b in zip(row, entry["b"])), 6) for row in covariance],
            "chol": [[round(value, 6) for value in row] for row in _cholesky(covariance)],
            "pulls": entry["pulls"],
            "rewards": entry["rewards"],
        }
    return {"type": "bandit", "version": version, "features": list(SCALES), "arms": arms}


def expected(model, arm, features):
    """Return the arm's expected click rate, without any exploration."""
    entry = model["arms"].get(arm)
    if entry is None:
        return None
    x = context(features)
    return max(0.0, min(1.0, sum(a * b for a, b in zip(x, entry["mean"]))))


def sample(model, arm, x, rng):
    """Return a click rate sampled from the arm's current belief."""
    entry = model["arms"][arm]
    noise = [rng.gauss(0.0, 1.0) for _ in x]
    total = 0.0
    for i, xi in enumerate(x):
        row = entry["chol"][i]
        drift = sum(row[j] * noise[j] for j in range(i + 1))
        total += xi * (entry["mean"][i] + EXPLORATION_SCALE * drift)
    return total


def choose_arm(model, features, rng):
    x = context(features)
    return max(model["arms"], key=lambda arm: sample(model, arm, x, rng))
