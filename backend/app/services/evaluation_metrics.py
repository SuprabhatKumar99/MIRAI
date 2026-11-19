"""Compute metrics from scored agent trials and retain explicit denominators."""

from collections import Counter
import csv
from io import StringIO
from math import sqrt
from statistics import mean, stdev

from app.schemas.evaluation import EvaluationReport, Metrics, Rate, TrialEvidence


def rate(numerator: int, denominator: int) -> Rate:
    if not denominator:
        return Rate(numerator=numerator, denominator=denominator)
    fraction = numerator / denominator
    interval = None
    if denominator >= 3:
        z = 1.959963984540054
        correction = 1 + z * z / denominator
        center = (fraction + z * z / (2 * denominator)) / correction
        margin = z * sqrt(fraction * (1 - fraction) / denominator + z * z / (4 * denominator ** 2)) / correction
        interval = max(0, center - margin), min(1, center + margin)
    return Rate(numerator=numerator, denominator=denominator, value=fraction, wilson95=interval)


def summarize(label: str, trials: list[TrialEvidence], scheduled: int) -> Metrics:
    scored = [trial for trial in trials if trial.scored]
    native = [trial for trial in trials if trial.role == "agent" and trial.origin == "harbor"]
    features = [trial.feature_tests for trial in scored if trial.feature_tests is not None]
    regressions = [trial.regression_tests for trial in scored if trial.regression_tests is not None]
    builds = [trial.build_passed for trial in scored if trial.build_passed is not None]
    durations = [trial.agent_duration_seconds for trial in native if trial.agent_duration_seconds is not None]
    inputs = [trial.input_tokens for trial in native if trial.input_tokens is not None]
    outputs = [trial.output_tokens for trial in native if trial.output_tokens is not None]
    costs = [trial.cost_usd for trial in native if trial.cost_usd is not None]
    return Metrics(
        label=label, scheduled=scheduled, recorded=len(trials), scored=len(scored),
        verified_success=rate(sum(trial.reward == 1 for trial in scored), len(scored)),
        feature_pass=Rate(numerator=sum(item.passed for item in features), denominator=sum(item.total for item in features),
                          value=_ratio(features)),
        regression_pass=Rate(numerator=sum(item.passed for item in regressions),
                             denominator=sum(item.total for item in regressions), value=_ratio(regressions)),
        build_success=rate(sum(builds), len(builds)), failure_counts=dict(Counter(t.failure for t in native if t.failure)),
        mean_agent_seconds=mean(durations) if durations else None, agent_seconds_observed=len(durations),
        sample_agent_seconds_sd=stdev(durations) if len(durations) >= 2 else None,
        input_tokens_total=sum(inputs) if inputs else None, input_tokens_observed=len(inputs),
        output_tokens_total=sum(outputs) if outputs else None, output_tokens_observed=len(outputs),
        cost_usd_total=sum(costs) if costs else None, cost_observed=len(costs),
    )


def _ratio(counts):
    return sum(item.passed for item in counts) / sum(item.total for item in counts) if counts else None


def populate_metrics(report: EvaluationReport) -> None:
    if report.mode != "agent":
        return
    report.metrics = [
        summarize(agent.label, [trial for trial in report.trials if trial.label == agent.label], report.repeats)
        for agent in report.agents
    ]
    if report.package:
        total = summarize("all", report.trials, report.repeats * len(report.agents))
        report.strata = {name: {getattr(report.package, name): total} for name in ("language", "category", "difficulty")}


def trials_csv(report: EvaluationReport) -> str:
    output = StringIO(newline="")
    fields = (
        "campaign_id", "package_sha256", "role", "origin", "label", "attempt", "model", "requested_model", "resolved_models",
        "harbor_trial_id", "scored", "reward", "failure", "build_passed", "feature_passed", "feature_total",
        "regression_passed", "regression_total", "agent_duration_seconds", "input_tokens", "output_tokens", "cost_usd",
    )
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    trials = [trial for trial in (report.oracle, report.baseline_control) if trial] + report.trials
    for trial in trials:
        writer.writerow({
            "campaign_id": report.id, "package_sha256": report.package.package_sha256 if report.package else None,
            "role": trial.role, "origin": trial.origin, "label": trial.label, "attempt": trial.attempt,
            "model": trial.configuration.model if trial.configuration else None,
            "requested_model": trial.requested_model,
            "resolved_models": "|".join(trial.resolved_models), "harbor_trial_id": trial.harbor_trial_id,
            "scored": trial.scored, "reward": trial.reward, "failure": trial.failure, "build_passed": trial.build_passed,
            "feature_passed": trial.feature_tests.passed if trial.feature_tests else None,
            "feature_total": trial.feature_tests.total if trial.feature_tests else None,
            "regression_passed": trial.regression_tests.passed if trial.regression_tests else None,
            "regression_total": trial.regression_tests.total if trial.regression_tests else None,
            "agent_duration_seconds": trial.agent_duration_seconds, "input_tokens": trial.input_tokens,
            "output_tokens": trial.output_tokens, "cost_usd": trial.cost_usd,
        })
    return output.getvalue()


def summary_csv(report: EvaluationReport) -> str:
    output = StringIO(newline="")
    writer = csv.writer(output)
    names = ("verified_success", "feature_pass", "regression_pass", "build_success")
    writer.writerow([
        "campaign_id", "package_sha256", "dimension", "group", "scheduled", "recorded", "scored",
        *(f"{name}_{column}" for name in names for column in ("numerator", "denominator", "value", "wilson95_low", "wilson95_high")),
        "mean_agent_seconds", "agent_seconds_observed", "sample_agent_seconds_sd", "input_tokens_total", "input_tokens_observed",
        "output_tokens_total", "output_tokens_observed", "cost_usd_total", "cost_observed", "timeout_count", "agent_crash_count",
    ])
    rows = [("configuration", item.label, item) for item in report.metrics]
    rows.extend((dimension, key, item) for dimension, groups in report.strata.items() for key, item in groups.items())
    for dimension, key, item in rows:
        values = []
        for name in names:
            measurement = getattr(item, name)
            values.extend((measurement.numerator, measurement.denominator, measurement.value,
                           *(measurement.wilson95 or (None, None))))
        writer.writerow([
            report.id, report.package.package_sha256 if report.package else None, dimension, key,
            item.scheduled, item.recorded, item.scored, *values, item.mean_agent_seconds, item.agent_seconds_observed,
            item.sample_agent_seconds_sd, item.input_tokens_total, item.input_tokens_observed, item.output_tokens_total,
            item.output_tokens_observed, item.cost_usd_total, item.cost_observed,
            item.failure_counts.get("timeout", 0), item.failure_counts.get("agent_crash", 0),
        ])
    return output.getvalue()
