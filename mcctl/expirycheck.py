"""`mcctl expiry-check`: the weekly credential and domain expiry check (design §3 "Expiry", §6, P11).

Runs in the config repo's Monday `expiry-check` job with **no admin token**: it reads `expiry.yml` (written
and committed by `mcctl doctor`, names and dates only), asks RDAP for the zone's registration expiry, and
reads the watchdog Worker's public `/status`. Findings:

- a credential expired, or expiring within 30 days (WARN_DAYS) -> problem;
- an entry whose `checked_at` is older than 45 days (STALE_DAYS) -> problem: run `mcctl doctor` and commit;
- the zone expiring within 30 days (or expired) -> problem; RDAP unavailable -> note only;
- the watchdog's cron not run for 15 minutes, or a check late/failing -> problem (who watches the watchdog);
  not deployed yet (deploy.enabled false) -> skipped.

Problems go to Discord (DISCORD_WEBHOOK_URL, when alerts.discord; one alert in the product's template) and
fail the job; nothing secret is read.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from mcctl import alerttext, configrepo, http, render
from mcctl.providers import rdap, watchdog

WARN_DAYS = 30
STALE_DAYS = 45


@dataclass
class Findings:
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _date(v: object) -> datetime.date | None:
    if v is None or isinstance(v, datetime.date):
        return v  # type: ignore[return-value]
    return datetime.date.fromisoformat(str(v))


def check(repo: configrepo.ConfigRepo, today: datetime.date, transport: http.Transport) -> Findings:
    f = Findings()
    for c in repo.expiry:
        name, exp, checked = c["name"], _date(c.get("expires_on")), _date(c.get("checked_at"))
        if exp is not None:
            days = (exp - today).days
            if days < 0:
                f.problems.append(
                    f"credential {name} expired on {exp}: create a new one, then `mcctl doctor`"
                )
            elif days <= WARN_DAYS:
                f.problems.append(
                    f"credential {name} expires in {days} days ({exp}): renew it, then `mcctl doctor`"
                )
            else:
                f.notes.append(f"credential {name}: expires {exp}")
        else:
            f.notes.append(f"credential {name}: no expiry date")
        if checked is not None and (today - checked).days > STALE_DAYS:
            f.problems.append(
                f"credential {name} was last checked on {checked} (> {STALE_DAYS} days): run `mcctl doctor` "
                "and "
                "commit expiry.yml"
            )
    if not repo.expiry:
        f.problems.append("expiry.yml lists no credentials: run `mcctl doctor` and commit it")

    zone = repo.server.dns.zone
    try:
        when = rdap.expiration(zone, transport)
    except (rdap.RdapError, http.HttpError) as e:
        f.notes.append(f"domain {zone}: RDAP lookup failed ({e})")
    else:
        if when is None:
            f.notes.append(f"domain {zone}: the registry publishes no expiration date")
        elif (when - today).days < 0:
            f.problems.append(f"domain {zone} expired on {when}")
        elif (when - today).days <= WARN_DAYS:
            f.problems.append(f"domain {zone} expires in {(when - today).days} days ({when}): renew it")
        else:
            f.notes.append(f"domain {zone}: registered until {when}")

    if not repo.server.deploy.enabled:
        f.notes.append("watchdog: skipped (deploy.enabled is false)")
    else:
        try:
            data = watchdog.status(render.watchdog_url(repo.server), transport)
        except http.HttpError as e:
            data = None
            f.notes.append(f"watchdog: {e}")
        level, summary = watchdog.assess(data)
        (f.notes if level == "ok" else f.problems).append(f"watchdog: {summary}")
    return f


def alert(repo: configrepo.ConfigRepo, findings: Findings, run_url: str = "") -> str:
    """The Discord alert for the problems, in the product's one template (mcctl/alerttext.py)."""
    problems = findings.problems
    impact = []
    if any(p.startswith("domain ") for p in problems):
        impact.append("if the domain expires, players can't find the server by its address any more")
    if any(p.startswith(("credential ", "expiry.yml")) for p in problems):
        impact.append(
            "players are not affected yet, but an expired credential stops deploys, backups or the "
            "watchdog (whichever uses it)"
        )
    if any(p.startswith("watchdog:") for p in problems):
        impact.append("outages and late backups may go unreported while the watchdog isn't checking")
    text = "; ".join(impact)
    rerun = f"the run: {run_url}" if run_url else "Actions, server, Run workflow, expiry-check"
    return alerttext.render(
        alerttext.Alert(
            "action",
            f"The weekly expiry check found {len(problems)} problem(s) for {repo.server.name}",
            impact=text[:1].upper() + text[1:] + ".",
            cause="Credentials and the domain registration have fixed lifetimes, and expiry.yml is only as "
            "fresh as the last `mcctl doctor`.",
            todo="one by one:\n"
            + "\n".join(f"- {p}" for p in problems)
            + f"\nThen commit expiry.yml and re-run the check ({rerun}).",
            details=f"{len(problems)} problem(s), {len(findings.notes)} OK; nothing secret was read",
        )
    )


def report(
    repo: configrepo.ConfigRepo,
    findings: Findings,
    webhook: str,
    post: Callable[[str, str], None],
    run_url: str = "",
) -> str:
    text = "\n".join(f"- {p}" for p in findings.problems)
    if findings.problems and repo.server.alerts.discord and webhook:
        post(webhook, alert(repo, findings, run_url))
    return text


def run_url(env: Mapping[str, str]) -> str:
    """This Actions run's page (GitHub sets these variables in every job), or "" outside Actions."""
    parts = [env.get(k, "") for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")]
    return f"{parts[0]}/{parts[1]}/actions/runs/{parts[2]}" if all(parts) else ""


def discord_post(transport: http.Transport) -> Callable[[str, str], None]:
    def post(url: str, message: str) -> None:
        # allowed_mentions: nothing in a message can ping anyone (same as the Worker and health.py)
        http.send_json(transport, "POST", url, {"content": message, "allowed_mentions": {"parse": []}})

    return post
